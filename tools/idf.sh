#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
IDF_DIR="${PROJECT_ROOT}/.tooling/esp-idf"
FW_ROOT="${PROJECT_ROOT}/fw"
DEFAULT_FW="frame-sniffer"

if [[ ! -f "${IDF_DIR}/export.sh" ]]; then
    echo "ESP-IDF is not installed in ${IDF_DIR}."
    echo "Run ./tools/bootstrap.sh first."
    exit 1
fi

# Each fw/<name>/ directory is its own independent ESP-IDF project (own
# CMakeLists.txt, own main component, own version.txt) -- see README.md
# "Firmware builds". Defaults to DEFAULT_FW so existing commands like
# `./tools/idf.sh build` and `./tools/idf.sh -p /dev/ttyUSB0 flash monitor`
# keep working unchanged.
FW="${DEFAULT_FW}"
if [[ "${1:-}" == "-f" || "${1:-}" == "--fw" ]]; then
    if [[ $# -lt 2 ]]; then
        echo "Usage: $0 [-f|--fw <firmware-name>] <idf.py args...>" >&2
        exit 1
    fi
    FW="$2"
    shift 2
fi

FW_DIR="${FW_ROOT}/${FW}"
if [[ ! -d "${FW_DIR}" ]]; then
    echo "Unknown firmware '${FW}': no such directory ${FW_DIR}" >&2
    echo "Available firmware builds:" >&2
    for d in "${FW_ROOT}"/*/; do
        echo "  - $(basename "${d}")" >&2
    done
    exit 1
fi

# shellcheck disable=SC1091
. "${IDF_DIR}/export.sh" >/dev/null

exec idf.py -C "${FW_DIR}" "$@"
