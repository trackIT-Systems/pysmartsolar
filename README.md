# smartsolar

[![Tests](https://github.com/trackIT-Systems/pysmartsolar/actions/workflows/ci.yml/badge.svg)](https://github.com/trackIT-Systems/pysmartsolar/actions/workflows/ci.yml)

A daemon and a CLI for Victron SmartSolar charge controllers over Bluetooth Low Energy, for autonomous sensor nodes (Raspberry Pi). `smartsolard` follows one SmartSolar permanently and reports its values to a CSV file and to MQTT (JSON and CBOR). `smartsolar` talks to the running daemon: cached and live values, key, reconnect, scan.

> **Caution:** the GATT part of this project is based on reverse engineering. Writing is limited to a short list of registers (see [Writing](#writing)); use it only if you understand the effect.

## Why advertisements, and GATT only on top

A GATT connection to a SmartSolar is unusable while the Raspberry Pi's 2.4 GHz Wi-Fi access point is up: the Wi-Fi and Bluetooth radios share one chip, and links are set up and then lost within a second (0 usable links in 26 attempts; with the access point down, 10 of 10 connected in half a second). The **Instant Readout** advertisements are not affected: the device broadcasts its main values encrypted, about 4 times a second, and a passive scan receives them reliably with the access point up (2447 of 2447 advertisements decrypted in 10 minutes). So:

* The advertisements are always the main data source. No connection, no pairing.
* GATT is optional (`gatt.mode`) for the values that are not in the advertisements (firmware, uptime, load output, ...), and it fetches the Instant Readout key once if you did not configure it.

The measurements are in the trackIT wiki ("Victron SmartSolar BLE readout").

## Install

Python 3.12+, BlueZ.

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
smartsolar status                  device, key, advertisement and GATT state
smartsolar cached [advert|gatt]    last values the daemon has (no radio traffic)
smartsolar read [advert|gatt]      newest advertisement values; GATT: hold = now, poll = connects now
smartsolar scan                    Victron devices seen, with model, record type and key state
smartsolar rescan                  choose the device again now
smartsolar reconnect               drop the GATT connection and connect again
smartsolar write NAME=VALUE ...    change a register over GATT (see Writing)
smartsolar profile save|load|diff|show|list   sets of charge settings (see Profiles)
smartsolar fetch-key [--show]      read the Instant Readout key over GATT now
smartsolar decode --key HEX DATA   decode one advertisement offline
smartsolar vregs [--writable]      the registers decoded over GATT, or the ones `write` can change
```

`--json` prints the raw result, `--socket` / `--config` select the daemon. Exit codes: 0 ok, 1 daemon error, 2 usage error, 3 daemon unreachable. The CLI and the daemon talk newline-delimited JSON over a Unix socket (`daemon.socket`).

## Writing

`smartsolar write NAME=VALUE ...` changes registers over GATT, through the daemon (it connects on demand in `off` and `poll` mode and uses the open link in `hold`). Only these registers can be written; everything else, including the key, the passkey and the keepalive, is refused before anything is sent:

| Name | Register | Values |
| --- | --- | --- |
| `device_mode` | `0x0200` | `on`, `off` (the charger) |
| `load_output_control` | `0xEDAB` | `off`, `on`, `alt1` |
| `float_voltage` | `0xEDF6` | 5..70 V |
| `absorption_voltage` | `0xEDF7` | 5..70 V |
| `battery_max_current` | `0xEDF0` | 0..100 A |

```
smartsolar write device_mode=off
smartsolar write float_voltage=13.6 absorption_voltage=14.4 --yes
```

It asks for confirmation on a terminal (`--yes` skips it, and is required without one). Every write is checked twice: the device must echo the value, and a read-back must show it. A write the device does not accept gets **no answer at all**, which is reported as a failure. The old and the new value are shown, and every write is logged as a `register_written` event (CSV and MQTT). Exit code 1 if a write failed or the read-back differs.

Notes: `load_output_control` `alt1` is the mode the test device was found in; after switching the load off, going back to `alt1` alone did not switch it on again, `on` then `alt1` did. The ranges of the voltages and the current are only plausibility bounds; the device decides what it accepts, and the real limits depend on the system voltage. Whether `device_mode=off` stops charging was only seen as the register value (the test happened at night).

Not exposed on purpose: battery type (it may reset other settings), restore-defaults and clear commands, and anything that could lock the device out. Other registers can be added to `WRITABLE` in `src/smartsolar/vreg.py` once their behaviour is known.

Open: the registers for battery current, yield and panel power are decoded but only their night values (zero) were seen; they need a daytime check.

## Profiles

A profile is a YAML file with a set of charge settings (`absorption_voltage`, `float_voltage`, `battery_max_current`); it never contains the charger mode or the load output. Profiles of the battery types in Victron's charge algorithm table are built in, for 12 V and 24 V systems:

```
smartsolar profile list                      the built-in profiles with their voltages
smartsolar profile show lifepo4-12v
smartsolar profile save before.yml           save the device's current settings (connects if needed)
smartsolar profile diff gel-deep-discharge-12v
smartsolar profile load gel-deep-discharge-12v   diff, confirm, write only what differs, read back and verify
smartsolar profile load before.yml -y        restore
```

| Name (12 V; `-24v` is twice the voltages) | Absorption / float | Battery types (switch position in the manual) |
| --- | --- | --- |
| `gel-long-life-12v` | 14.1 / 13.8 V | Gel long life (OPzV), Gel Exide A600, Gel MK (0) |
| `gel-deep-discharge-12v` | 14.3 / 13.8 V | Gel Victron deep discharge, Gel Exide A200, AGM Victron deep discharge, stationary tubular plate OPzS (1) |
| `victron-default-12v` | 14.4 / 13.8 V | the default algorithm of the same battery types (2) |
| `agm-spiral-cell-12v` | 14.7 / 13.8 V | AGM spiral cell, stationary tubular plate OPzS, Rolls AGM (3) |
| `pzs-tubular-1-12v` | 14.9 / 13.8 V | PzS tubular plate traction or OPzS (4) |
| `pzs-tubular-2-12v` | 15.1 / 13.8 V | PzS tubular plate traction or OPzS (5) |
| `pzs-tubular-3-12v` | 15.3 / 13.8 V | PzS tubular plate traction or OPzS (6) |
| `lifepo4-12v` | 14.2 / 13.5 V | Lithium iron phosphate (7) |

The values are from the "battery charge algorithm" table in Victron's MPPT manual (BlueSolar MPPT 100/30 and 100/50, which uses the same charge algorithms as the SmartSolar). **They are not the presets of the VictronConnect app:** the app's list for the SmartSolar was not available when this was written, so check the voltages against your battery's datasheet and the manual of your model. A profile only sets the absorption and float voltage (and the current, if the file has it). The presets also define the equalization voltage, the temperature compensation and the low-voltage limits, which `smartsolar` can not write; the device reports `battery_type` 255 (user defined) either way. `profile load` compares with the live values first, asks for confirmation (`--yes` skips it) and verifies by reading back; `--dry-run` only shows the differences.

## GATT protocol

Service `306b0001-b081-4037-83dc-e59fcc3cdfd0`, three characteristics (`0021` control, `0024` and `0027` data). Pairing: SMP passkey, default `000000`. A BlueZ agent that answers the passkey request is registered from before the connect until pairing is done (`dbus-fast`). Answers are records `[kind][ctx][CBOR register id][CBOR value]`, requests are `[0x05 get | 0x06 set][ctx][CBOR array]`; registers are read and written in context 3 (contexts 0 and 1 only answer for a few static registers). The details are in `src/smartsolar/vreg.py`, the register list is `smartsolar vregs`.

## Layout

`src/smartsolar/`: `instant.py` (Instant Readout decoding), `vreg.py` (GATT records and registers), `tracker.py` (device selection, decoding), `keys.py`, `gatt_session.py` (one link), `gatt_manager.py` (modes, retries), `daemon.py`, `cli.py`, `server.py` and `ipc.py` (control socket), `outputs/`, `ble/` (transport interface; only `ble/bleak_backend.py` and `ble/agent.py` import bleak and dbus-fast). `tests/` runs without hardware against a fake backend (`tests/fakes.py`).

## Credits

The Instant Readout format follows Victron's published documentation. The GATT protocol was derived from a Bluetooth trace of the VictronConnect app and from the community work referenced in the wiki.
