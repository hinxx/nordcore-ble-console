# LOG 1 (log1.txt)

This first log is actually very good. The ESP32 is clearly receiving both directions correctly, and it confirms the framing we inferred from the scope.

The startup confirms the expected setup: ESP-IDF 5.5.5, GPIO26 and GPIO27 configured as `1200 8N2`, TX disabled. 

The dominant packets are exactly these:

```text
GPIO27:
68 0C A0 00 00 00 00 9F 00 00 00 00 4B 43

GPIO26:
68 08 20 00 00 00 00 14 3C 43
```

You can see both cleanly very early in the capture. 

I parsed the whole log. The results are:

```text
GPIO27:
53 complete frames found

48 × 68 0C A0 00 00 00 00 9F 00 00 00 00 4B 43
 4 × 68 0C A0 00 00 00 00 A0 00 00 00 00 4C 43
 1 × 68 0C A0 00 00 00 00 9F 00 01 00 00 4C 43


GPIO26:
66 complete frames found

66 × 68 08 20 00 00 00 00 14 3C 43
```

So **GPIO26 is completely invariant in this capture**. Every valid frame is identical.

GPIO27 has three variants, which is interesting. One of the changes is:

```text
... 9F ... 4B ...
          ↓
... A0 ... 4C ...
```

and another is:

```text
... 9F 00 00 00 00 4B ...
             ↓
... 9F 00 01 00 00 4C ...
```

That strongly suggests the penultimate byte really is some kind of **checksum/check byte**, because when one payload byte increments by 1, the penultimate byte also increments by 1.

For example, this variant appears in the log:

```text
68 0C A0 00 00 00 00 A0 00 00 00 00 4C 43
```

instead of the usual:

```text
68 0C A0 00 00 00 00 9F 00 00 00 00 4B 43
```

You can see one of those transitions around lines 237–250. 

That is very useful evidence.

There is also a very regular timing pattern on GPIO27: frame starts are almost exactly **220.0 ms apart** for essentially the entire capture. So GPIO27 looks like a periodic status/heartbeat source.

GPIO26 behaves differently. It sometimes emits isolated frames, but at other times sends bursts of the same frame about **20.8 ms apart**. That likely corresponds to some console activity or repeated command transmission.

One important implementation issue shows up in the timestamps. Within some UART packets, the first few bytes are stamped only ~100 µs apart, even though at 1200 8N2 a physical byte occupies:

```text
11 / 1200 ≈ 9.17 ms
```

For example:

```text
224200 GPIO26 68
224316 GPIO26 08
224425 GPIO26 20
224533 GPIO26 00
...
```

Those timestamps clearly are **not wire-arrival timestamps**; they're when the task drains bytes already buffered by the UART driver. 

Then later in the same frame, the reads spread out closer to ~2 ms increments, which just reflects scheduling/driver buffering behavior. So the timestamp field is useful for packet/burst timing, but **not for bit or byte timing**.

That is completely fine for the current purpose.

Also encouraging: I see **no UART framing, parity, FIFO-overflow, or buffer-overflow errors** in the log. The only `ERROR` text is the header describing the output format. So electrically, the divider + ESP32 UART setup appears clean.

The protocol framing now looks quite solid:

```text
68 LEN TYPE/PAYLOAD ... CHECK? 43
```

with:

```text
0x68 = start
LEN  = number of bytes after LEN
0x43 = end marker
```

and very likely:

```text
second-to-last byte = checksum
```

The next thing I would do is no longer simply “idle capture.” Capture separate logs where you perform exactly one action at a known point:

```text
idle
speed +
speed -
start
stop
incline +
incline -
```

Ideally one action per log. Then we can diff complete packets and start assigning semantics to each byte.

This first capture says the sniffer itself is working correctly.


## The data directions

```text
GPIO27 = HC32L130 RX
       = baseboard -> console/L130

GPIO26 = HC32L130 TX
       = console/L130 -> baseboard
```

That means the dominant idle traffic is:

```text
Baseboard -> console:
68 0C A0 00 00 00 00 9F 00 00 00 00 4B 43

Console -> baseboard:
68 08 20 00 00 00 00 14 3C 43
```

So the **GPIO27 packet is the periodic baseboard status/heartbeat**, while the **GPIO26 packet is the console-originated message/command/acknowledgement**.

That mapping is important for all further protocol analysis. I’ll use:

```text
GPIO27 = BASE -> L130
GPIO26 = L130 -> BASE
```

from now on.


