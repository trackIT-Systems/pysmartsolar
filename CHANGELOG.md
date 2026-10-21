# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), the versioning follows [Semantic Versioning](https://semver.org/). Releases are made by pushing a tag `vX.Y.Z` (see [docs/releasing.md](docs/releasing.md)).

## [Unreleased]

### Changed

- **Output formats.** Every reading and event goes out over MQTT as CSV, JSON and CBOR (`mqtt.csv`, `mqtt.json`, `mqtt.cbor`). All three carry the same fixed columns: `time` (with UTC offset; CBOR tag 0), `synced`, `device`, then the values under their names. The columns are the same for all sources, and MQTT no longer says which source a reading came from (no `source`). GATT's `panel_power` and `load_output_current` are published as `pv_power` and `load_current`, and `yield_today` is always in kWh (the advertisement's Wh are converted). The display texts with units (`"Battery voltage (V)"`) are gone from all outputs; units are documented per column in [docs/outputs.md](docs/outputs.md). CBOR is a list in column order with each number in its smallest exact form: a VE.Direct reading is about 175 bytes instead of 480. Event data is in one `detail` column (`key=value` pairs); the retained state is unchanged.
- **MQTT rate limit.** `mqtt.publish_interval_s` (default 300 s) allows at most one reading per device in that time. The best source held back goes out (VE.Direct, then GATT, then the advertisements), the newest within a source; `0` publishes every reading. Events, state and files are not limited.
- **CSV files.** One file per calendar day, `<csv.path>/<host>/smartsolar/<host>_<YYYY-MM-DD>_smartsolar.csv`, with the MQTT columns plus `block`; events in `.../events/`. This replaces one long-format file per daemon start.
- **Clock.** Readings taken before the clock is synced (the kernel's `adjtimex` state) are published over MQTT with `synced` false and held in a spool for the files. At the sync they are re-timed from the time since boot and written to the right day. A spool from a boot that never synced becomes `<host>_unsynced_<boot_id>_smartsolar.csv`.
- The default config file on Linux is `/etc/smartsolar/smartsolar.yml` (was `/boot/firmware/smartsolar.yml`). A system that keeps the file elsewhere passes `--config`.
- **Logging.** Under systemd, `smartsolard` logs to the journal directly, with the syslog priority of each message, `SYSLOG_IDENTIFIER` `smartsolard` and the source location (`CODE_FILE`, `CODE_LINE`, `CODE_FUNC`). Before, every line went through stderr and was stored as `info`. On a terminal, the output on stderr is unchanged.

## [1.2.0] - 2026-10-14

### Added

- `serial.port` can be a pattern such as `/dev/ttyUSBvd*` (the new default): the first match, by index, is used and looked up again at every open, so a cable that is plugged in later, or shows up under another index, is found. The daemon still follows one device.

## [1.1.1] - 2026-10-14

### Changed

- A VE.Direct cable that is not there (or goes away) is reported once, at INFO and as one `serial_lost` event, instead of at every retry (every 2 to 60 s). A different failure is reported again; the next successful open starts over. A system can therefore leave `serial.enabled: true` for the case that a cable is attached.

## [1.1.0] - 2026-10-13

### Added

- macOS and Windows support (untried with a real charger, tested against fakes in CI): the BLE code only asks bleak, the BlueZ pairing agent is Linux-only, and on macOS and Windows the system pairs (see `docs/platforms.md`).
- Devices can be named by serial number (`ble.address`, `keys:`), next to MAC addresses and the device UUIDs of macOS. Fetched keys are stored under the serial number.
- `daemon.socket: tcp://127.0.0.1:port` as a loopback control endpoint (the default on Windows); `--socket` takes it too.
- Per-platform default paths (config, state, data, socket); shutdown on Ctrl+Break on Windows.
- CI on Ubuntu, macOS and Windows (Windows reports without failing the build until it has been tried).

### Changed

- `dbus-fast` is only required on Linux. The serial port is only opened with `exclusive` on POSIX.

## [1.0.0] - 2026-10-13

A rewrite: from a one-shot library to a daemon and a CLI. It breaks with the 0.x API in every way (module, entry points, dependencies, output format), hence 1.0.0. The never released 0.2.0 development line is part of this release.

### Added

- `smartsolard`, a daemon that follows one SmartSolar permanently and reports to CSV (`/data/<host>/smartsolar/...`) and MQTT (JSON and CBOR, `{host}/smartsolar/{serial}/json|cbor`, retained `state`, `events`), configured by a YAML file.
- **Instant Readout** advertisement decoding (AES-128-CTR) as the main data path: no connection, works with the Wi-Fi access point up. Key from the config or fetched once over GATT and stored (mode 0600).
- Optional **GATT** (`gatt.mode: off | hold | poll`) for values that are not advertised, with pairing (passkey, BlueZ agent), keepalive, backoff and reconnects.
- **VE.Direct cable** as a second interface: text blocks as readings, the HEX protocol for registers. Either interface, or both at once; Bluetooth is optional (`ble.enabled: false`).
- **Writing** of registers (`smartsolar write`) over the cable or GATT with read-back verification: device mode, load output algorithm (off, BatteryLife, conventional 1 and 2, on, user 1 and 2, AES), battery type and charge settings (22 registers).
- Safety checks before every write: battery type must be `user` for charge voltages, the device's own voltage range and the temperature compensation limit (error 119), no VE.Direct port functions over the cable.
- **VictronConnect settings files** (`.vcsf`): `profile show|diff|load|save`, with product id, firmware and system voltage checks (`--ignore-device` for provisioning with a file made for another model).
- `smartsolar` CLI over a Unix socket: `status`, `cached`, `read` (`--raw`), `scan`, `rescan`, `reconnect`, `fetch-key`, `write`, `profile`, `decode`, `vregs`.
- Devices are identified by serial number everywhere (BLE name or `SER#`), the MAC address only as a fallback.
- Documentation of the protocols, the quirks of the device and the open questions in `docs/`.
- Tests without hardware (fake device, fake serial port, `pytest -W error`), CI on Python 3.12 to 3.14, release workflow.

### Changed

- Python 3.12 or newer; `bleak`, `dbus-fast`, `cbor2`, `cryptography`, `paho-mqtt`, `PyYAML` and `pyserial` instead of `gatt` and `dbus-python`.
- The registers are decoded from the GATT record stream (`[kind][ctx][CBOR id][CBOR value]`); the old parser misread the protocol.

### Removed

- The one-shot `read_nearest_vedevice()` API and the `python -m smartsolar` readout of 0.1.x.

### Fixed

- Pairing failed sometimes ([#1](https://github.com/trackIT-Systems/pysmartsolar/issues/1)): BlueZ starts pairing by itself while it discovers the services of a device that requires it, so the pairing agent has to be registered *before* the connection is opened (registered only around `pair()` it fails with `AuthenticationFailed`). The agent is now registered first. The readings no longer depend on pairing at all, because the Instant Readout advertisements are the main data source.
- A Raspberry Pi 4 did not connect to the device, `Page Timeout` ([#2](https://github.com/trackIT-Systems/pysmartsolar/issues/2)): on the Raspberry Pi the Wi-Fi and Bluetooth radios share one chip, and with the 2.4 GHz access point up a connection is mostly lost within a second. The daemon reads the advertisements (no connection); GATT is optional and retried with backoff.
- GATT connections failed on tsOS with the 2.4 GHz access point up (0 usable links in 26 attempts); the advertisements are now the main source.
- Pairing failed with `AuthenticationFailed` when the agent was registered only around `pair()`; it is registered before the connection is opened.

## [0.1.0] - 2022-11-15

Reconstructed from the git history; the version was never tagged and stayed 0.1.0 until the rewrite.

### Added

- `read_nearest_vedevice()`: scan for Victron devices, connect over GATT (`gatt`, `dbus-python`), pair automatically, read a fixed set of registers and return them as a dictionary (2022-11-05 to 2022-11-18).
- Automated pairing agent (2022-11-11), modular structure (2022-11-15), module logger and separated init and main (2022-11-18).
- Registers for panel voltage and panel power (2023-03-02).

### Fixed

- The manager stops on error (2022-11-20).
- A message is printed while pairing (2023-03-06).
- An error when the number of parameters was inconclusive (2024-02-12).

[1.2.0]: https://github.com/trackIT-Systems/pysmartsolar/releases/tag/v1.2.0
[1.1.1]: https://github.com/trackIT-Systems/pysmartsolar/releases/tag/v1.1.1
[1.1.0]: https://github.com/trackIT-Systems/pysmartsolar/releases/tag/v1.1.0
[1.0.0]: https://github.com/trackIT-Systems/pysmartsolar/releases/tag/v1.0.0
[0.1.0]: https://github.com/trackIT-Systems/pysmartsolar/commits/main
