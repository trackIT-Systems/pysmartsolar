# Magic values and open questions

What is sent or accepted without being understood, so nobody has to rediscover that it is a guess.

## GATT handshake (replayed from a VictronConnect trace)

| Where | Bytes | What we know |
|---|---|---|
| control `0021` | `fa80ff` | sent first; meaning unknown |
| control `0021` | `f980` | meaning unknown |
| control `0021` | `f941` | sent last; the device starts pushing values after it |
| control `0021` (device) | `f901` | the device's acknowledgement of every control write |
| data `0024` | `01` | meaning unknown |
| data `0024` | `0300` | meaning unknown |

The whole sequence is `INIT_SEQUENCE` in `vreg.py`. **It is not known which parts are required.** The probe script (not in the repo) had `full`, `short` and `none` modes, but no result was recorded. A bisection on the device (drop parts, see whether the link and reads still work) would turn this into a list of what is needed.

## Init requests whose purpose is unknown

Inside the init sequence (the bytes are in `INIT_SEQUENCE`):

* On `0024`, in the same write as the ping and the key/MAC request (`05 00 82 19 ec66 19 ec65`, context 0), four bytes **`03 01 03 03`**: a kind `03` item whose meaning is unknown.
* Requests in **context 0** for `0x010D`, `0x0090`, `0xEC3F`, `0xEC12` and `0xEC7D`, the same four in **context 1** (`0xEC7D`, `0x0090`, `0xEC3F`, `0xEC12`) on `0027`, and `0x0100` twice in context 1. Their answers are not used; what the registers hold is unknown.
* A request of **kind `03`** on its own: `03 81 19 ed ac` (register `0xEDAC`).
* A trailing **`05`** after `05 03 81 19 ed a9` (`...eda905`).
* Context 3 requests for `0x0102`, `0xED8C`, `0xEDEC`, `0x2211`, `0x2212` and `0xEDA8` (copied; only the answers are used, they work as plain reads too).

## Contexts

Context 3 is the one where reads and writes work. Contexts 0 and 1 answer only a few static registers. What the context byte selects is not known.

## Record kinds

* `0x07` ("status") and `0x02` (handshake answer) are parsed and ignored.
* Several registers answered with `09 ... 01` in some contexts (`0xEDD5`, `0xEDD7`, `0xEDD3`, `0xEDBB`/`0xEDBC`, `0x0201`) although they exist (they answer in context 3). Why the standalone request fails is unknown.
* The meaning of the error code `01` in `09` records is a guess (not available).

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
