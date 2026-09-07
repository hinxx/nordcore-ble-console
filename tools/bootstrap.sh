#!/usr/bin/env bash
set -euo pipefail

IDF_VERSION="v5.5.5"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TOOLING_DIR="${PROJECT_ROOT}/.tooling"
IDF_DIR="${TOOLING_DIR}/esp-idf"

install_host_deps_debian() {
    if ! command -v apt-get >/dev/null 2>&1; then
        echo "No apt-get detected. Install ESP-IDF host prerequisites for your distribution manually."
        return
    fi

    echo "Installing Debian/Ubuntu/Mint host prerequisites..."
    sudo apt-get update
    sudo apt-get install -y \
        git wget flex bison gperf \
        python3 python3-pip python3-venv python3-setuptools \
        cmake ninja-build ccache \
        libffi-dev libssl-dev \
        dfu-util libusb-1.0-0
}

if [[ "${1:-}" == "--skip-host-deps" ]]; then
    echo "Skipping host package installation."
else
    install_host_deps_debian
fi

mkdir -p "${TOOLING_DIR}"

if [[ ! -d "${IDF_DIR}/.git" ]]; then
    echo "Cloning ESP-IDF ${IDF_VERSION}..."
    git clone --branch "${IDF_VERSION}" --recursive --depth 1 \
        https://github.com/espressif/esp-idf.git "${IDF_DIR}"
else
    echo "ESP-IDF checkout already exists at ${IDF_DIR}"
    current_tag="$(git -C "${IDF_DIR}" describe --tags --exact-match 2>/dev/null || true)"
    if [[ "${current_tag}" != "${IDF_VERSION}" ]]; then
        echo "ERROR: existing ESP-IDF checkout is '${current_tag:-unknown}', expected ${IDF_VERSION}."
        echo "Remove ${IDF_DIR} and run this script again."
        exit 1
    fi
    git -C "${IDF_DIR}" submodule update --init --recursive
fi

echo "Installing ESP32 toolchain and ESP-IDF Python environment..."
"${IDF_DIR}/install.sh" esp32

# shellcheck disable=SC1091
. "${IDF_DIR}/export.sh"

cd "${PROJECT_ROOT}"
idf.py set-target esp32

echo
echo "Bootstrap complete."
echo "Build with:"
echo "  ./tools/idf.sh build"
echo "Flash/monitor with:"
echo "  ./tools/idf.sh -p /dev/ttyUSB0 flash monitor"
