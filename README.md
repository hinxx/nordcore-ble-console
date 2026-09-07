# ESP32 Treadmill UART Sniffer

Passive dual-UART sniffer for the treadmill console/baseboard link using an original ESP32-WROOM-32 development board.

## Electrical wiring

The treadmill serial signals are approximately 5 V logic. Use one divider per input:

```text
line A ---- 10k ----+---- GPIO26
                    |
                   15k
                    |
                   GND

line B ---- 10k ----+---- GPIO27
                    |
                   15k
                    |
                   GND

console GND -------------- ESP32 GND
```

Do not connect an ESP32 TX pin to either treadmill line in the passive-sniffer stage.

For installed operation the ESP32 board may be fed from the console's regulated +5 V rail via its `5V`/`VIN` pin. During USB development, disconnect the external +5 V feed unless the development board's USB/external-5-V power-path isolation has been verified.

## UART setup

- GPIO26 -> ESP32 UART1 RX
- GPIO27 -> ESP32 UART2 RX
- 1200 baud, 8 data bits, no parity, 2 stop bits
- UART0 remains the normal USB serial/programming console

## Bootstrap on Ubuntu / Mint / Debian

```bash
./tools/bootstrap.sh
```

This installs common host prerequisites, clones **ESP-IDF v5.5.5** recursively into `.tooling/esp-idf`, and runs Espressif's official `install.sh esp32` to fetch the target toolchain and Python tooling.

If host prerequisites are already installed:

```bash
./tools/bootstrap.sh --skip-host-deps
```

## Build

```bash
./tools/idf.sh build
```

## Flash and monitor

Find the USB serial port, commonly `/dev/ttyUSB0`, then:

```bash
./tools/idf.sh -p /dev/ttyUSB0 flash monitor
```

Exit the ESP-IDF monitor with `Ctrl+]`.

## Output

The baseline firmware emits one line per byte:

```text
000001234567 GPIO26 68
000001243729 GPIO26 0C
000001252891 GPIO26 A0
000001272201 GPIO27 68
```

The source is deliberately named by GPIO until the physical direction of each tapped line is recorded conclusively.

Timestamps are software receive time (when the sniffer task pulled the byte out of the UART driver's ring buffer), not oscilloscope-grade wire-arrival time. At 1200 baud 8N2 (~9.17 ms per character) ordinary scheduler jitter is far smaller than the inter-byte spacing, so this is fine for protocol reverse engineering but should not be treated as precise bit-level timing.

### Error events

UART framing/parity errors and RX overflow are reported inline as their own lines rather than being silently dropped, so a later analysis doesn't mistake a lossy capture for a complete one:

```text
000001234567 GPIO26 ERROR FRAME_ERR
000001235012 GPIO27 ERROR FIFO_OVF bytes_lost=unknown
000001235014 GPIO27 ERROR RX_FLUSH
```

`FIFO_OVF` and `BUFFER_FULL` are followed by an `RX_FLUSH` line once the driver's input buffer has been flushed to recover; any bytes lost to the overflow are not recoverable and are not counted.

See `REQUIREMENTS.md` for the frozen baseline intent and future stages.
