# smartsolar

[![Tests](https://github.com/trackIT-Systems/pysmartsolar/actions/workflows/ci.yml/badge.svg)](https://github.com/trackIT-Systems/pysmartsolar/actions/workflows/ci.yml)

A daemon and a CLI for Victron SmartSolar charge controllers over Bluetooth Low Energy, for autonomous sensor nodes (Raspberry Pi). `smartsolard` follows one SmartSolar permanently and reports its values to a CSV file and to MQTT (JSON and CBOR). `smartsolar` talks to the running daemon: cached and live values, key, reconnect, scan.

> **Caution:** the GATT part of this project is based on reverse engineering. It reads values only and **does not write to the device** (writing is not implemented, see below).

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
smartsolar fetch-key [--show]      read the Instant Readout key over GATT now
smartsolar decode --key HEX DATA   decode one advertisement offline
smartsolar vregs                   the registers decoded over GATT
```

`--json` prints the raw result, `--socket` / `--config` select the daemon. Exit codes: 0 ok, 1 daemon error, 2 usage error, 3 daemon unreachable. The CLI and the daemon talk newline-delimited JSON over a Unix socket (`daemon.socket`).

## Not implemented

**Writing to the device is not implemented** (`smartsolar write` says so). The GATT protocol has a set request (kind `0x06`, used for the keepalive), but which registers can be written and how, and what a write does to the device, is not known. It stays open.

Also open: registers for battery current, yield, panel voltage/power and charger state over GATT did not answer in the tests (the advertisements carry those values anyway), and only night values were verified so far (PV power, yield, charger state need a daytime check).

## GATT protocol

Service `306b0001-b081-4037-83dc-e59fcc3cdfd0`, three characteristics (`0021` control, `0024` and `0027` data). Pairing: SMP passkey, default `000000`. A BlueZ agent that answers the passkey request is registered only while pairing (`dbus-fast`). Records are `[kind][ctx][CBOR register id][CBOR value]`; the details are in `src/smartsolar/vreg.py`, the register list is `smartsolar vregs`.

## Layout

`src/smartsolar/`: `instant.py` (Instant Readout decoding), `vreg.py` (GATT records and registers), `tracker.py` (device selection, decoding), `keys.py`, `gatt_session.py` (one link), `gatt_manager.py` (modes, retries), `daemon.py`, `cli.py`, `server.py` and `ipc.py` (control socket), `outputs/`, `ble/` (transport interface; only `ble/bleak_backend.py` and `ble/agent.py` import bleak and dbus-fast). `tests/` runs without hardware against a fake backend (`tests/fakes.py`).

## Credits

The Instant Readout format follows Victron's published documentation. The GATT protocol was derived from a Bluetooth trace of the VictronConnect app and from the community work referenced in the wiki.
