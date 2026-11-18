# Writing settings and profiles

`smartsolar write NAME=VALUE ...` changes settings through the daemon, over the VE.Direct cable if it is connected, else over GATT (it connects on demand in `off` and `poll` mode and uses the open link in `hold`); see [the VE.Direct cable](daemon.md#the-vedirect-cable) for `--via`. Only the settings in `smartsolar vregs --writable` can be written; everything else, including the key, the passkey, the keepalive, restore-defaults and the system voltage, is refused before anything is sent.

```
smartsolar write device_mode=off
smartsolar write float_voltage=13.6 absorption_voltage=14.4 --yes
smartsolar vregs --writable
```

| Setting | Values |
| --- | --- |
| `device_mode` | `on`, `off` (the charger) |
| `battery_type` | `user` or a factory preset (`preset1` to `preset8`) |
| `battery_max_current`, `tail_current` | A |
| `absorption_voltage`, `float_voltage`, `equalisation_voltage` | V, inside the range the device allows |
| `temperature_compensation` | mV/K, at most ±21 for a 12 V system |
| `absorption_time_limit`, `equalisation_duration` | hours |
| `adaptive_mode`, `equalisation_auto_stop` | `on`, `off` |
| `auto_equalise_days`, `equalisation_current_percent`, `rebulk_voltage_offset`, `low_temperature_level`, `low_temperature_current` (`none`: the maximum) | |
| `load_output_control` | `off`, `batterylife`, `conventional1`, `conventional2`, `on`, `user1`, `user2`, `aes` |
| `load_user_high_voltage`, `load_user_low_voltage`, `load_aes_timer` | V, V, minutes |
| `rx_port_function`, `tx_port_function` | the VE.Direct port functions |
| `instant_readout` | `on`, `off`: whether the advertisements carry the encrypted live data. A setting of the Bluetooth part, so only over GATT (`auto` takes GATT for it) |
| `custom_name` | text, up to 80 bytes UTF-8: the name VictronConnect shows |
| `bms_present` | `absent`, `present` (the charger sets `present` itself when it sees a BMS) |
| `identify` | `on`, `off`: the charger blinks its LEDs until set `off` again |

It asks for confirmation on a terminal (`--yes` skips it, and is required without one). Every write is checked twice: the device must echo the value, and a read-back must show it. A write the device does not accept gets **no answer at all**, which is reported as a failure. The old and the new value are shown, and every write is logged as a `register_written` event (CSV and MQTT). Exit code 1 if a write failed or the read-back differs.

**Safety checks before a write**, from Victron's VE.Direct protocol document: the charge voltages are only taken if `battery_type` is `user` (otherwise the write is refused with a hint; set the type first), and a voltage outside the range the device reports (8 to 17.4 V for a 12 V system) or a temperature compensation beyond ±21 mV/K makes the device raise **error 119** (settings data invalid), which only a factory reset plus a power cycle clears. `smartsolar` reads the allowed range from the device and refuses such values. The settings are stored in non-volatile memory: do not write them in a loop.

`load_output_control` is the load output algorithm of the VictronConnect load output settings: `batterylife` is the self-adapting default, `conventional1` switches off below 11.1 V and on above 13.1 V, `conventional2` below 11.8 V and above 14.0 V (12 V system; multiplied for 24 V and 48 V), `on` and `off` are always on and always off, and `user1`, `user2` and `aes` (Automatic Energy Selector) use `load_user_high_voltage` and `load_user_low_voltage` (and the AES timer). Except for `on` and `off`, the load output follows an algorithm only after a 2-minute delay. Whether `device_mode=off` stops charging was only seen as the register value (the test happened at night), and it only works while remote on/off is enabled (register `0x0202`, bit 1; set by default on firmware 1.42 and newer).

Not exposed on purpose: restore-defaults and clear commands, the system voltage, the Instant Readout key and the passkey. The streetlight timer, the relay, the Bluetooth switch (`ble_mode`: off would cut the link to the charger) and the PIN code are not covered. Other registers can be added to `WRITABLE` in `src/smartsolar/vreg.py`.

## Profiles

The folder [profiles/](../profiles/) has settings files made with VictronConnect (SmartSolar 75/10 AGM and LiFePO4, 75/15 LiFePO4). They are complete app files and contain the history and serial number of the charger they were saved from; only the `/Settings/` part is used.

A profile is a **VictronConnect settings file** (`.vcsf`): the file the app saves with "Save settings to file" and loads onto a charger of the same model. `smartsolar` reads those files and applies their settings over GATT; it can also save the settings of the charger to such a file.

```
smartsolar profile show "profiles/VE MPPT 75_15 LiFePo Profile.vcsf"   what the file sets (works without the daemon)
smartsolar profile diff FILE.vcsf                            compare with the charger
smartsolar profile load FILE.vcsf                            check, diff, confirm, write only what differs, read back
smartsolar profile load FILE.vcsf --dry-run                  the same without writing
smartsolar profile load FILE.vcsf --ignore-device            also for a file made for another product id or newer firmware
smartsolar profile save FILE.vcsf                            save the charger's settings (experimental)
```

The file is a `VCCFH` header ([vcsf-format.md](vcsf-format.md)) and a zlib-compressed JSON document with VictronConnect's model of one device: the settings under `/Settings/`, but also live values, history and the serial number. Only the `/Settings/` keys are used; the others are ignored. 22 settings are imported (the table above, except the device mode); the other `/Settings/` keys (streetlight, relay, BMS, auxiliary input) are counted and reported but not imported, because their registers were not checked yet. The mapping was checked against a SmartSolar 75/15 that was configured like the sample file (`tests/data/ve-mppt-75-15-lifepo4.vcsf`): reading the registers gave the same values for every setting of the file, and `profile diff` of the unmodified file says "nothing to change".

`profile load` refuses to write anything if the file does not fit the charger: another product id, a firmware older than the file's minimum, or a different system voltage (the file's charge voltages are for another battery voltage). For provisioning with a file made for another model, `--ignore-device` turns the product id and firmware mismatches into warnings; the system voltage mismatch always blocks, and the charger still refuses values outside its own ranges. It writes the battery type first, and if the file selects a factory preset instead of `user` it only writes the type, because the charge voltages are part of the preset. Settings the charger does not report (older firmware) are left out with a note. The same range and battery type checks as for `write` apply.

`profile save` writes the same container with the settings of the charger, the product id, the firmware as the minimum version and the system voltage. **This export is experimental:** `smartsolar` reads it back, but whether VictronConnect accepts such a file was not tested, because the file lacks the live values and history of a file made by the app. Use it to restore the charger with `profile load`, and do not rely on it for the app.
