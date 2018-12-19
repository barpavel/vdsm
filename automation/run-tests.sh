#!/bin/bash -xe

source automation/common.sh

PYTHON_VERSION="$1"

prepare_env
install_dependencies
build_vdsm

function collect_logs {
    res=$?
    [ "$res" -ne 0 ] && echo "*** err: $res"
    cd /var/log
    tar --exclude "journal/*" -czf "$EXPORT_DIR/mock_varlogs.tar.gz" *
    cd /var/host_log
    tar --exclude "journal/*" -czf "$EXPORT_DIR/host_varlogs.tar.gz" *
}

trap collect_logs EXIT

tests/profile debuginfo-install debuginfo-install -y python

# Make sure we have enough loop device nodes.
create_loop_devices 8

TIMEOUT=600 make "tests-$PYTHON_VERSION" NOSE_WITH_COVERAGE=1 NOSE_COVER_PACKAGE="$PWD/vdsm,$PWD/lib"

generate_combined_coverage_report "$PYTHON_VERSION"