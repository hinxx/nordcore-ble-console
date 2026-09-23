# BLE connection reliability on this Linux host — known fragile, partially self-healed

**Status: cause #1 self-heals automatically in `treadmill_app.py` (see
below); cause #2 and blueman-manager itself are still open, not root-caused
to a permanent fix.** Separate from
`fw/controller/BLE_NOTIFY_LATENCY_INVESTIGATION.md` (which is about telemetry
*timing* once connected, and is resolved) -- this is about the connection
itself frequently failing to establish at all, or refusing a reconnect,
purely on the Linux/BlueZ host side. Confirmed the board's own BLE stack is
not at fault every time this has come up.

Two distinct causes found so far, both host-side, both requiring manual
intervention when they happen:

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
