# GATT protocol

The GATT service speaks **VE.Smart**, the protocol of VictronConnect. It was taken from VictronConnect 6.43 for macOS (the classes `VeSmartService` and `VeService`, their log messages and the decompiled code) and checked on a SmartSolar 75/15 with firmware 1.64 (`dev-vhftest-03`). The code is `vesmart.py` (transport and messages) and `gatt_session.py` (the session). See [unknowns.md](unknowns.md) for what is still open.

## Service

Service `306b0001-b081-4037-83dc-e59fcc3cdfd0` with three characteristics. The code calls them by the last digits of their handles:

| Name | UUID | VictronConnect | Use |
|---|---|---|---|
| `0021` | `306b0002-...` | control | transport parameters (read) and flow control (write, notify) |
| `0024` | `306b0003-...` | lastData | the last piece of a chunk |
| `0027` | `306b0004-...` | data | the other pieces of a chunk |

Notifications are enabled on all three, in this order. Writes are "write without response".

## Transport

Both sides send **chunks**: one or more messages, at most `chunkSize` bytes, written (or notified) in pieces of at most `maxAttLength` bytes. All pieces but the last go to data, the last to lastData; a notification on lastData ends the chunk, and the receiver parses it.

**Parameters.** After the notifications are enabled the client reads the control characteristic (little endian):

| Bytes | Field | SmartSolar 75/15 |
|---|---|---|
| u8 | flags | 0 |
| u16 | maxInstances | 4 |
| u8 | maxFreeChunks: chunks the device takes before it allows more | 1 |
| u8 | chunkSize | 222 |
| u16, optional | maxAttLength (20 if missing or smaller) | 74 |

Then the client writes `FA 80 FF` to control (constant in VictronConnect) and allows the device its first chunks.

**Flow control.** Opcodes on control:

| Bytes | Direction | Meaning |
|---|---|---|
| `F9 n` | both | ReadyToReceive: the other side may send `n` more chunks |
| `F8` | device | drop the partly received chunk |
| `F7 e` | device | error `e` (one or two bytes) |

* The device allows us `maxFreeChunks` chunks at a time, 1 on the SmartSolar: a chunk may only be written after its `F9 01` for the previous one. The device answers every chunk with `F9 01` before its answers. VictronConnect queues messages while it has no credit and gives up after 10 s without any.
* The client allows the device `0x80` chunks at the start (`F9 80`) and gives back what it received once that is more than `0x40` (the `F9 41` in VictronConnect traces). **The device keeps this credit in one byte:** giving the same chunks back twice made it overflow, and the device stopped sending (and with it the keepalive echo, after which it closed the link).

Writing without credit, chunks larger than `chunkSize`, and not reading the parameters is what the old replay of a VictronConnect trace did: the device then sent hundreds of empty notifications per second on data and closed the link after 10 s, before any value arrived.

## Messages

A message is a sequence of CBOR items: an opcode, then its arguments. Several messages can share a chunk.

| Opcode | Direction | Arguments |
|---|---|---|
| `1` GetDevices | client | |
| `2` DeviceList | device | `[instance, parent, instance, parent, ...]` |
| `3` Subscribe | client | instance: the device then pushes the values of the instance when they change, about once a second |
| `5` GetValues | client | instance, `[vreg, ...]` |
| `6` SetValues | client | instance, `[vreg, bytes]` |
| `7` Response | device | instance, opcode, result (0: success; answers Subscribe) |
| `8` Value | device | instance, vreg, bytes |
| `9` ValueResponse | device | instance, vreg, result (1: not available) |

`05 03 82 19 0100 19 0102` reads registers `0x0100` and `0x0102` of instance 3. A value is a CBOR byte string in the register's little-endian encoding (`42 3305` is 2 bytes, 13.31 V). VictronConnect knows more opcodes for its "path" interface (other products); an unknown opcode makes the receiver skip the rest of the chunk.

**Instances.** The device is a tree of instances. The SmartSolar 75/15 answers `[0, 0, 1, 0, 3, 1]`:

| Instance | Parent | Product id (`0x0100`) | Holds |
|---|---|---|---|
| 0 | 0 (root) | `0xA197` | the Bluetooth part: key `0xEC65`, MAC `0xEC66`, keepalive `0x93` |
| 1 | 0 | `0xFE00` | product id only |
| 3 | 1 | `0xA053` | the charger: all VE.Direct registers |

The session takes the instance whose product id is the one of the advertisement, else the deepest one. The old code called the instance a "context" and always used 3.

## Keepalive

`06 00 82 18 93 42 10 27` sets `VE_REG_CONNECTION_KEEP_ALIVE` (`0x93`, instance 0) to 10000 ms: the device closes the link 10 s after the last one. The device echoes it as a Value; VictronConnect starts its keepalive timer (3.333 s) with that echo, and stops it if the device answers with an error. The daemon sends it every `gatt.keepalive_s` (3 s).

## Session

1. Connect (needs the Wi-Fi access point down, see [quirks.md](quirks.md)).
2. Pair with the passkey (default `000000`). The pairing agent must be registered *before* the connection is opened; registering it only around `pair()` fails with `AuthenticationFailed`.
3. Enable notifications on control, lastData and data.
4. Read the parameters, write `FA 80 FF` and `F9 80`.
5. GetDevices; Subscribe to every instance; the keepalive.
6. Read `0x0100` and `0x0102` of every instance to find the charger, and the MAC of instance 0.
7. The device pushes the values that change (on the SmartSolar: panel voltage and power, channel 1 voltage and current, load output, ...); the daemon reads the rest with GetValues.

A 10-minute run on the SmartSolar stayed connected with about 12 values a second, until a bug in the test client gave chunks back twice (see Transport).

## Registers

The register ids and encodings are those of Victron's VE.Direct HEX protocol (rev 18) and are the same over the cable. VictronConnect carries Victron's register dictionary (1733 registers with name, type, scale and description, e.g. `0xEDBC VE_REG_DC_INPUT_POWER un32 [0.01 W]`); the table in `vreg.py` (`VREGS`, `WRITABLE`) was checked against it. `smartsolar vregs` lists them with a `verified` flag (seen with plausible values on the device).

`0xEC5A`, published as `uptime`, is `VE_REG_TREND_LAST_TIME_REFERENCE` in the dictionary: a time reference of the trend data that counts seconds, not the uptime. Other registers the SmartSolar pushes that are not in the table (so only in `raw_registers`): `0xED8C` (`VE_REG_DC_CHANNEL1_CURRENT_MA`, sn32 mA), `0xEC89`/`0xEC8A` (output current and input power for trends), `0x0202` (`VE_REG_REMOTE_CONTROL_USED`), `0x0090` (`VE_REG_BLE_MODE`), `0x0150` (`VE_REG_CAPABILITIES_BLE`) and the history records `0x104F` (total) and `0x1050` (today).

Special ones:

| Register | Content |
|---|---|
| `0xEC65` | the Instant Readout key, 16 bytes (never published or logged) |
| `0xEC66` | the MAC address |
| `0x0100` | product id: byte 0 an identifier, bytes 1-2 the id (little endian), byte 3 flags |
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
