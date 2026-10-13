# VictronConnect settings files (`.vcsf`)

The file VictronConnect saves with "Share settings" / "Save settings to file". `smartsolar profile show|diff|load|save` reads and writes them.

## Container

```
"VCCF" | 0x48 | u32 big-endian size of the JSON | zlib stream of a JSON document
```

The byte `0x48` after the magic is constant in the files seen (see [unknowns.md](unknowns.md)). The reader refuses bad magic, a wrong size, bad zlib data or JSON, and limits the sizes (8 MB of JSON, 4 MB file) against decompression bombs.

## JSON

```json
{"appName": "...", "appversion": "...", "productid": "0xA053", "minfwversion": "0x163",
 "structureversion": 1, "description": "...", "data": {"/Settings/FloatVoltage": 13.5, ...}}
```

`data` is a flat dictionary of about 1350 keys of the device's data model; about 280 have values. It also holds live values, history and the serial number. Only keys under `/Settings/` are used. `minfwversion` and `productid` drive the compatibility check of `profile load` (`--ignore-device` turns a mismatch into a warning).

## Mapping (verified on the device)

All 22 mapped keys matched the registers read from the charger (see `Writable.vcsf` in `vreg.py` for the full list), e.g. `/Settings/FloatVoltage` = register `0xEDF6` in 0.01 V, `/Settings/BatteryType` = `0xEDF1`, `/Settings/Load/UserHighVoltage` = `0xED9D`. A file made from the same configuration as the device gives "nothing to change".

## Not imported

Streetlight, relay and BMS settings are counted and reported, not written (their registers are not checked yet). The battery presets of the app (named profiles such as "LiFePO4") are not in the file as values: a file with a preset type only sets the type.

## Export

`profile save` writes the same container with the 22 settings, the product id, the firmware as minimum version and the system voltage. It is read back by `smartsolar`; whether VictronConnect accepts such a file was **not tested** (the file lacks the live values and history of an app-made file).
