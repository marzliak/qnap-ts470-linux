# ICP A125 LCD serial protocol

Wire-level reference for the front-panel display fitted to QNAP TS-x70 chassis.

Everything here was observed on the **reference TS-470 Pro** running Linux.
Each statement is tagged:

- **Verified** — reproduced directly on the reference unit.
- **Inferred** — consistent with observation and with related public drivers,
  but not independently confirmed. Treat as a starting point, not as fact.

Other TS-x70 models are unverified; see [COMPATIBILITY.md](COMPATIBILITY.md).

---

## 1. Physical layer

| Property | Value | Status |
|---|---|---|
| Device | `/dev/ttyS1` on the reference unit | Verified |
| Baud rate | 1200 | Verified |
| Frame | 8 data bits, no parity, 1 stop bit (8N1) | Verified |
| Flow control | none | Verified |
| Direction | full duplex — the panel sends button events on the same port | Verified |
| Display | 2 rows × 16 characters | Verified |
| Character set | 7-bit ASCII | Inferred |

The port is an ordinary 8250-family UART exposed by the board, not a USB
adapter. On the reference unit the device node is owned by `root:dialout` with
mode `0660`.

Do not assume the node is always `/dev/ttyS1`. Enumeration depends on how many
UARTs the kernel finds, which varies with the board and the kernel version. The
path is configurable — see [LCD_GUIDE.md](LCD_GUIDE.md#8-configuration).

### Raw setup

```bash
stty -F /dev/ttyS1 1200 cs8 -cstopb -parenb -echo raw
```

---

## 2. Command format

Every host-to-panel command begins with the prefix byte `0x4D` (ASCII `M`).

| Command | Bytes | Meaning | Status |
|---|---|---|---|
| Backlight on | `4D 5E 01` | Switch the backlight on | Verified |
| Backlight off | `4D 5E 00` | Switch the backlight off | Verified |
| Write row 0 | `4D 0C 00 10` + 16 bytes | Replace the top row | Verified |
| Write row 1 | `4D 0C 01 10` + 16 bytes | Replace the bottom row | Verified |
| Write both rows | `4D 0C 00 20` + 32 bytes | Replace both rows | Verified, see §3 |
| Clear | `4D 28` | Clear the display content | Verified |
| Stop built-in clock | `4D 0D` | Stop the panel's own clock display | Verified |

Byte 3 of a write command is the row index, byte 4 is the payload length in
bytes. The payload is **not** NUL-terminated and is **not** length-prefixed
beyond that byte: the panel consumes exactly the number of bytes announced.

### Payload rules

- The payload must be **exactly** the announced length. Short payloads leave
  the panel waiting for the remaining bytes and the next command is consumed
  as text.
- Pad with spaces (`0x20`) to reach 16 bytes; truncate anything longer.
- Only 7-bit ASCII is known to render correctly. This project replaces
  anything else with `?` before transmitting, so one source character always
  costs exactly one column.

---

## 3. The 32-byte combined write

`4D 0C 00 20` followed by 32 bytes writes both rows at once, but on the
reference unit this **only works reliably for the first write after the panel
powers up**. Subsequent combined writes produce partial or misaligned text.

Consequence for implementers: after the first write, always send two separate
16-byte row writes. This project does that unconditionally, which costs one
extra command and avoids the failure mode entirely.

Status: **Verified** that repeated combined writes misbehave. The underlying
reason is **inferred** — most likely an internal buffer on the panel's
controller that is only reset on power-up.

---

## 4. Timing

At 1200 baud with 8N1 framing, each byte occupies 10 bit-times:

| Quantity | Value |
|---|---|
| Bit time | 1 / 1200 s ≈ 0.833 ms |
| Byte time | ≈ 8.33 ms |
| One row write (4 + 16 = 20 bytes) | ≈ 167 ms |
| Both rows as two writes | ≈ 333 ms |
| Recommended delay after a row write | **180 ms** |
| Recommended delay after a backlight command | 50 ms |

The 180 ms figure is the transmission time plus a small margin. Writing faster
than the wire can carry produces corrupted or interleaved text — this is the
single most common cause of garbled output.

**Practical consequence:** character-by-character scrolling is not viable. A
full-width scroll would need one 167 ms write per frame, so a 16-column marquee
costs about 2.7 s per pass. Use page rotation instead, with a page interval of
at least 0.5 s and realistically 3–5 s.

Status: byte timing is arithmetic from the line rate. The 180 ms minimum is
**Verified** empirically on the reference unit.

---

## 5. Button events

The panel sends a 4-byte frame on the same port when a front-panel button
changes state:

```
0x53 0x05 0x00 <bitmask>
```

| Bitmask | Meaning | Status |
|---|---|---|
| `0x01` | ENTER pressed | Verified |
| `0x02` | SELECT pressed | Verified |
| `0x03` | both pressed | Verified |
| `0x00` | all released | Verified |

### Parsing constraints

A `read()` on the port returns whatever bytes have arrived, which means a
parser must handle all of:

- a complete frame;
- **several frames in one read** — holding a button produces repeats;
- **a frame split across two reads** — a 4-byte frame at 1200 baud takes about
  33 ms to arrive, so a poll can easily land in the middle of one;
- bytes that are not part of any frame.

A parser that scans a single read buffer and discards the remainder loses
presses whenever a frame straddles a read boundary. Keep unconsumed bytes in a
buffer between reads and re-scan. This project implements that in
`ButtonParser`, and `tests/test_lcd_protocol.py` covers the split, multiple and
noise cases.

Release events (`0x00`) are delivered like any other frame. Treat them as
"no button" rather than as a press, otherwise every press registers twice.

### The USB Copy button

The front-panel USB Copy button does **not** appear on this serial port and is
not exposed through the Linux input subsystem on the reference unit. It is
**inferred** to be wired to the IT8528E embedded controller. See the
[references](#7-references) for projects working on that controller.

---

## 6. Known limits

- **No brightness or contrast control.** Commands `0x5B`, `0x5C`, `0x5D` and
  `0x5F` were tried with a range of values on the reference unit; none changed
  brightness. Only backlight on/off is available. A dim panel is normally an
  aged backlight, not a software setting. (Verified.)
- **No cursor addressing.** Writes replace a whole row.
- **No readback.** The panel does not report its current contents.
- **No model identification.** There is no command that returns a panel or
  firmware version, so software cannot detect the panel type — which is exactly
  why this project requires a manual protocol test before installing anything.

---

## 7. References

- [QNAP TS-453 Pro LCD, LEDs, fan and buttons (gist)](https://gist.github.com/zopieux/0b38fe1c3cd49039c98d5612ca84a045)
- [LCDproc `icp_a106` driver source](https://github.com/lcdproc/lcdproc/blob/master/server/drivers/icp_a106.c)
- [qnap8528 kernel module](https://github.com/0xGiddi/qnap8528)
- [QNAP-EC](https://github.com/Stonyx/QNAP-EC)
- [Unraid LCD_Manager plugin](https://forums.unraid.net/topic/136952-plugin-lcd_manager/)
- [Unraid QNAP-EC plugin](https://github.com/ich777/unraid-qnapec)

The A106 driver in LCDproc targets a related ICP panel and uses the same
`0x4D` command prefix. It is the closest public prior art to this protocol and
was useful for cross-checking command bytes, but it is not the same panel and
its command set does not map one-to-one.
