# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), the versioning follows [Semantic Versioning](https://semver.org/). Releases are made by pushing a tag `vX.Y.Z` (see [docs/releasing.md](docs/releasing.md)).

## [1.3.0] - 2026-11-18

**Breaking: the MQTT and CSV outputs are new** (see Changed and [docs/outputs.md](docs/outputs.md)). Readings and events go out with fixed columns as CSV, JSON and CBOR, without time, display texts or `source`; the retained `state` topic is gone; the CSV files are one file per day. Consumers of the 1.2 outputs must be adapted; tsOS brings this version with the tsOS release that announces the change. This is the one exception to semantic versioning: the CLI, the config and the control socket stay compatible with 1.2, and from 1.3.0 on the outputs are covered too ([docs/releasing.md](docs/releasing.md)). Two values change because they were decoded wrongly over GATT and the cable: `battery_current` (was the charge current, now in `charge_current`) and `uptime` (see Fixed).

### Added

- **MIT license** ([LICENSE](LICENSE)), and a section on Victron Energy in the README: the chargers, their firmware and protocols are Victron's; this project only interfaces with them and redistributes nothing of theirs. The credits name Victron's documentation and the community thread and notes the GATT protocol was worked out with.
- **Chargers with Instant Readout disabled are read over GATT** (#5). Such a charger advertises only its product id (`10 02 | model`). It is now selected, after any charger with Instant Readout, and read over GATT without fetching a key: in `gatt.mode`, or, while that is `off`, in the new `gatt.fallback` (default `hold`; `off` restores the old behaviour). It is recognised by its product id or its `SmartSolar` name; `smartsolar scan` shows `readout off` and `status` "Instant Readout off, GATT only". The scan watchdog leaves it alone (its adverts are mostly not reported while the access point is up), and a connect falls back to the device BlueZ knows from its last advert when the lookup does not find it again. While the 2.4 GHz access point is up, finding and connecting to it takes many attempts; a held connection then stays.
- **Passive scanning.** On Linux, the scan for advertisements and the lookup before a GATT connection are passive where BlueZ offers `AdvertisementMonitor1` (bluetoothd with `Experimental = true`), matching Victron's manufacturer data. A passive scan sends no scan requests:
  - It needs no scan response. During an active scan BlueZ reports a connectable advertisement only together with its scan response, and with the Pi's Wi-Fi access point up none arrives: a SmartSolar went unseen for good (measured on a Pi 4 with the hotspot up: last seen 38 minutes ago while its adverts reached the controller several times a second).
  - It needs no random address, which the controller refuses while the station advertises: the kernel logged `Bluetooth: hci0: Opcode 0x2005 failed: -16` at every scan restart, about every 10 s (tsOS-base#50).
  - The name, and with it the serial number, is only in the scan response. BlueZ keeps the names it has seen (`/var/lib/bluetooth/<adapter>/cache`), so a charger seen once in an active scan keeps its serial number; a new one is known by its MAC address until then.
  - bleak reports an advertisement in a passive scan only when its data changes, so a charger with Instant Readout off (always the same bytes) was not reported to a daemon started after bluetoothd. BlueZ calls `DeviceFound` on the advertisement monitor for every advert; the daemon replaces bleak's monitor class at runtime (in its own process, bleak's files are unchanged) by one that reports each of them. Without the bleak internals this needs, `auto` scans actively.

  `ble.scanning` chooses: `auto` (default: passive, else active with one warning), `passive` (no fallback) or `active`. macOS and Windows always scan actively.

- **More GATT values, as columns**: `channel_1_current_ma` (`0xED8C`), `pv_power_trend` and `load_current_trend` (`0xEC8A`, `0xEC89`), `remote_control_used` (`0x0202`), `ble_mode` (`0x0090`), `ble_capabilities` (`0x0150`), and the history records: all time (`0x104F`: `errors_total`, `yield_system_total`, `panel_voltage_max_total`, `battery_voltage_max_total`, `battery_voltage_min_total`, `history_days`) and today (`0x1050`: `consumed_today`, `battery_voltage_max_today`, `battery_voltage_min_today`, `errors_today`, `time_bulk_today`, `time_absorption_today`, `time_float_today`, `battery_current_max_today`, `panel_voltage_max_today`, and `day_sequence`, which VE.Direct already had). The SmartSolar pushes them; a poll reads them too. Their decoding was checked against the register dictionary in VictronConnect and against the VE.Direct cable of the same charger.

- **Four more settings for `smartsolar write`**: `instant_readout` (`0xEC7D`, `on`/`off`), `custom_name` (`0x010C`, text), `bms_present` (`0xEDE8`) and `identify` (`0x010E`, blinks the LEDs), also as columns. `instant_readout` belongs to the charger's Bluetooth part (VE.Smart instance 0), which the VE.Direct cable does not reach. A write over the cable first reads every register it changes; when the device does not have one, nothing is written and `auto` takes GATT for the whole write.
- **The VE.Direct cable reads the registers too** (`serial.registers_interval_s`, default 60 s, `0` off; needs `serial.hex`): the settings, the history records, the firmware and everything else GATT publishes, so a VE.Direct reading fills the same columns. Registers the device does not have are not asked for again until the port is reopened.
- **`smartsolar history`** (socket command `history`): the charger's day records, today and up to 30 days before (`0x1050`..`0x106E`): yield, load consumption, maximum power, battery voltage range, maximum battery current, maximum panel voltage, time in bulk, absorption and float, the day's errors. Read over the cable or GATT like the settings.
- **GATT fills the columns of the VE.Direct text block**: `tracker_mode` (`0xEDB3`), `off_reason` (`0x0207`), `load_output` (`0xEDA8`), `device_state_code` and `charger_error_code`. The two transports now publish the same columns; the tracker mode and off reason tables are shared.

### Fixed

- **GATT works: the daemon speaks VE.Smart, VictronConnect's protocol, instead of replaying a trace of it**. The replay wrote several chunks without waiting for the device to allow them, wrote chunks larger than the device takes, and never read the transport parameters; a SmartSolar 75/15 (firmware 1.64) then sent hundreds of empty notifications a second and closed the link after 10 s, before any value arrived. The protocol was taken from VictronConnect 6.43 and is described in [docs/gatt-protocol.md](docs/gatt-protocol.md): the session reads the parameters from the control characteristic, sends one chunk at a time once the device allows it (`F9 01`), gives received chunks back, asks for the device list, subscribes to every instance and finds the charger's instance by its product id (the byte that was called "context" and fixed to 3). The device then pushes the values that change about once a second. `GattLink` has a `read()` for the parameters.
- **A held GATT link ignored key requests and a change of mode.** `smartsolar fetch-key` timed out while the link was held, because the key was only fetched before the hold began; and after `instant_readout=on`, the fallback hold went on although the charger now advertises its data. The hold now serves key requests and ends when the mode is no longer `hold`; a missing key is then fetched and the link closed.
- **GATT's `battery_current` was the charge current.** `0xEDD7` is the current the charger delivers, to the battery and the load output together (seen on a SmartSolar 75/15: 2.1 A with 1.1 A into the battery and 1.0 A load, where the VE.Direct cable said 1.1 A). `battery_current` now comes from channel 1 (`0xED8F`), as the text block's `I`; `0xEDD7` is the new column `charge_current`.
- **`uptime` is the time since the charger started** (`0x0120`, `VE_REG_UPTIME`, also over the cable). It was `0xEC5A`, a time reference of the trend data (136 days on a charger that had started 19 days before), which is now the column `trend_time_reference`.
- **Changing the load output algorithm switched the streetlight function off.** `load_output_control` is bits 0..3 of `0xEDAB`; bit 7 switches the streetlight function (VictronConnect's streetlight page). A write sent the whole byte and cleared bit 7. It now keeps the bits outside the setting as the device has them, over the cable and GATT, and refuses the write if the register cannot be read first.
- **A settings file needs the full `VCCFH` header**, as VictronConnect's reader does; the fifth byte was ignored before.
- **The scan ended for good when bluetoothd restarted** (#9). The scan's advertisement monitor, or its discovery, goes with bluetoothd, but bleak's scanner does not notice, and the scan watchdog only acts for a selected charger, which is deselected once its adverts stop. The daemon now watches the system bus for bluetoothd taking its name again and starts the scan anew within seconds (`scan_restarted` event); so it does when the scanner cannot be restarted after a GATT connect. Tested on a Raspberry Pi with `systemctl restart bluetooth` and with bluetoothd stopped for 30 s.
- **Refused GATT writes and reads name the reason** from the device's ValueResponse: `unknown register` (1), `access denied` (3: a write to a read-only register, a read of a write-only one), `index out of range` (-5). A write to a read-only register now fails at once instead of after the timeout.
- **Channel 1 voltage (`0xED8D`) is signed**, as in Victron's register dictionary.
- **`Future exception was never retrieved` (ERROR) after a lost GATT link.** A register request or write registers the wait for its answer before the request is written. When the link was lost during that write, the write failed and the wait got the `link lost` error, which nobody awaited any more; asyncio logged it with a traceback. The wait is now dropped when the write fails.
- **Retry delays overflowed.** After 1024 retries without success (about 8.5 h of a VE.Direct port that is not there, or of failing GATT connections), the delay calculation raised `OverflowError(34, 'Numerical result out of range')` and the daemon exited. The retry count no longer grows once the delay has reached its maximum (#4).

### Changed

- **`gatt.keepalive_s` is 3 s** (was 5 s). The device closes the link 10 s after the last keepalive; VictronConnect sends one every 3.3 s.
- **Logging.** Under systemd, `smartsolard` logs to the journal through `python3-systemd`'s `JournalHandler` (Debian package `python3-systemd`), with the syslog priority of each message, `SYSLOG_IDENTIFIER` `smartsolard`, the source location (`CODE_FILE`, `CODE_LINE`, `CODE_FUNC`), `LOGGER`, `THREAD_NAME`, `PROCESS_NAME`, `CODE_ARGS` and, with a traceback, `EXCEPTION_TEXT`. Before, every line went through stderr and was stored as `info`. Without `python3-systemd` (macOS) and on a terminal it logs to stderr as before (tsOS-base#44).
- **Output formats.** Every reading and event goes out over MQTT as CSV, JSON and CBOR (`mqtt.csv`, `mqtt.json`, `mqtt.cbor`). All three carry the same fixed columns: `device`, then the values under their names. They carry no time (the receiver stamps a reading on arrival, tsOS-base docs/time.md), and nothing is retained. The columns are the same for all sources, and MQTT no longer says which source a reading came from (no `source`). GATT's `panel_power` and `load_output_current` are published as `pv_power` and `load_current`, and `yield_today` is always in kWh (the advertisement's Wh are converted). The display texts with units (`"Battery voltage (V)"`) are gone from all outputs; units are documented per column in [docs/outputs.md](docs/outputs.md). CBOR is a list in column order with each number in its smallest exact form: a VE.Direct reading is about 145 bytes instead of 480. Event data is in one `detail` column (`key=value` pairs). The retained `state` topic and its `offline` last will are gone: a silent controller shows as missing readings, and connection changes are events.
- **MQTT rate limit.** `mqtt.publish_interval_s` (default 300 s) allows at most one reading per device in that time. The best source held back goes out (VE.Direct, then GATT, then the advertisements), the newest within a source; `0` publishes every reading. Events and files are not limited.
- **CSV files.** One file per calendar day, `<csv.path>/<host>/smartsolar/<host>_<YYYY-MM-DD>_smartsolar.csv`, with `time` (UTC offset) first, then the MQTT columns plus `block`; events in `.../events/`. This replaces one long-format file per daemon start.
- **Clock.** Rows reach the CSV files only once the clock is trusted (the kernel's `adjtimex` state: chrony has a source, NTP, GNSS or the checked RTC). Before that, readings only go over MQTT; there is no spool and no re-timing.
- The default config file on Linux is `/etc/smartsolar/smartsolar.yml` (was `/boot/firmware/smartsolar.yml`). While it does not exist, the old one is still read, with a warning to move it. A system that keeps the file elsewhere passes `--config`.
- **Default log level WARNING.** `daemon.log_level` defaults to `WARNING` (was `INFO`), as do the shipped configs, following tsOS's logging convention: a working station logs nothing in normal operation. `-v` still turns on DEBUG.
- **Log changes of state.** A failure that is retried (a GATT connection, the VE.Direct port, starting or stopping the scan, the scan watchdog, the BLE scanner after a connection) is logged once when it starts, again only when the error changes, and once when it works again (`... works again`, `Connected to ...`); the repeats are at DEBUG. A GATT failure counts as over with the first reading, not when the link comes up, so a connection whose work keeps failing (e.g. the key fetch) stays one failure. GATT's state changes (`connecting`, `backoff`, ...) are DEBUG; a poll's connection is told at INFO only the first time. A missing VE.Direct cable is INFO when `serial.port` is a pattern (the default `/dev/ttyUSBvd*`: a cable if there is one) and a WARNING for a fixed port. A failing output or callback logs its traceback (ERROR) only the first time for the same error. A config error or a fatal error at start goes to the log at CRITICAL instead of `error: ...` on stderr (exit code 1 as before). Messages are capitalized and name what they are about, and the daemon's logger is `smartsolar.daemon` (was `smartsolard`; the `SYSLOG_IDENTIFIER` stays `smartsolard`).
- **Compatibility from here on.** The outputs are now covered by semantic versioning like the CLI, the config and the socket: within a major version nothing is removed, renamed or changes its meaning, and new columns, events, keys and commands come in minor versions ([docs/releasing.md](docs/releasing.md)). Columns are only appended; the messages carry no schema version, the number of values in a CBOR array tells which columns it has ([docs/outputs.md](docs/outputs.md#compatibility)). A test pins the columns of 1.3.0.

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

[1.3.0]: https://github.com/trackIT-Systems/pysmartsolar/releases/tag/v1.3.0
[1.2.0]: https://github.com/trackIT-Systems/pysmartsolar/releases/tag/v1.2.0
[1.1.1]: https://github.com/trackIT-Systems/pysmartsolar/releases/tag/v1.1.1
[1.1.0]: https://github.com/trackIT-Systems/pysmartsolar/releases/tag/v1.1.0
[1.0.0]: https://github.com/trackIT-Systems/pysmartsolar/releases/tag/v1.0.0
[0.1.0]: https://github.com/trackIT-Systems/pysmartsolar/commits/main
