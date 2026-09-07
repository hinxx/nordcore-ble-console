# ESP32 Treadmill UART Sniffer — Requirements

## 1. Purpose

Build a passive, reproducible ESP32 firmware project that captures both directions of the treadmill console/baseboard serial link and reports raw bytes over the ESP32 development board's USB serial console.

This is the baseline for later work in which the ESP32 may:

1. decode the treadmill protocol,
2. expose state over BLE and/or Wi-Fi, and
3. actively control the baseboard after the protocol is understood and explicit TX support is added.

The initial sniffer shall **not transmit onto either treadmill UART line**.

## 2. Target hardware

- Development board: AZ-Delivery ESP32 NodeMCU / ESP32-WROOM-32 class board.
- MCU family: original ESP32 (target `esp32`).
- Development USB interface: onboard USB-to-UART bridge connected to ESP32 UART0.
- Treadmill signals:
  - GPIO26: passive tap of one serial direction.
  - GPIO27: passive tap of the other serial direction.
- Common ground between treadmill console and ESP32.
- ESP32 may be powered from the console's regulated +5 V rail through the ESP32 board's `5V`/`VIN` pin for installed operation.
- During development, avoid powering the ESP32 simultaneously from console +5 V and USB unless the board's power-path isolation has been verified. Prefer USB-only power while programming/debugging, or disconnect the external +5 V feed.

## 3. Electrical interface

The treadmill UART lines have been observed at approximately 5 V logic and must not be connected directly to ESP32 GPIOs.

Each tapped signal shall use this divider:

```text
Treadmill UART signal ---- 10 kΩ ----+---- ESP32 GPIO26 or GPIO27
                                     |
                                    15 kΩ
                                     |
                                    GND
```

Nominal divider output:

- 5.0 V input -> 3.0 V
- 5.5 V input -> 3.3 V

The ESP32 connections are RX-only in the sniffer firmware. No ESP32 TX GPIO shall be connected to the treadmill bus.

## 4. Serial parameters

Both sniffing UARTs shall use ESP32 hardware UART peripherals and the following configuration:

- Baud rate: **1200 baud**
- Data bits: **8**
- Parity: **none**
- Stop bits: **2**
- Idle polarity: **high**
- Flow control: **none**
- RX only

Mapping:

| ESP32 peripheral | GPIO | Purpose |
|---|---:|---|
| UART0 | board default | USB serial console / flashing / monitor |
| UART1 | GPIO26 | treadmill line A, RX only |
| UART2 | GPIO27 | treadmill line B, RX only |

GPIO26/GPIO27 are chosen GPIO-matrix routes; they are not fixed UART pins.

## 5. Firmware behavior

The baseline firmware shall:

- initialize UART1 and UART2 as independent RX-only hardware UARTs;
- capture both streams concurrently;
- timestamp every received byte using the ESP-IDF high-resolution timer;
- write one stable, machine-readable line per received byte to the USB serial console;
- identify the source as `GPIO26` or `GPIO27` rather than assuming protocol direction until wiring direction is documented;
- report UART framing, parity, FIFO overflow, and buffer overflow events when available;
- not interpret or modify received bytes in the baseline capture path;
- not transmit on UART1 or UART2;
- remain capable of later adding a protocol parser, BLE NimBLE service, Wi-Fi, and active UART TX without replacing the SDK/framework.

Baseline output format:

```text
<timestamp_us> <source> <hex-byte>
```

Example:

```text
000001234567 GPIO26 68
000001243729 GPIO26 0C
000001252891 GPIO26 A0
000001272201 GPIO27 68
```

## 6. Software platform

- Framework: **Espressif ESP-IDF 5.x**.
- Pinned release for reproducibility: **ESP-IDF v5.5.5**.
- Language: C.
- Target: `esp32`.
- Build system: ESP-IDF CMake / `idf.py`.
- Future BLE implementation should use ESP-IDF's NimBLE host unless later requirements justify another stack.

The project must not depend on Arduino.

## 7. Reproducible toolchain setup

The repository shall contain scripts that:

1. install/check common Linux host prerequisites on Debian/Ubuntu/Mint systems;
2. clone the pinned ESP-IDF release recursively into a project-local `.tooling/esp-idf` directory;
3. run ESP-IDF's official `install.sh esp32` installer to fetch the Xtensa toolchain, Python environment, CMake/Ninja-related ESP-IDF tooling, and target tools;
4. provide a wrapper that sources ESP-IDF `export.sh` and invokes `idf.py` without requiring a system-wide ESP-IDF installation.

ESP-IDF itself and downloaded toolchains shall not be committed to the project archive/repository.

## 8. Build / flash / monitor workflow

After bootstrap:

```bash
./tools/idf.sh build
./tools/idf.sh -p /dev/ttyUSB0 flash
./tools/idf.sh -p /dev/ttyUSB0 monitor
```

or combined:

```bash
./tools/idf.sh -p /dev/ttyUSB0 flash monitor
```

The serial monitor defaults to 115200 baud through UART0.

## 9. Validation criteria

The baseline is accepted when:

1. firmware builds for target `esp32` with ESP-IDF v5.5.5;
2. the board flashes over its onboard USB serial bridge;
3. GPIO26 and GPIO27 both receive independently;
4. captured bytes match the oscilloscope UART decoder at 1200 8N2;
5. repeated idle packets can be captured without dropped bytes;
6. no activity generated by the ESP32 is visible on the treadmill UART lines.

## 10. Known protocol observations, not yet requirements

Current scope captures suggest repeated frames beginning with `0x68`, with a likely length field immediately after it and `0x43` appearing at the end of observed frames. This is **not yet treated as a protocol contract**. The baseline firmware intentionally captures raw bytes so those assumptions can be validated before a parser is introduced.

## 11. Future stages

Not implemented in this baseline:

- packet framing and checksum/CRC identification;
- semantic decoding of speed/state/commands;
- BLE GATT telemetry;
- BLE command channel;
- Wi-Fi communication;
- transmission to the treadmill baseboard;
- console emulation or replacement.

Active control must be introduced only after protocol validation and with explicit safeguards separating passive sniff mode from transmit/control mode.
