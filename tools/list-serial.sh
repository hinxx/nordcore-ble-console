#!/usr/bin/env bash
set -euo pipefail
ls -l /dev/ttyUSB* /dev/ttyACM* 2>/dev/null || {
    echo "No /dev/ttyUSB* or /dev/ttyACM* devices found."
    exit 1
}
