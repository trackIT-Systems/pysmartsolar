# Magic values and open questions

What is sent or accepted without being understood, so nobody has to rediscover that it is a guess.

## GATT (VE.Smart)

The protocol is understood ([gatt-protocol.md](gatt-protocol.md)), including the opcodes, the flags byte, the ValueResponse results and VictronConnect's handling of `F7`. Still open:

* The meaning of `FA 80 FF` beyond "open"; VictronConnect sends these constant bytes and does not name them.
* The names of the `F7` error codes; VictronConnect only singles out `3` (it drops the link).
* Whether a write of a value out of the device's range gets an answer (a read-only or unknown register does).
* The path interface (opcodes 10 to 16) is only known from VictronConnect: no device with it was seen.
* Only firmware 1.64 and 1.74 were tried (SmartSolar 75/15).

## Advertisements

* Bit 7 of the byte after the `10` prefix: VictronConnect keeps it per device without naming it.
* The upper 7 bits of the load current word are not used.
* Record types other than `0x01` are not decoded.

## `.vcsf` files

* Whether VictronConnect accepts a file written by `profile save` has not been tested.
* Settings of the streetlight and relay groups are not mapped. Battery presets of the app are not in the file as values.

## Unverified encodings

* **Temperature compensation** (`0xEDF2`): scale 0.01 mV/K from the VE.Direct document; only the value 0 was seen on the device.
* The device mode write (`0x0200`) needs the remote on/off bit (`0x0202`); not tested.

## Not done

Pi 3 tests, long runs (hours or days), daytime values with real PV power, other Victron devices and record types.
