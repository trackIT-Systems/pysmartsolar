# smartsolar

[![Tests](https://github.com/trackIT-Systems/pysmartsolar/actions/workflows/ci.yml/badge.svg)](https://github.com/trackIT-Systems/pysmartsolar/actions/workflows/ci.yml)

A daemon and a CLI for Victron SmartSolar charge controllers over Bluetooth Low Energy and over the VE.Direct cable, for autonomous sensor nodes (Raspberry Pi). `smartsolard` follows one SmartSolar permanently and reports its values to a CSV file and to MQTT (JSON and CBOR). `smartsolar` talks to the running daemon: cached and live values, key, reconnect, scan, settings.

> **Caution:** the GATT part of this project is based on reverse engineering. Writing is limited to a short list of registers (see [Writing](#writing)); use it only if you understand the effect.

## Why advertisements, and GATT only on top

A GATT connection to a SmartSolar is unusable while the Raspberry Pi's 2.4 GHz Wi-Fi access point is up: the Wi-Fi and Bluetooth radios share one chip, and links are set up and then lost within a second (0 usable links in 26 attempts; with the access point down, 10 of 10 connected in half a second). The **Instant Readout** advertisements are not affected: the device broadcasts its main values encrypted, about 4 times a second, and a passive scan receives them reliably with the access point up (2447 of 2447 advertisements decrypted in 10 minutes). So:

* The advertisements are always the main data source. No connection, no pairing.
* GATT is optional (`gatt.mode`) for the values that are not in the advertisements (firmware, uptime, load output, ...), and it fetches the Instant Readout key once if you did not configure it.

The measurements behind this, and everything else that was found out about the device, are in [docs/](docs/README.md), starting with [docs/quirks.md](docs/quirks.md).

## VE.Direct cable

Some systems have the charger on a VE.Direct cable (FTDI USB, `/dev/ttyUSBvd<n>` on tsOS), with or without Bluetooth. The daemon can use either interface or both; each is its own block of readings (`source` in MQTT, `block` in the CSV): `advert`, `gatt` and `vedirect`.

```yaml
serial:
  enabled: true
  port: /dev/ttyUSBvd*    # a port, or a pattern: the first match (lowest index) is used, looked up at every open
ble:
  enabled: false          # a system without Bluetooth: nothing of the BLE stack is started
```

* **Text blocks.** The charger pushes a block of text about once a second (battery and panel voltage, current, power, state, error, yields, load output, serial number; 19200 baud). Each valid block (checksum) becomes a reading. A block can be lost now and then (the device sometimes garbles the block after a burst of HEX frames); `smartsolar status` counts them.
* **HEX protocol.** The same cable reads and writes the settings registers (the same ids and encodings as over GATT, so `write`, `profile diff` and `profile load` work unchanged). Without Bluetooth this is the only way to provision a charger. A request makes the device cut the text block it is sending, so it is only used when you ask (`serial.hex: false` keeps the daemon from ever sending a byte).
* **Which way settings take.** `daemon.registers: auto` uses the cable when it is connected and falls back to GATT; `serial` and `gatt` insist on one. `--via serial|gatt` overrides it per command. `rx_port_function` and `tx_port_function` are never written over the cable (a wrong value could cut the connection).
* **Identity.** Devices are called by their serial number everywhere (MQTT topic `{host}/smartsolar/{serial}/json`, CSV `device`). The cable sends it (`SER#`); over Bluetooth it is part of the name (`SmartSolar HQ22294ZGEU`). With both interfaces the BLE device with the cable's serial number is the one that is followed, whatever its signal strength. The MAC address is only used when no serial number is known.
* **One reader.** The port is opened exclusively. Do not run another program that reads it (the old `vedirect_dump` of mqttutil, for example): two readers take bytes from each other and the blocks fail their checksums.
* The charger's TX port function must stay `communication` (the default); `rx_port_function` and `tx_port_function` can only be changed over Bluetooth.

## Install

Python 3.12+. Linux (BlueZ; the Raspberry Pi with tsOS is the tested platform), macOS and Windows; see [docs/platforms.md](docs/platforms.md) for what differs (pairing, device names, control socket). BlueZ is not needed with `ble.enabled: false`; pyserial is used for the cable.

```bash
pip install -e .          # smartsolard, smartsolar
cp config/smartsolar.yml /boot/firmware/smartsolar.yml
smartsolard --config config/smartsolar.yml
```

## Instant Readout key

Each device has a 16 byte key. Either put it in the config (VictronConnect: device > settings > Product info > Instant readout via Bluetooth > Show) or leave it out: the daemon then connects once over GATT, pairs with `gatt.passkey`, reads the key (register `0xEC65`) and stores it in `daemon.state_dir` (mode 0600). That one connection needs a working link, so run it with the access point down, or retry until it works (failed attempts are retried with a growing delay). `smartsolar fetch-key` triggers it by hand. If the device is reset and the stored key stops matching, it is dropped and fetched again; a wrong key in the config is only reported (`smartsolar status`: `key ... mismatch`).

The key and the passkey are never logged and never published.

## Daemon

`smartsolard [--config FILE]`, default config `/boot/firmware/smartsolar.yml`, see [config/smartsolar.yml](config/smartsolar.yml).

* **Device:** the configured `ble.address`, otherwise the strongest SmartSolar (solar charger records of other Victron devices are listed but never selected). The selection is reviewed every `ble.rescan_interval_s`; another device must be `ble.switch_hysteresis_db` stronger to replace the current one.
* **Advertisements:** decoded as they arrive; the latest reading is published every `advert.publish_interval_s` (the device sends a new one about once a second). Fields: state, charger error, battery voltage and current, yield today, PV power, load current; values the device reports as not available are `null`.
* **GATT** (`gatt.mode`):
  * `off`: no connection, except to fetch a missing key.
  * `hold`: keep a connection and publish the values the device pushes every `gatt.publish_interval_s`. A ping every `gatt.keepalive_s` keeps it open (the device closes the link 10 s after the last ping).
  * `poll`: connect every `gatt.poll_interval_s`, collect the values, publish, disconnect.

  The device keeps advertising while a GATT connection is open, so the advertisement data stays fresh in `hold` and `poll`.

  While a connection is being opened the advertisement scan is paused (BlueZ rejects connects while the same client scans), so with an access point up on 2.4 GHz the connection attempts of `hold` and `poll` cost advertisement readings (in a test, no reading for the first minute while the first attempts timed out). Failed attempts are retried with a jittered, growing delay, and nothing here touches Wi-Fi. Use `gatt.mode: off` there.
* **CSV:** `/data/<host>/smartsolar/<host>_<timestamp>-smartsolar.csv`, one row per value (`ts, device, block, key, value, unit`; block `advert`, `gatt` or `event`).
* **MQTT:** `{host}/smartsolar/{mac}/json|cbor` per reading (field `source`: `advert` or `gatt`), `{host}/smartsolar/state/json|cbor` (retained, `offline` as the last will) and `{host}/smartsolar/events/json|cbor`.

## CLI

```
smartsolar status                  device, key, advertisement, GATT and cable state
smartsolar cached [advert|gatt|vedirect]   last values the daemon has (no radio traffic)
smartsolar read [advert|gatt|vedirect]     newest advertisement values; GATT: hold = now, poll = connects now; vedirect: newest text block
smartsolar scan                    Victron devices seen, with model, record type and key state
smartsolar rescan                  choose the device again now
smartsolar reconnect               drop the GATT connection and connect again
smartsolar write NAME=VALUE ... [--via serial|gatt]   change settings over the cable or GATT (see Writing)
smartsolar profile show|diff|load|save FILE.vcsf   VictronConnect settings files (see Profiles)
smartsolar fetch-key [--show]      read the Instant Readout key over GATT now
smartsolar decode --key HEX DATA   decode one advertisement offline
smartsolar vregs [--writable]      the registers decoded over GATT, or the ones `write` can change
```

`--json` prints the raw result, `--socket` / `--config` select the daemon. Exit codes: 0 ok, 1 daemon error, 2 usage error, 3 daemon unreachable. The CLI and the daemon talk newline-delimited JSON over a Unix socket (`daemon.socket`).

## Writing

`smartsolar write NAME=VALUE ...` changes settings through the daemon, over the VE.Direct cable if it is connected, else over GATT (it connects on demand in `off` and `poll` mode and uses the open link in `hold`); see [VE.Direct cable](#vedirect-cable) for `--via`. Only the settings in `smartsolar vregs --writable` can be written; everything else, including the key, the passkey, the keepalive, restore-defaults and the system voltage, is refused before anything is sent.

```
smartsolar write device_mode=off
smartsolar write float_voltage=13.6 absorption_voltage=14.4 --yes
smartsolar vregs --writable
```

| Setting | Values |
| --- | --- |
| `device_mode` | `on`, `off` (the charger) |
| `battery_type` | `user` or a factory preset (`preset1` to `preset8`) |
| `battery_max_current`, `tail_current` | A |
| `absorption_voltage`, `float_voltage`, `equalisation_voltage` | V, inside the range the device allows |
| `temperature_compensation` | mV/K, at most ±21 for a 12 V system |
| `absorption_time_limit`, `equalisation_duration` | hours |
| `adaptive_mode`, `equalisation_auto_stop` | `on`, `off` |
| `auto_equalise_days`, `equalisation_current_percent`, `rebulk_voltage_offset`, `low_temperature_level`, `low_temperature_current` (`none`: the maximum) | |
| `load_output_control` | `off`, `batterylife`, `conventional1`, `conventional2`, `on`, `user1`, `user2`, `aes` (`alt1`, `alt2` are old names) |
| `load_user_high_voltage`, `load_user_low_voltage`, `load_aes_timer` | V, V, minutes |
| `rx_port_function`, `tx_port_function` | the VE.Direct port functions |

It asks for confirmation on a terminal (`--yes` skips it, and is required without one). Every write is checked twice: the device must echo the value, and a read-back must show it. A write the device does not accept gets **no answer at all**, which is reported as a failure. The old and the new value are shown, and every write is logged as a `register_written` event (CSV and MQTT). Exit code 1 if a write failed or the read-back differs.

**Safety checks before a write**, from Victron's VE.Direct protocol document: the charge voltages are only taken if `battery_type` is `user` (otherwise the write is refused with a hint; set the type first), and a voltage outside the range the device reports (8 to 17.4 V for a 12 V system) or a temperature compensation beyond ±21 mV/K makes the device raise **error 119** (settings data invalid), which only a factory reset plus a power cycle clears. `smartsolar` reads the allowed range from the device and refuses such values. The settings are stored in non-volatile memory: do not write them in a loop.

`load_output_control` is the load output algorithm of the VictronConnect load output settings: `batterylife` is the self-adapting default, `conventional1` switches off below 11.1 V and on above 13.1 V, `conventional2` below 11.8 V and above 14.0 V (12 V system; multiplied for 24 V and 48 V), `on` and `off` are always on and always off, and `user1`, `user2` and `aes` (Automatic Energy Selector) use `load_user_high_voltage` and `load_user_low_voltage` (and the AES timer). Except for `on` and `off`, the load output follows an algorithm only after a 2-minute delay. Whether `device_mode=off` stops charging was only seen as the register value (the test happened at night), and it only works while remote on/off is enabled (register `0x0202`, bit 1; set by default on firmware 1.42 and newer).

Not exposed on purpose: restore-defaults and clear commands, the system voltage, the Instant Readout key and the passkey. The streetlight timer, the relay and the BMS settings are not covered yet. Other registers can be added to `WRITABLE` in `src/smartsolar/vreg.py`.

## Profiles

The folder [profiles/](profiles/) has settings files made with VictronConnect (SmartSolar 75/10 AGM and LiFePO4, 75/15 LiFePO4). They are complete app files and contain the history and serial number of the charger they were saved from; only the `/Settings/` part is used.

A profile is a **VictronConnect settings file** (`.vcsf`): the file the app saves with "Save settings to file" and loads onto a charger of the same model. `smartsolar` reads those files and applies their settings over GATT; it can also save the settings of the charger to such a file.

```
smartsolar profile show "profiles/VE MPPT 75_15 LiFePo Profile.vcsf"   what the file sets (works without the daemon)
smartsolar profile diff FILE.vcsf                            compare with the charger
smartsolar profile load FILE.vcsf                            check, diff, confirm, write only what differs, read back
smartsolar profile load FILE.vcsf --dry-run                  the same without writing
smartsolar profile load FILE.vcsf --ignore-device            also for a file made for another product id or newer firmware
smartsolar profile save FILE.vcsf                            save the charger's settings (experimental)
```

The file is a `VCCF` header and a zlib-compressed JSON document with VictronConnect's model of one device: the settings under `/Settings/`, but also live values, history and the serial number. Only the `/Settings/` keys are used; the others are ignored. 22 settings are imported (the table above, except the device mode); the other `/Settings/` keys (streetlight, relay, BMS, auxiliary input) are counted and reported but not imported, because their registers were not checked yet. The mapping was checked against a SmartSolar 75/15 that was configured like the sample file (`tests/data/ve-mppt-75-15-lifepo4.vcsf`): reading the registers gave the same values for every setting of the file, and `profile diff` of the unmodified file says "nothing to change".

`profile load` refuses to write anything if the file does not fit the charger: another product id, a firmware older than the file's minimum, or a different system voltage (the file's charge voltages are for another battery voltage). For provisioning with a file made for another model, `--ignore-device` turns the product id and firmware mismatches into warnings; the system voltage mismatch always blocks, and the charger still refuses values outside its own ranges. It writes the battery type first, and if the file selects a factory preset instead of `user` it only writes the type, because the charge voltages are part of the preset. Settings the charger does not report (older firmware) are left out with a note. The same range and battery type checks as for `write` apply.

`profile save` writes the same container with the settings of the charger, the product id, the firmware as the minimum version and the system voltage. **This export is experimental:** `smartsolar` reads it back, but whether VictronConnect accepts such a file was not tested, because the file lacks the live values and history of a file made by the app. Use it to restore the charger with `profile load`, and do not rely on it for the app.

## GATT protocol

Service `306b0001-b081-4037-83dc-e59fcc3cdfd0`, three characteristics (`0021` control, `0024` and `0027` data). Pairing: SMP passkey, default `000000`. A BlueZ agent that answers the passkey request is registered from before the connect until pairing is done (`dbus-fast`). Answers are records `[kind][ctx][CBOR register id][CBOR value]`, requests are `[0x05 get | 0x06 set][ctx][CBOR array]`; registers are read and written in context 3 (contexts 0 and 1 only answer for a few static registers). The details are in `src/smartsolar/vreg.py`, the register list is `smartsolar vregs`. The protocol, the quirks of the device and what is still unknown are written down in [docs/](docs/README.md).

## Layout

`src/smartsolar/`: `instant.py` (Instant Readout decoding), `vreg.py` (GATT records and registers), `tracker.py` (device selection, decoding), `keys.py`, `gatt_session.py` (one link), `gatt_manager.py` (modes, retries), `daemon.py`, `cli.py`, `server.py` and `ipc.py` (control socket), `outputs/`, `ble/` (transport interface; only `ble/bleak_backend.py` and `ble/agent.py` import bleak and dbus-fast). `tests/` runs without hardware against a fake backend (`tests/fakes.py`).

## Releases

Semantic versioning; releases are made by pushing a tag `vX.Y.Z`, see [docs/releasing.md](docs/releasing.md) and the [changelog](CHANGELOG.md).

## Credits

* The **Instant Readout** format is described in Victron's published documentation. The community project [victron-ble](https://github.com/keshavdv/victron-ble) is a Python implementation of the same decoding and a useful cross-reference for the AES-CTR layout; `smartsolar` has its own implementation (see [docs/instant-readout.md](docs/instant-readout.md)).
* The **VE.Direct** registers and encodings are from Victron's "VE.Direct protocol, BlueSolar and SmartSolar MPPT chargers" (rev 18).
* The **GATT protocol** was derived from Bluetooth traces of the VictronConnect app and tests on a real charger; what is understood and what is replayed blindly is in [docs/gatt-protocol.md](docs/gatt-protocol.md) and [docs/unknowns.md](docs/unknowns.md).
* Developed by the trackIT Systems team for the tsOS sensor platform. The first versions (0.x, 2022-2024) were a one-shot BLE query library by Jonas Höchst.
