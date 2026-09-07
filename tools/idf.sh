#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
IDF_DIR="${PROJECT_ROOT}/.tooling/esp-idf"

if [[ ! -f "${IDF_DIR}/export.sh" ]]; then
    echo "ESP-IDF is not installed in ${IDF_DIR}."
    echo "Run ./tools/bootstrap.sh first."
    exit 1
fi

# shellcheck disable=SC1091
. "${IDF_DIR}/export.sh" >/dev/null

cd "${PROJECT_ROOT}"
exec idf.py "$@"
