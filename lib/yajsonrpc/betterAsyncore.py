# Copyright (C) 2014 Saggi Mizrahi, Red Hat Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public
# License along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA 02110-1301 USA

# Asyncore uses inheritance all around which makes it not flexible enough for
# us to use. This does tries to reuse enough code from the original asyncore
# while enabling compositing instead of inheritance.
from __future__ import absolute_import
import asyncore
import logging
import socket
from errno import EWOULDBLOCK

from vdsm.sslcompat import sslutils
from vdsm.infra.eventfd import EventFD


class Dispatcher(asyncore.dispatcher):

    _log = logging.getLogger("vds.dispatcher")

    def __init__(self, impl=None, sock=None, map=None):
        # This has to be done before the super initialization because
        # dispatcher implements __getattr__.
        self.__impl = None
        asyncore.dispatcher.__init__(self, sock=sock, map=map)
        if impl is not None:
            self.switch_implementation(impl)

    def handle_connect(self):
        self._delegate_call("handle_connect")

    def handle_close(self):
        self._delegate_call("handle_close")

    def handle_accept(self):
        self._delegate_call("handle_accept")

    def handle_expt(self):
        self._delegate_call("handle_expt")

    def handle_error(self):
        self._delegate_call("handle_error")

    def readable(self):
        return self._delegate_call("readable")

    def writable(self):
        return self._delegate_call("writable")

    def handle_read(self):
        self._delegate_call("handle_read")

    def handle_write(self):
        self._delegate_call("handle_write")

    def switch_implementation(self, impl):
        self.__impl = impl

        if hasattr(impl, 'init'):
            impl.init(self)

    def next_check_interval(self):
        """
        Return the relative timeout wanted between poller refresh checks

        The function should return the number of seconds it wishes to wait
        until the next update. None should be returned in cases where the
        implementation doesn't care.

        Note that this value is a recommendation only.
        """
        default_func = lambda: None
        return getattr(self.__impl, "next_check_interval", default_func)()

    def recv(self, buffer_size):
        try:
            data = self.socket.recv(buffer_size)
            if data == "":
                # a closed connection is indicated by signaling
                # a read condition, and having recv() return 0.
                self.handle_close()
                return ''
            else:
                return data
        except socket.error as why:
            # winsock sometimes raises ENOTCONN
            if why.args[0] == EWOULDBLOCK:
                return None
            elif why.args[0] in asyncore._DISCONNECTED:
                self.handle_close()
                return ''
            else:
                raise
        except sslutils.SSLError as e:
            self._log.error('SSL error during reading data: %s', e)
            self.handle_close()
            return ''

    def send(self, data):
        try:
            result = self.socket.send(data)
            if result == -1:
                return 0
            return result
        except socket.error as why:
            if why.args[0] == EWOULDBLOCK:
                return 0
            elif why.args[0] in asyncore._DISCONNECTED:
                self.handle_close()
                return 0
            else:
                raise
        except sslutils.SSLError as e:
            self._log.error('SSL error during sending data: %s', e)
            self.handle_close()
            return 0

    def del_channel(self, map=None):
        asyncore.dispatcher.del_channel(self, map)
        self.__impl = None
        self.connected = False

    def _delegate_call(self, name):
        if hasattr(self.__impl, name):
            return getattr(self.__impl, name)(self)
        else:
            return getattr(asyncore.dispatcher, name)(self)

    # Override asyncore.dispatcher logging to use our logger
    log = _log.debug

    def log_info(self, message, type='info'):
        level = getattr(logging, type.upper(), None)
        if not isinstance(level, int):
            raise ValueError('Invalid log level: %s' % type)
        self._log.log(level, message)


class AsyncoreEvent(asyncore.file_dispatcher):
    def __init__(self, map=None):
        self._eventfd = EventFD()
        try:
            asyncore.file_dispatcher.__init__(
                self,
                self._eventfd.fileno(),
                map=map
            )
        except:
            self._eventfd.close()
            raise

    def writable(self):
        return False

    def set(self):
        self._eventfd.write(1)

    def handle_read(self):
        self._eventfd.read()

    def close(self):
        try:
            self._eventfd.close()
        except (OSError, IOError):
            pass

        asyncore.file_dispatcher.close(self)


class Reactor(object):
    """
    map dictionary maps sock.fileno() to channels to watch. We add channels to
    it by running add_dispatcher and removing by remove_dispatcher.
    It is used by asyncore loop to know which channels events to track.

    We use eventfd as mechanism to trigger processing when needed.
    """

    def __init__(self):
        self._map = {}
        self._is_running = False
        self._wakeupEvent = AsyncoreEvent(self._map)

    def create_dispatcher(self, sock, impl=None):
        return Dispatcher(impl=impl, sock=sock, map=self._map)

    def process_requests(self):
        self._is_running = True
        while self._is_running:
            asyncore.loop(
                timeout=self._get_timeout(self._map),
                use_poll=True,
                map=self._map,
                count=1,
            )

        for dispatcher in self._map.values():
            dispatcher.close()

        self._map.clear()

    def _get_timeout(self, map):
        timeout = 30.0
        for disp in self._map.values():
            if hasattr(disp, "next_check_interval"):
                interval = disp.next_check_interval()
                if interval is not None and interval >= 0:
                    timeout = min(interval, timeout)
        return timeout

    def wakeup(self):
        self._wakeupEvent.set()

    def stop(self):
        self._is_running = False
        try:
            self.wakeup()
        except (IOError, OSError):
            # Client woke up and closed the event dispatcher without our help
            pass
