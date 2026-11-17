# Magic values and open questions

What is sent or accepted without being understood, so nobody has to rediscover that it is a guess.

## GATT (VE.Smart)

The handshake that used to be replayed from a VictronConnect trace is understood now ([gatt-protocol.md](gatt-protocol.md)): `fa80ff` opens the transport, `f9 n` is flow control (`f980` allows the device 0x80 chunks, `f941` gives 65 back, `f901` is the device allowing one more), `01` asks for the device list, `03 i` subscribes to instance `i`, and the "context" byte is the instance. Still open:

* The meaning of `FA 80 FF` beyond "open"; VictronConnect sends these constant bytes.
* The `flags` byte of the parameters (0 on the SmartSolar).
* After the parameters, VictronConnect queues a `SetValues` of register 0 with `maxAttLength - 6` zero bytes when `maxAttLength` is more than 29, equals `chunkSize` and `maxFreeChunks` is more than 1 (a check of the ATT length?). The SmartSolar does not meet the condition; the daemon does not send it.
* Instance 1 (product id `0xFE00`) of the SmartSolar answers only `0x0100`.
* Error codes after `F7` and the result codes of `Response` and `ValueResponse` other than 0 and 1.
* Whether a device closes a link that never got a keepalive.
* A refused write (a read-only register) still gets no answer at all; whether a `Response` comes for other refusals is not known.
* Firmware older than 1.64 (pysmartsolar#6, `dev-vhftest-02`) was not tried with VE.Smart yet.

## Advertisements

* The byte after the `10` prefix is `02` in every sample; not interpreted.
* The upper 7 bits of the load current word are not used.
* Record types other than `0x01` are not decoded.

## `.vcsf` files

* The byte `0x48` after the magic `VCCF` is constant in the files seen; it may be a version or a flag.
* Whether VictronConnect accepts a file written by `profile save` has not been tested.
* Settings of the streetlight, relay and BMS groups are not mapped. Battery presets of the app are not in the file as values.

## Unverified encodings

* **Temperature compensation** (`0xEDF2`): scale 0.01 mV/K from the VE.Direct document; only the value 0 was seen on the device.
* Several read-only registers are marked unverified in `smartsolar vregs` (battery current, yields, charger temperature, panel power) because they were seen only with 0 or constant values.
* The device mode write (`0x0200`) needs the remote on/off bit (`0x0202`); not tested.

## Not done

Pi 3 tests, long runs (hours or days), daytime values with real PV power, other Victron devices and record types.
