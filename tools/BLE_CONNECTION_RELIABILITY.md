# BLE connection reliability on this Linux host — known fragile, partially self-healed

**Status: cause #1 self-heals automatically in `treadmill_app.py` (see
below); cause #2 and blueman-manager itself are still open, not root-caused
to a permanent fix.** Separate from
`fw/controller/BLE_NOTIFY_LATENCY_INVESTIGATION.md` (which is about telemetry
*timing* once connected, and is resolved) -- this is about the connection
itself frequently failing to establish at all, or refusing a reconnect,
purely on the Linux/BlueZ host side. Confirmed the board's own BLE stack is
not at fault every time this has come up.

Three distinct causes found so far, all host-side (the first two) or
board-side/adapter-side (the third), all needing a manual step when they
happen apart from #1's self-heal:

## 1. `blueman-manager` grabs the connection out from under the app

**Symptom**: `treadmill_app.py`/`controller_ui.py`/`controller.py` scan fails
to find the device, or `bluetoothctl info <mac>` shows `Connected: yes` even
though nothing in the app is using that connection.

**Root cause**: `blueman-manager` (the "Bluetooth Devices" GUI, part of the
standard desktop environment here) has a background task,
`_monitor_power_levels()`, that polls raw HCI roughly once a second and logs
`Failed to get power levels, probably a LE device` -- it doesn't recognize
this device's custom GATT profile, so it just keeps failing and retrying.
Separately, opening/selecting the device's row in that GUI opens a real GATT
connection to browse its services (again because it doesn't recognize the
profile and has no defined "done" signal), and that connection isn't
released automatically. Since the board only supports one BLE connection at
a time and (correctly) refuses to re-advertise while it thinks it's
connected (see `fw/controller/main/ble_gatt.c`'s `ADV_COMPLETE` handling),
whichever side grabs the connection first locks the other out completely.

**Fix used repeatedly**: `bluetoothctl disconnect <mac>` clears it
immediately. For a longer-lived fix, `systemctl --user mask
blueman-manager.service` stops it from respawning at all (confirmed via
D-Bus service activation, `org.blueman.Manager.service` -- masking the
*service* is what's needed, not just killing the process, which respawns).
Trade-off: masking it also disables the "Bluetooth Devices" GUI entirely,
which isn't always wanted for normal desktop use -- has been masked and
unmasked several times over the course of this project depending on whether
active BLE testing was happening. Not aware of a way to keep the GUI
available while stopping just the background polling/promiscuous connecting.

## 2. Bonds get out of sync, connection fails during service discovery

**Symptom**: `bleak` accepts the connection at the link layer, then fails
with `BleakError: failed to discover services, device disconnected` -- looks
like a dead/misbehaving board, but isn't.

**Root cause, as best determined**: the device was paired/bonded (originally
done specifically to make Bluetooth-Devices-GUI encounters less disruptive,
since a bonded device needs less re-discovery). At some point BlueZ's stored
bond and the ESP32's own NVS-stored bonding state end up mismatched -- exact
trigger not pinned down (a reflash doesn't erase NVS by default, so it's not
simply "every reflash breaks it," but something does). The link comes up,
then whatever key/encryption negotiation is needed for a bonded device to
proceed to GATT discovery fails, and the stack drops the connection.

**Fix used**: `bluetoothctl remove <mac>` to delete the stale bond entirely,
then reconnect fresh and unbonded. The app's own characteristics don't
require encryption, so an unbonded connection works exactly as well as a
bonded one for actual use -- bonding here was only ever a mitigation attempt
for problem #1, not something the app needs for correctness.

## 3. The Bluetooth adapter's LE scanning is wedged (nothing can find the board)

**Symptom**: the app says it can't find `TreadmillController`; a scan from
*any* client sees **no BLE devices at all**, even with the board known to be
advertising (check with a phone's BLE scanner app). `bluetoothctl info
<mac>` shows no RSSI, and `hcitool con` may show a connection stuck forever
at `state 5` (still connecting; a healthy link is `state 1`).

**How it differs from #1/#2**: nothing holds a connection to the board and
there's no stale bond -- the host simply can't scan. The giveaway is that
BlueZ's discovery state machine is jammed:

```bash
bluetoothctl scan off    # -> Failed to stop discovery: org.bluez.Error.Failed
bluetoothctl scan on     # -> Failed to start discovery: org.bluez.Error.InProgress
dmesg | grep hci         # -> "command 0x2005 tx timeout" / "Opcode 0x2041 failed: -110"
```

(`0x2005`/`0x2041` are LE controller commands; the timeouts mean the USB
adapter stopped answering them.) Seen on the Realtek USB dongle (`0bda:8771`)
after days of uptime.

**Don't be fooled by classic devices**: `bluetoothctl scan on` also does a
classic (BR/EDR) *inquiry*, so it can list plenty of neighbouring devices
(speakers, headsets) while LE scanning is completely dead. Judge by an LE
scan (`bleak`'s `BleakScanner.discover()`), not by a long list in
`bluetoothctl`.

**What did NOT fix it**: `bluetoothctl power off/on` (power off itself
fails), `rfkill block/unblock bluetooth`. **Re-enumerating the dongle at the
USB level** is the obvious next step -- physically unplug/replug it, or in
software -- **but on 2026-10-12 it did NOT fix it either**: after a clean
replug (even into a different port) the same `0x2005`/`0x2041` timeouts hit
~9s after init, and afterwards `bluetoothctl scan le` and `bleak` both
reported "Discovery started" with no kernel errors yet saw **zero** BLE
devices (a paired iPhone nearby should alone produce some). So treat LE
scanning as dead on that adapter until proven otherwise. Unconfirmed
suspects: the dongle's LE side itself failing, or BlueZ's first post-init LE
setup (random address / extended scan params, as used for background
scanning for the paired+trusted devices -- an iPhone with an IRK, a
WH-1000XM4) being something this Realtek firmware stops answering; the
trigger isn't pinned down. Cheapest next experiments: try a different
Bluetooth adapter; or remove the iPhone pairing (`bluetoothctl remove
<mac>`, re-pairable) and replug. The software replug, for reference:

```bash
readlink -f /sys/class/bluetooth/hci*      # find the USB path, e.g. .../7-1.2.4/...
sudo sh -c 'echo 0 > /sys/bus/usb/devices/7-1.2.4/authorized; sleep 2; echo 1 > /sys/bus/usb/devices/7-1.2.4/authorized'
```

(needs root; an unprivileged session can only do the rfkill/`power` things
above, which don't help). Not fixed permanently -- a more robust adapter, or
a USB power/autosuspend change, might; the trigger isn't pinned down.

## Working diagnostic checklist

When "can't connect" comes up, fastest path to a diagnosis, in order:

```bash
bluetoothctl info <mac> | grep -i connected   # stale BlueZ-side "Connected: yes"?
hcitool con                                   # a REAL live HCI connection (not just stale metadata)?
ps aux | grep blueman-manager                 # is it running and holding it?
```

- `Connected: yes` + a real `hcitool con` entry + `blueman-manager` running
  -> almost certainly cause #1. `bluetoothctl disconnect <mac>`.
- Connects, then `BleakError: failed to discover services, device
  disconnected` -> almost certainly cause #2. `bluetoothctl remove <mac>`,
  reconnect unbonded.
- Board never found, and an LE scan sees *nothing at all* (not even other
  BLE devices) with `scan off`/`scan on` failing as above -> cause #3.
  Re-enumerate the USB adapter. (If an LE scan sees other devices but not the
  board, the board really isn't advertising: check its power.)

## Self-healing for cause #1 (`tools/treadmill_app.py` only)

`BLEWorker._connect()` now runs `_clear_stale_connection()` before every
scan: it looks up the device's MAC via `bluetoothctl devices`, checks
`bluetoothctl info <mac>` for `Connected: yes`, and if so runs
`bluetoothctl disconnect <mac>` -- exactly the manual fix above, just run
automatically on every connect/reconnect instead of requiring a human to
notice and run it. This has to happen *before* scanning, not in response to
a failed one, since a stale OS-side connection means the board isn't
advertising and the scan would simply never find it. It's a no-op (and
silent) when nothing's stuck, and silently skips itself if `bluetoothctl`
isn't on PATH -- best-effort, not a hard requirement to run the app.

This only covers cause #1. Cause #2 (stale bonds) isn't self-healed --
`bluetoothctl remove <mac>` is destructive (drops the bond entirely) in a
way a background reconnect shouldn't do unprompted, so that one still needs
a human to recognize the `BleakError: failed to discover services`
symptom and run the fix above. `controller_ui.py` also doesn't get this
fix -- it stays deliberately unmodified (see its own header comment).

## Not yet done

- No permanent fix for cause #1 that doesn't trade away the Bluetooth
  Devices GUI (masking `blueman-manager.service`) -- the self-heal above
  avoids needing that tradeoff for `treadmill_app.py`, but blueman is still
  free to grab the connection first each time; the app just clears it
  automatically now instead of failing.
- No fix, automatic or otherwise, that drops bonding entirely as a
  permanent state (removing the bond each time it goes stale, for cause
  #2, is still a per-incident manual step).
- Root cause of *why* the bond goes stale (not just that it does) isn't
  pinned down -- worth a closer look if this keeps recurring, since knowing
  the trigger might turn "remove and reconnect" into an actual fix instead
  of a recurring workaround.
