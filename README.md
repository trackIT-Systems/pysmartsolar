# smartsolar

[![Tests](https://github.com/trackIT-Systems/pysmartsolar/actions/workflows/ci.yml/badge.svg)](https://github.com/trackIT-Systems/pysmartsolar/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/trackIT-Systems/pysmartsolar)](https://github.com/trackIT-Systems/pysmartsolar/releases/latest)
[![Python](https://img.shields.io/badge/python-3.12%20%7C%203.13%20%7C%203.14-blue)](pyproject.toml)
[![Platforms](https://img.shields.io/badge/platform-Linux%20%7C%20macOS%20%7C%20Windows-lightgrey)](docs/platforms.md)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

A daemon and a CLI for Victron SmartSolar charge controllers over Bluetooth Low Energy and the VE.Direct cable, made for autonomous sensor nodes (Raspberry Pi, [tsOS](https://github.com/trackIT-Systems/tsOS-base)). `smartsolard` follows one charger and reports its values to CSV files and MQTT; `smartsolar` talks to the running daemon.

> **Caution:** the GATT part is based on reverse engineering. Writing is limited to a short list of settings ([docs/writing.md](docs/writing.md)); use it only if you understand the effect.

## Features

* **Instant Readout:** decrypts the charger's advertisements with a passive scan, which keeps working while the Pi's 2.4 GHz access point is up. No connection and no pairing needed.
* **GATT (VE.Smart):** optional, for everything the advertisements lack (settings, history, firmware, load output), held open or polled. It reads chargers with Instant Readout disabled, and fetches the Instant Readout key once if it is not configured.
* **VE.Direct cable:** text blocks and the HEX protocol, with the same registers as GATT; it works with or without Bluetooth.
* **Outputs:** the same columns from every source, as CSV (one file per day) and MQTT (CSV, JSON and CBOR), stable within a major version ([docs/outputs.md](docs/outputs.md)).
* **Settings:** checked writes, read back; VictronConnect settings files (`.vcsf`) can be shown, compared, loaded and saved.

## Install

Python 3.12+ on Linux (BlueZ; the tested platform is a Raspberry Pi with tsOS), macOS or Windows ([docs/platforms.md](docs/platforms.md)).

```bash
pip install -e .
sudo install -D config/smartsolar.yml /etc/smartsolar/smartsolar.yml
smartsolard                       # or: smartsolard --config FILE
smartsolar status
```

The config file explains every key: [config/smartsolar.yml](config/smartsolar.yml). Give the Instant Readout key there (VictronConnect: device > settings > Product info > Instant readout via Bluetooth > Show), or leave it out and the daemon fetches it over GATT once. For a charger on a VE.Direct cable, set `serial.enabled: true`. How the daemon picks a charger, uses GATT and the cable, and why: [docs/daemon.md](docs/daemon.md).

## CLI

```
smartsolar status                  device, key, advertisement, GATT and cable state
smartsolar cached [advert|gatt|vedirect]   last values the daemon has (no radio traffic)
smartsolar read [advert|gatt|vedirect]     newest values; GATT in poll mode connects now
smartsolar scan                    Victron devices seen, with model, record type and key state
smartsolar rescan | reconnect      choose the device again | drop and reopen the GATT link
smartsolar write NAME=VALUE ... [--via serial|gatt]   change settings (docs/writing.md)
smartsolar profile show|diff|load|save FILE.vcsf   VictronConnect settings files
smartsolar history [--days N]      the charger's day records: today and up to 30 days before
smartsolar fetch-key [--show]      read the Instant Readout key over GATT now
smartsolar decode --key HEX DATA   decode one advertisement offline
smartsolar vregs [--writable]      the registers, or the ones `write` can change
```

`--json` prints the raw result. Exit codes: 0 ok, 1 daemon error, 2 usage error, 3 daemon unreachable. The control socket's API: [docs/control-socket.md](docs/control-socket.md).

## Documentation

[docs/](docs/README.md) has the daemon, the outputs, writing and profiles, the protocols (Instant Readout, GATT, VE.Direct, `.vcsf`), the quirks of the device and what is still unknown. Releases follow semantic versioning ([docs/releasing.md](docs/releasing.md), [changelog](CHANGELOG.md)). `tests/` runs without hardware against fakes.

## Victron Energy

The SmartSolar charge controllers, their firmware, VictronConnect and the protocols they speak are the work and the intellectual property of [Victron Energy B.V.](https://www.victronenergy.com/) Victron, SmartSolar, BlueSolar, VictronConnect, VE.Direct, VE.Smart and Instant Readout are their names and trademarks. This project is not made, endorsed or supported by Victron Energy.

Its only goal is to let a sensor node work with these chargers: read what they measure and keep their settings, as VictronConnect does. It is written with respect for Victron's work and in thanks for their openness: they publish the VE.Direct protocol and the Instant Readout format, announced the Bluetooth protocol to the community, and answer questions about it in their forum. Nothing of Victron's is redistributed here: no firmware, no code of VictronConnect and none of its data files (such as its register dictionary); the protocol descriptions in [docs/](docs/README.md) are our own, written to interoperate. For anything beyond reading values and the few settings `smartsolar` can write, use VictronConnect.

## Credits

* **Victron Energy**, for the chargers and for documenting them:
  * the **VE.Direct** protocol: the text protocol, and the registers and encodings of the HEX protocol from ["VE.Direct protocol, BlueSolar and SmartSolar MPPT chargers"](https://www.victronenergy.com/upload/documents/BlueSolar-HEX-protocol.pdf) (rev 18);
  * the **Instant Readout** advertisements ([VictronConnect manual](https://www.victronenergy.com/media/pg/VictronConnect_app/en/stored-trends---instant-readout.html) and Victron's published format);
  * the **Bluetooth GATT protocol**, announced by Victron in the community thread ["Victron Bluetooth BLE Protocol announcement"](https://communityarchive.victronenergy.com/questions/93919/victron-bluetooth-ble-protocol-publication.html) (2021).
* The **GATT protocol** (VE.Smart) was worked out by studying how VictronConnect 6.43 talks to a charger, guided by the notes in ["Some notes about Victron BLE"](https://github.com/Olen/VictronConnect/issues/2) (Olen/VictronConnect, issue 2), and checked against a real SmartSolar 75/15 and its VE.Direct cable. What is understood and what is still open is in [docs/gatt-protocol.md](docs/gatt-protocol.md) and [docs/unknowns.md](docs/unknowns.md).
* The community project [victron-ble](https://github.com/keshavdv/victron-ble) is a Python implementation of the Instant Readout decoding and a useful cross-reference for the AES-CTR layout; `smartsolar` has its own implementation (see [docs/instant-readout.md](docs/instant-readout.md)).
* Developed by the trackIT Systems team for the tsOS sensor platform. The first versions (0.x, 2022-2024) were a one-shot BLE query library by Jonas Höchst.

## License

[MIT](LICENSE). The license covers this project's code and documentation, not Victron's products, protocols or trademarks.
