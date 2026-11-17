# Quirks and special cases

Things that are surprising about the device, BlueZ or the Raspberry Pi, with how the daemon deals with them.

## Wi-Fi and Bluetooth share a radio

On the Raspberry Pi 3 and 5 Wi-Fi and Bluetooth share one chip. With the 2.4 GHz access point up:

* **GATT links are unusable.** Links are set up and then lost within a second: 0 usable links in 26 attempts; with the access point down 10 of 10 connected in half a second.
* **Advertisements are fine, with a passive scan.** A passive scan keeps receiving them (gaps up to 22 s). This is the main reason for using Instant Readout. An *active* scan is not fine: BlueZ reports a connectable advertisement only together with its scan response, and none arrives while the access point is up, so the device is not seen at all. The daemon therefore scans passively on BlueZ (`ble.scanning: auto`), which needs bluetoothd with `Experimental = true` (`AdvertisementMonitor1`); without it, it warns once and scans actively.
* **In an active scan, adverts that wait for a scan response are lost.** A charger with Instant Readout disabled sends connectable `ADV_IND` adverts. During an active scan the kernel reports such an advert only together with its scan response, and drops repeats from the same address. With the access point up the charger sent no scan responses at all, so BlueZ never reported it (measured: 0 scan responses and 0 reports in 40 s; with the access point down about 100 scan responses per 10 s and 201 reports). Instant Readout adverts are not affected. The daemon selects such a charger whenever one advert gets through, keeps it, and connects by BlueZ's last known device when the lookup fails. Bonding makes BlueZ keep it for good. A passive scan does not wait for scan responses and reports such a charger with every advert (see BlueZ below).
* The daemon never touches Wi-Fi. GATT work (key fetch, `hold`, `poll`, settings over Bluetooth) simply retries with backoff. For provisioning use the VE.Direct cable, which is unaffected.

## BlueZ

* **Pairing agent first.** The agent that answers the passkey request must be registered before the connection is opened. Registering it only around `pair()` gives `AuthenticationFailed`.
* **No connect during our own scan.** A GATT connect while the permanent scan runs fails with `org.bluez.Error.InProgress`. The scan is paused for each connect and restarted after it.
* **The device keeps advertising while connected.** The advert block stays current in `hold` mode.
* A scan that goes quiet (no advertisement for 3 minutes although the device is selected) is restarted by a watchdog.
* **Active scans need a random address.** For every active scan the kernel first sets a non-resolvable private address, which the controller refuses while the same system advertises (tsOS: `tsconfig-ble`). The kernel logs `Bluetooth: hci0: Opcode 0x2005 failed: -16` and scans with the public address. BlueZ restarts discovery about every 10 s, so an active scan logged this every 10 s. A passive scan sets no address.
* **A passive scan gets no name.** Victron's name (`SmartSolar HQ22294ZGEU`) is only in the scan response. BlueZ remembers names it has seen, so a device keeps its serial number once it was in an active scan; otherwise it is known by its MAC address.
* **Unchanged advertisements, passive scan.** bleak reports an advertisement when BlueZ changes a property of the device, and BlueZ changes one only when the data differs (in a passive scan the signal strength only on large swings). A charger with Instant Readout off sends the same bytes every time, so it was reported once in the life of bluetoothd, and never to a daemon started after that. BlueZ does call `DeviceFound` on the scan's advertisement monitor for every matching advert (about 4 per second); bleak only logs it. `smartsolar.ble.passive` replaces bleak's monitor class at runtime, in the daemon's process only, by one that runs bleak's advertisement callbacks from `DeviceFound`. It uses bleak internals (the same in 0.22 and 3.1); if they are missing, `ble.scanning: auto` scans actively and warns once.

## The device

* **A refused write is silent.** Accepted writes are echoed, refused ones get no answer. Always read back.
* **Error 119.** Charge voltages outside the device's own range (reported in `0x2211`/`0x2212`, 8..17.4 V on a 12 V system) and a temperature compensation beyond 21 mV/K put the charger into "invalid settings" until a factory reset and a power cycle. The daemon checks before writing; limits in the register table are only static sanity limits.
* **Presets swallow charge voltages.** With a battery type other than `user`, writes of absorption, float, equalisation voltage and temperature compensation are accepted and ignored. The daemon refuses them, and `profile load` leaves them out when the file selects a preset.
* **Load output algorithm change.** A new algorithm (`0xEDAB`) becomes active after about 2 minutes, not at once. An early test that read "alt 1 did not switch on" was this delay.
* **Not-available values.** `0x7FFF` / `0xFFFF` / `0x1FF` in advertisements; `0xFFFF` for the battery temperature without sensor and for "low temperature current: use the maximum".
* **Daytime.** Everything was tested at night or with a very dim panel (0 to 2 W). PV power, yield and battery current at real power are unchecked.
* **A new GATT connection blinks the LEDs.** The charger sets `identify` (`0x010E`) to on for about 10 s after a GATT connection is set up, then back to off (SmartSolar 75/15, firmware 1.74). A write right after connecting reports the old value as `on`, and a poll then publishes `identify=on`.
* **Charge current and battery current.** `0xEDD7` (`charge_current`) is what the charger delivers to the battery and the load output together; the battery current is channel 1 (`0xED8F`), the VE.Direct text block's `I`.

## Two interfaces, one device

* The serial number is the device id everywhere. The cable sends it (`SER#`), over Bluetooth it is the second word of the name (`SmartSolar HQ22294ZGEU`).
* With both interfaces, the BLE device with the cable's serial is followed even if another SmartSolar is stronger.
* The VE.Direct port is exclusive: no other reader may use it (the old `vedirect_dump` of mqttutil, for example).
* **What the cable does not reach.** Over HEX the charger answers every charger register except the trend and channel registers (`0xEC5A`, `0xEC89`, `0xEC8A`, `0xED8C`, `0xED8D`, `0xED8F`), the custom name (`0x010C`, firmware 1.64) and the Bluetooth part (VE.Smart instance 0: `0xEC65`, `0xEC66`, `0xEC7D`); the firmware is a command, not register `0x0102`. A register sweep of all 1662 ids takes 90 s (SmartSolar 75/15, firmware 1.64). A write of one of these over the cable is refused before anything is sent, and `auto` takes GATT.

## Daemon

* The first Instant Readout key is fetched once over GATT and stored in `daemon.state_dir` (mode 0600). If the stored key stops matching (device reset), it is dropped after 5 mismatches and fetched again. A wrong key in the config is only reported.
* MQTT topics use the serial number (`{host}/smartsolar/{serial}/csv|json|cbor`), the MAC address as 12 hex digits only if no serial is known.
