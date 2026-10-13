# GATT protocol

Reverse engineered from Bluetooth traces of VictronConnect and from tests on a SmartSolar 75/15. See [unknowns.md](unknowns.md) for what is replayed without being understood.

## Service

Service `306b0001-b081-4037-83dc-e59fcc3cdfd0` with three characteristics, called by the last digits of their short id:

| Name | UUID | Use |
|---|---|---|
| `0021` | `306b0002-...` | control; the device answers its writes with `f901` |
| `0024` | `306b0003-...` | requests and answers |
| `0027` | `306b0004-...` | requests and answers (a second channel, same format) |

Notifications are enabled on all three. Writes are "write without response".

## Records

Everything the device sends on `0024` and `0027` is a byte stream of records. A record can be split across notifications, and several records can share one notification (the parser keeps incomplete data for the next chunk).

```
[kind] [ctx] [CBOR register id] [CBOR value]      kind 0x07, 0x08, 0x09
[kind] [CBOR item]                                kind 0x02 (handshake answer)
```

* `08`: a value, a CBOR byte string in the register's little-endian encoding.
* `09`: the device refuses the register; the value is an error code (`1` seen).
* `07`: status record; parsed, not interpreted.
* `02`: answer to the handshake; parsed, not interpreted.
* An unknown kind makes the rest of the chunk unparsable (the length is not known): it is dropped and counted.

## Requests

```
[0x05] [ctx] [CBOR array of register ids]            get
[0x06] [ctx] [CBOR array [register id, byte string]] set
```

`05 03 82 19 0100 19 0102` reads registers `0x0100` and `0x0102` in context 3.

* **Context 3 is where reads and writes work.** Contexts 0 and 1 only answer a few static registers (see [unknowns.md](unknowns.md)).
* A get gets one `08` record per register (or `09` if the register is not available on this model or firmware). Registers are read one after the other with a timeout per register; the answer to a get arrives as a record in the stream, not as a reply tied to the request.
* A set is answered by an `08` record that echoes the stored value. A write the device refuses (a read-only register, an out-of-range value in some cases) gets **no answer at all**, so a missing echo is the only sign of a refused write. The daemon therefore times out, reports "the device did not confirm the write" and always reads the register back.

## Keepalive

`06 00 82 18 93 42 10 27` sets register `0x93` to `0x2710` (10000). The device closes the link about 10 s after the last such ping (measured). The daemon sends it every `gatt.keepalive_s` (5 s) and when connecting.

## Connecting

1. Connect (needs the Wi-Fi access point down, see [quirks.md](quirks.md)).
2. Pair with the passkey (default `000000`). The pairing agent must be registered *before* the connection is opened; registering it only around `pair()` fails with `AuthenticationFailed`.
3. Enable notifications on the three characteristics.
4. Send the init sequence (`INIT_SEQUENCE` in `vreg.py`), a replay of the VictronConnect trace: handshake bytes, the ping, requests for the key and the MAC, and the subscriptions.
5. After `f941` on the control characteristic the device pushes its values.

## Registers

The register ids and encodings are those of Victron's VE.Direct HEX protocol (rev 18) and are the same over the cable. The table is in `vreg.py` (`VREGS`, `WRITABLE`); `smartsolar vregs` lists them with a `verified` flag (seen with plausible values on the device).

Special ones:

| Register | Content |
|---|---|
| `0xEC65` | the Instant Readout key, 16 bytes (never published or logged) |
| `0xEC66` | the MAC address |
| `0x0100` | product id: byte 0 instance, bytes 1-2 id (little endian) |
| `0x0102` | firmware: bytes 1-2 little endian, `0x0164` = 1.64 |
| `0xEDEC` | battery temperature in 0.01 K, `0xFFFF` = no sensor |
| `0x2211` / `0x2212` | the allowed range of charge voltages (0.01 V): 8.00 .. 17.40 V on a 12 V system |
| `0xEDEF` | the battery system voltage (12 on this device) |
| `0xEDF1` | battery type; `0xFF` = user (own settings) |

## Writing

* The charge voltages are only taken when the battery type is `user` (`0xFF`). With a preset the device accepts the write and ignores it, so the daemon refuses it first.
* A voltage outside the range reported in `0x2211`/`0x2212`, or a temperature compensation beyond 21 mV/K (12 V system), puts the charger into **error 119** (invalid settings). Only a factory reset followed by a power cycle clears it. The daemon reads the range first and refuses such values.
* Settings are in non-volatile memory: do not write in loops.
* The device mode (`0x0200`) is only honoured when the remote on/off bit is set in `0x0202` (not tested).
* Load output algorithm (`0xEDAB`): `0` off, `1` BatteryLife, `2` conventional 1 (off below 11.1 V, on above 13.1 V), `3` conventional 2 (11.8 V / 14.0 V), `4` on, `5` user 1, `6` user 2, `7` AES. Only the low 4 bits are used. A change takes effect after about 2 minutes.

## Where this was checked

All 22 settings the `.vcsf` import maps were read back from the device and matched the VictronConnect file ([vcsf-format.md](vcsf-format.md)); a write of 7 settings and the restore worked over GATT and over the cable.
