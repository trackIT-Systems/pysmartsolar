# VE.Direct cable (serial)

19200 baud, 8N1, through an FTDI cable (`0403:6001`, `/dev/ttyUSBvd<n>` on tsOS). Two protocols share the line.

## Text blocks

Pushed by the charger about once a second:

```
\r\nPID\t0xA053\r\nFW\t164\r\nSER#\tHQ22294ZGEU\r\nV\t13320\r\n ... \r\nChecksum\t<1 byte>
```

The sum of all bytes of the block, including the leading `\r\n` and the checksum byte, is 0 modulo 256.

| Key | Meaning | Unit / encoding |
|---|---|---|
| PID | product id | hex, `0xA053` |
| FW | firmware | `164` = 1.64 |
| SER# | serial number | string |
| V | battery voltage | mV |
| I | battery current | mA, signed |
| VPV | panel voltage | mV |
| PPV | PV power | W |
| CS | state | number, same table as BLE (0 off, 3 bulk, 4 absorption, 5 float, ...) |
| MPPT | tracker | 0 off, 1 limited, 2 tracking |
| OR | off reason | bit mask, e.g. `0x00000001` no input power |
| ERR | error | number, same table as BLE |
| LOAD | load output | `ON` / `OFF` |
| IL | load current | mA |
| H19 | yield total | 0.01 kWh |
| H20 | yield today | 0.01 kWh |
| H21 | maximum power today | W |
| H22 / H23 | yield / maximum power yesterday | 0.01 kWh / W |
| HSDS | day sequence number | 0..364 |

Other models may send more fields (relay, alarm, ...); the daemon keeps unknown ones and shows them in `status`.

## HEX frames

```
: command-nibble  payload (hex bytes)  checksum (1 byte as 2 hex digits)  \n
```

Command nibble + all payload bytes + checksum = `0x55` (mod 256). Commands seen: `7` get, `8` set, `A` async, `1` done, `3` get application version (answered with `1` + version), `4` get product id (answered with `1` + `53A0`), `5` ping reply (the ping is command `1`). Get, set and async frames carry `register (u16 LE), flags (u8), value (little endian)`. Flags: `0x01` unknown id, `0x02` not supported, `0x04` parameter error.

The registers are the same as over GATT (ids, widths, scales), so the writing code is shared. Exceptions on this device:

* Register `0x0102` (firmware) is an unknown id over HEX. The firmware comes from command `3` (`:1` + `6441` = `0x4164`: the top nibble tells release from beta, the lower 12 bits `0x164` are the version).
* Registers `0x0101` and `0x0103` are unknown ids. `0x010A` is the serial number as a string, `0x010B` the description ("SmartSolar Charger MPPT 75/15").

## Quirks (all observed on the real device)

* **NUL padding.** The device inserts `\x00` bytes into the output, inside text lines (`\x00PPV`, `H19\t\x009847`) and inside HEX frames. They add 0 to the checksum and have to be skipped. A parser that cuts values at the first NUL loses fields.
* **`:` is not always a frame start.** The byte after `Checksum\t` can be `:` (0x3A). It is the checksum, not a HEX frame.
* **HEX frames inside a text block.** Replies and async frames appear between the lines of a text block; they do not count for the text checksum.
* **Requests cost a block.** A request while the device writes a text block makes it stop the block, send the reply and start the block again. That one block is lost (its checksum fails or it is incomplete). Therefore HEX is only used on request.
* **A garbled block after bursts.** After a burst of HEX frames the next block is occasionally damaged: 1 bad block in 69 over 70 s without any request of ours. Only blocks with a valid checksum are used.
* **Async frames.** The device pushes `:A` frames (about 118 in 70 s) for many registers (`0x0200`, `0x0201`, `0xEDD5`, `0xEDBB`, ...) without being asked. The first request on a quiet line sees them before its answer, so answers are matched by command and register.
* **Ping and app version.** `:154\n` (ping) is answered with `:5` + `6441`; the same `6441` as the application version.
* **One reader.** Two programs on the same port take bytes away from each other. The daemon opens the port exclusively.
* **Unplugging.** A removed USB adapter shows as "device reports readiness to read but returned no data"; the port name disappears until the device comes back.

## Safety

The VE.Direct TX and RX port functions (`0xED9E`, `0xED98`) decide what the port does. A value other than "communication" can make the port unusable for HEX. The daemon never writes them over the cable.
