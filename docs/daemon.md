# The daemon

How `smartsolard` reads a charger, and why it does it this way. The config keys are described in [config/smartsolar.yml](../config/smartsolar.yml).

## Why advertisements, and GATT only on top

A GATT connection to a SmartSolar is unusable while the Raspberry Pi's 2.4 GHz Wi-Fi access point is up: the Wi-Fi and Bluetooth radios share one chip, and links are set up and then lost within a second (0 usable links in 26 attempts; with the access point down, 10 of 10 connected in half a second). The **Instant Readout** advertisements are not affected: the device broadcasts its main values encrypted, about 4 times a second, and a passive scan receives them reliably with the access point up (2447 of 2447 advertisements decrypted in 10 minutes). So:

* The advertisements are always the main data source. No connection, no pairing.
* GATT is optional (`gatt.mode`) for the values that are not in the advertisements (firmware, uptime, load output, ...), and it fetches the Instant Readout key once if you did not configure it.
* A charger with Instant Readout disabled can only be read over GATT. While `gatt.mode` is `off`, the daemon reads it in `gatt.fallback` (default `hold`; `off` leaves it alone). With the access point up this takes many attempts: even its advertisements are mostly not reported then (see [quirks.md](quirks.md)). Once a `hold` connection is up, it stays.

The measurements behind this, and everything else that was found out about the device, are in these pages, starting with [quirks.md](quirks.md).

## Device, sources and outputs

`smartsolard [--config FILE]`, default config `/etc/smartsolar/smartsolar.yml`, see [config/smartsolar.yml](../config/smartsolar.yml).

* **Device:** the configured `ble.address`, otherwise the strongest SmartSolar (solar charger records of other Victron devices are listed but never selected). A charger with Instant Readout disabled only sends its product id; it is recognised by that id (or its `SmartSolar` name) and selected unless both `gatt.mode` and `gatt.fallback` are `off` (or when it is pinned), but a charger with Instant Readout comes first. The selection is reviewed every `ble.rescan_interval_s`; another device must be `ble.switch_hysteresis_db` stronger to replace the current one.
* **Advertisements:** decoded as they arrive; the latest reading is published every `advert.publish_interval_s` (the device sends a new one about once a second). Fields: state, charger error, battery voltage and current, yield today, PV power, load current; values the device reports as not available are `null`.
* **GATT** (`gatt.mode`):
  * `off`: no connection, except to fetch a missing key. A charger with Instant Readout disabled is read in `gatt.fallback` (default `hold`) instead.
  * `hold`: keep a connection and publish the values the device pushes every `gatt.publish_interval_s`. A ping every `gatt.keepalive_s` keeps it open (the device closes the link 10 s after the last ping).
  * `poll`: connect every `gatt.poll_interval_s`, collect the values, publish, disconnect.

  The device keeps advertising while a GATT connection is open, so the advertisement data stays fresh in `hold` and `poll`. A charger with Instant Readout disabled may stop advertising while connected, and its adverts are mostly not reported while the access point is up; for it the scan watchdog is off, and a connect falls back to the device BlueZ knows from its last advert (for good once bonded) when the lookup does not find it again.

  While a connection is being opened the advertisement scan is paused (BlueZ rejects connects while the same client scans), so with an access point up on 2.4 GHz the connection attempts of `hold` and `poll` cost advertisement readings (in a test, no reading for the first minute while the first attempts timed out). Failed attempts are retried with a jittered, growing delay, and nothing here touches Wi-Fi. Use `gatt.mode: off` there.
* **MQTT:** every reading as CSV, JSON and CBOR on `{host}/smartsolar/{serial}/csv|json|cbor`, with the same columns whichever source it came from: `device`, then the values under their names (`battery_voltage`, `pv_power`, `yield_today`, ...). No time and nothing retained: the receiver stamps a reading on arrival. CBOR is a list in column order, about 145 bytes per reading, for cellular links. At most one reading per device every `mqtt.publish_interval_s` (300 s), the best source's newest. Events on `{host}/smartsolar/events/...`.
* **CSV:** one file per day, `/data/<host>/smartsolar/<host>_<date>_smartsolar.csv`, with `time` first and the same columns plus `block` (`advert`, `gatt` or `vedirect`). It gets every reading taken once the clock is trusted; before that, readings only go over MQTT.
* Topics, columns with units, examples and the clock handling: [outputs.md](outputs.md).

## The Instant Readout key

Each device has a 16 byte key. Either put it in the config (VictronConnect: device > settings > Product info > Instant readout via Bluetooth > Show) or leave it out: the daemon then connects once over GATT, pairs with `gatt.passkey`, reads the key (register `0xEC65`) and stores it in `daemon.state_dir` (mode 0600). That one connection needs a working link, so run it with the access point down, or retry until it works (failed attempts are retried with a growing delay). `smartsolar fetch-key` triggers it by hand. If the device is reset and the stored key stops matching, it is dropped and fetched again; a wrong key in the config is only reported (`smartsolar status`: `key ... mismatch`).

The key and the passkey are never logged and never published.

## The VE.Direct cable

Some systems have the charger on a VE.Direct cable (FTDI USB, `/dev/ttyUSBvd<n>` on tsOS), with or without Bluetooth. The daemon can use either interface or both; each is its own block of readings (`block` in the CSV): `advert`, `gatt` and `vedirect`; MQTT gets the best block's newest reading.

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

## GATT

Service `306b0001-b081-4037-83dc-e59fcc3cdfd0`, three characteristics (`0021` control, `0024` lastData, `0027` data). Pairing: SMP passkey, default `000000`. A BlueZ agent that answers the passkey request is registered from before the connect until pairing is done (`dbus-fast`). The service speaks VE.Smart, VictronConnect's protocol: CBOR messages (`[opcode][instance][arguments]`) in chunks with credit-based flow control on the control characteristic; the charger's registers are in one instance of the device's tree (3 on a SmartSolar 75/15). The protocol is in `src/smartsolar/vesmart.py`, the registers in `src/smartsolar/vreg.py`, the register list is `smartsolar vregs`. The protocol, the quirks of the device and what is still unknown are written down in these pages ([gatt-protocol.md](gatt-protocol.md)).
