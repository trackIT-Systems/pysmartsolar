# Instant Readout (advertisements)

The charger broadcasts its main values in the manufacturer data (company id `0x02E1`) of its BLE advertisements, about 4 times a second, new values about once a second. The payload is encrypted with a per-device 16 byte key. No connection or pairing is needed.

## Layout

```
10 02 | model u16 LE | record type u8 | nonce u16 LE | key check u8 | ciphertext
```

* `10` is the prefix, the byte after it (`02`) is not interpreted (see [unknowns.md](unknowns.md)).
* `model` is `0xA053` for the SmartSolar 75/15 (the product id).
* `record type` `0x01` is a solar charger. Other types (battery monitors, inverters, ...) exist; they are listed by `smartsolar scan` but never decoded.
* `key check` is the first byte of the key. It is only a quick test for a wrong key (1 in 256 false positives), not authentication.
* The ciphertext is AES-128-CTR. The counter block is the nonce as a 16 byte little-endian integer. The nonce is a counter that the device increments for every new reading; repeated nonces are repeated readings.

## Solar charger record (decrypted)

```
state u8 | charger error u8 | battery voltage s16 (0.01 V) | battery current s16 (0.1 A)
| yield today u16 (10 Wh) | PV power u16 (W) | load current u9 (0.1 A, low 9 bits)
```

"Not available" sentinels: voltage and current `0x7FFF`, yield and PV power `0xFFFF`, load current `0x1FF`. The state and error numbers are the VE.Direct ones (same table as the `CS` and `ERR` fields of the cable).

The upper 7 bits of the load current word are not used by the decoder and are not understood.

## Not in the advertisement

Panel voltage, charger temperature, total yield, yesterday's values, firmware, load output state and everything else need GATT or the VE.Direct cable.

## References

* Victron's published description of the Instant Readout advertisements (manufacturer data, company id `0x02E1`).
* The community project [victron-ble](https://github.com/keshavdv/victron-ble) decodes the same records for many Victron devices and is a cross-reference for the encryption layout. `smartsolar` uses its own code and only handles the solar charger record.

## Measured

* With the Wi-Fi access point of the Raspberry Pi up, advertisements keep arriving: 2447 of 2447 decrypted in 10 minutes, gaps up to 22 s. This is why they are the main data path.
* The device keeps advertising while a GATT link is open, so the advert block does not go stale in `hold` mode.
* The key can be read over GATT (register `0xEC65`) after pairing, see [gatt-protocol.md](gatt-protocol.md).
