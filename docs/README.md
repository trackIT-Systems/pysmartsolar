# Documentation

Technical notes on how the SmartSolar speaks, what was measured on a real device and what is still unknown. The code is the reference for behaviour; these pages are the reference for *why*.

| Page | Content |
|---|---|
| [instant-readout.md](instant-readout.md) | The encrypted advertisements: layout, decryption, sentinels |
| [gatt-protocol.md](gatt-protocol.md) | The GATT service: framing, requests, registers, pairing, keepalive, writes |
| [vedirect-protocol.md](vedirect-protocol.md) | The VE.Direct cable: text blocks, HEX frames, quirks |
| [platforms.md](platforms.md) | Linux, macOS and Windows: what works, what differs |
| [control-socket.md](control-socket.md) | The API of `smartsolard` on its socket: commands, arguments, results, errors |
| [vcsf-format.md](vcsf-format.md) | VictronConnect settings files (`.vcsf`) and how they map to registers |
| [quirks.md](quirks.md) | Odd behaviour and special cases of the device, BlueZ and the Raspberry Pi |
| [unknowns.md](unknowns.md) | Magic values that are replayed without being understood, and open questions |
| [releasing.md](releasing.md) | Versioning and how a release is made |

Everything was verified on one SmartSolar 75/15 (product id `0xA053`, firmware 1.64) unless a page says otherwise. Sources: Bluetooth traces of VictronConnect, Victron's "VE.Direct protocol, BlueSolar and SmartSolar MPPT chargers" (rev 18) and tests on the device.
