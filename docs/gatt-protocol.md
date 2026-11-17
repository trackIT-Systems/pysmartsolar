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
| u8 | flags: bit 0, the device has the path interface (see Messages) | 0 |
| u16 | maxInstances | 4 |
| u8 | maxFreeChunks: chunks the device takes before it allows more | 1 |
| u8 | chunkSize | 222 |
| u16, optional | maxAttLength (20 if missing or smaller) | 74 |

Then the client writes `FA 80 FF` to control and allows the device its first chunks (`F9 80`). `FA` is the control opcode after `F7`..`F9`; VictronConnect writes the three bytes as a constant and does not name them.

VictronConnect also does two things the SmartSolar does not trigger:
* If `maxAttLength` is more than 29, equals `chunkSize` and `maxFreeChunks` is more than 1, it queues `06 00 82 00 58 <maxAttLength-6> 00…`: a SetValues of register 0 (`VE_REG_INVALID`) on instance 0 whose zero bytes make the message exactly one full ATT write long, a padding message that exercises the full packet size.
* For product `0xC021` (a GlobalLink) with firmware below 2.05.01 it lowers `chunkSize` to `maxAttLength` ("faulty GlobalLink firmware").

**Flow control.** Opcodes on control:

| Bytes | Direction | Meaning |
|---|---|---|
| `F9 n` | both | ReadyToReceive: the other side may send `n` more chunks |
| `F8` | device | drop the partly received chunk |
| `F7 e` | device | error `e`: a signed byte, or a signed 16-bit little-endian number if longer. VictronConnect only logs it, except `3`, on which it drops the link to every instance |

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
| `4` Unsubscribe | client | instance: answered with Response, the pushes stop |
| `5` GetValues | client | instance, `[vreg, ...]` |
| `6` SetValues | client | instance, `[vreg, bytes]` |
| `7` Response | device | instance, opcode, result (0: success; answers Subscribe and Unsubscribe) |
| `8` Value | device | instance, vreg, bytes |
| `9` ValueResponse | device | instance, vreg, result: a get or set failed (below) |
| `10` GetPathList, `11` GetPathValues, `12` SetPathValue | client | the path interface |
| `13` PathList, `14` NewPath, `15` PathValue, `16` PathValueResponse | device | the path interface |

`05 03 82 19 0100 19 0102` reads registers `0x0100` and `0x0102` of instance 3. A value is a CBOR byte string in the register's little-endian encoding (`42 3305` is 2 bytes, 13.31 V). An unknown opcode makes the receiver skip the rest of the chunk.

**Path interface.** A device with bit 0 of `flags` is asked for its paths after a successful Subscribe (GetPathList); VictronConnect then addresses its values by path index instead of register (opcodes 10 to 16). The SmartSolar has `flags` 0 and is read by register.

**Results of ValueResponse** (SmartSolar 75/15, firmware 1.74; VictronConnect passes them on without interpreting them):

| Result | When | Over HEX |
|---|---|---|
| `1` | unknown register, read or write | flag `0x01` |
| `3` | access denied: a write to a read-only register (`0x0100`), a read of a write-only one (test mode `0x0020`, the settings lock `0x01A0`, `0x01A3`) | flag `0x02` for the reads |
| `-5` | index out of range: trend slots 6 and 7 of a device with 6 trends (`0xEC50`, `0xEC51`, `0xEC58`, `0xEC59`) | flag `0x01` |

A write of a value the device does not take (rather than of a register it does not let write) has not been tried; the daemon still treats no answer within the timeout as a refusal.

**Instances.** The device is a tree of instances. The SmartSolar 75/15 answers `[0, 0, 1, 0, 3, 1]`:

| Instance | Parent | Product id (`0x0100`) | Holds |
|---|---|---|---|
| 0 | 0 (root) | `0xA197` | the Bluetooth part: key `0xEC65`, MAC `0xEC66`, keepalive `0x93` |
| 1 | 0 | `0xFE00` | named `nRF5x-UART0` (`0x010C`): the trend configuration and time references (`0xEC4A`..`0xEC5F`), the battery temperature for trends (`0xEC88`) |
| 3 | 1 | `0xA053` | the charger: all VE.Direct registers |

The session takes the instance whose product id is the one of the advertisement, else the deepest one. The old code called the instance a "context" and always used 3.

## Keepalive

`06 00 82 18 93 42 10 27` sets `VE_REG_CONNECTION_KEEP_ALIVE` (`0x93`, instance 0) to 10000 ms: the device closes the link 10 s after the last one. A link that never got a keepalive stays open: one without it ran 63 s with values pushed throughout. The device echoes it as a Value; VictronConnect starts its keepalive timer (3.333 s) with that echo, and stops it if the device answers with an error. The daemon sends it every `gatt.keepalive_s` (3 s).

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

`0xEC5A` is `VE_REG_TREND_LAST_TIME_REFERENCE` in the dictionary: a time reference of the trend data that counts seconds, published as `trend_time_reference`. It was published as `uptime` before; the uptime is `0x0120` (`VE_REG_UPTIME`, 19 days on a charger whose `0xEC5A` said 136). Other registers the SmartSolar pushes that are not in the table (so only in `raw_registers`): `0xED8C` (`VE_REG_DC_CHANNEL1_CURRENT_MA`, sn32 mA), `0xEC89`/`0xEC8A` (output current and input power for trends), `0x0202` (`VE_REG_REMOTE_CONTROL_USED`), `0x0090` (`VE_REG_BLE_MODE`), `0x0150` (`VE_REG_CAPABILITIES_BLE`) and the history records `0x104F` (total) and `0x1050` (today).

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
