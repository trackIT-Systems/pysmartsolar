# Quirks and special cases

Things that are surprising about the device, BlueZ or the Raspberry Pi, with how the daemon deals with them.

## Wi-Fi and Bluetooth share a radio

On the Raspberry Pi 3 and 5 Wi-Fi and Bluetooth share one chip. With the 2.4 GHz access point up:

* **GATT links are unusable.** Links are set up and then lost within a second: 0 usable links in 26 attempts; with the access point down 10 of 10 connected in half a second.
* **Advertisements are fine.** A passive scan keeps receiving them (gaps up to 22 s). This is the main reason for using Instant Readout.
* The daemon never touches Wi-Fi. GATT work (key fetch, `hold`, `poll`, settings over Bluetooth) simply retries with backoff. For provisioning use the VE.Direct cable, which is unaffected.

## BlueZ

* **Pairing agent first.** The agent that answers the passkey request must be registered before the connection is opened. Registering it only around `pair()` gives `AuthenticationFailed`.
* **No connect during our own scan.** A GATT connect while the permanent scan runs fails with `org.bluez.Error.InProgress`. The scan is paused for each connect and restarted after it.
* **The device keeps advertising while connected.** The advert block stays current in `hold` mode.
* A scan that goes quiet (no advertisement for 3 minutes although the device is selected) is restarted by a watchdog.

## The device

* **A refused write is silent.** Accepted writes are echoed, refused ones get no answer. Always read back.
* **Error 119.** Charge voltages outside the device's own range (reported in `0x2211`/`0x2212`, 8..17.4 V on a 12 V system) and a temperature compensation beyond 21 mV/K put the charger into "invalid settings" until a factory reset and a power cycle. The daemon checks before writing; limits in the register table are only static sanity limits.
* **Presets swallow charge voltages.** With a battery type other than `user`, writes of absorption, float, equalisation voltage and temperature compensation are accepted and ignored. The daemon refuses them, and `profile load` leaves them out when the file selects a preset.
* **Load output algorithm change.** A new algorithm (`0xEDAB`) becomes active after about 2 minutes, not at once. An early test that read "alt 1 did not switch on" was this delay.
* **Not-available values.** `0x7FFF` / `0xFFFF` / `0x1FF` in advertisements; `0xFFFF` for the battery temperature without sensor and for "low temperature current: use the maximum".
* **Daytime.** Everything was tested at night or with a very dim panel (0 to 2 W). PV power, yield and battery current at real power are unchecked.

## Two interfaces, one device

* The serial number is the device id everywhere. The cable sends it (`SER#`), over Bluetooth it is the second word of the name (`SmartSolar HQ22294ZGEU`).
* With both interfaces, the BLE device with the cable's serial is followed even if another SmartSolar is stronger.
* The VE.Direct port is exclusive: no other reader may use it (the old `vedirect_dump` of mqttutil, for example).

## Daemon

* The first Instant Readout key is fetched once over GATT and stored in `daemon.state_dir` (mode 0600). If the stored key stops matching (device reset), it is dropped after 5 mismatches and fetched again. A wrong key in the config is only reported.
* MQTT topics use the serial number (`{host}/smartsolar/{serial}/csv|json|cbor`), the MAC address as 12 hex digits only if no serial is known.
