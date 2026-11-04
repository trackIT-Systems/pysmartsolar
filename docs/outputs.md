# What smartsolard publishes

`smartsolard` publishes the readings of the SmartSolar it follows, and its connection events, over MQTT and into CSV files. Both use the same columns. This page is the reference for consumers: topics, encodings, columns, rate limit and files.

A reading comes from one of three sources: the Instant Readout **advertisements**, a **GATT** connection, or the **VE.Direct** cable. They report overlapping sets of values. MQTT does not say which source a reading came from; all sources share one set of columns, and a value a source does not report is null. The CSV files name the source in an extra `block` column.

## MQTT

| Topic | Payload | Retained | Rate |
|---|---|---|---|
| `{host}/smartsolar/{device}/csv` | one `;`-separated line, no header | no | at most one per `mqtt.publish_interval_s` per device |
| `{host}/smartsolar/{device}/json` | object, keys in column order | no | as above |
| `{host}/smartsolar/{device}/cbor` | array, values in column order | no | as above |
| `{host}/smartsolar/events/{csv,json,cbor}` | one connection event | no | every event |

- `{host}` is the host name.
- `{device}` is the controller's serial number (e.g. `HQ22294ZGEU`); only while none is known, the MAC address as 12 hex digits.
- `mqtt.csv`, `mqtt.json` and `mqtt.cbor` switch the formats on and off.
- QoS is `mqtt.qos` (0 by default). A message published while the broker is unreachable is dropped.
- Nothing carries a time and nothing is retained: a reading is a snapshot of now, and the receiver stamps it on arrival. A reading that arrives is current (at most `publish_interval_s` plus the source's interval old); one that does not is lost. The station's time and whether it can be trusted are in tstelemetry's `timesync` readings (tsOS-base docs/time.md).

### Encodings

All three formats of a reading carry the same columns in the same order:

1. `device`: the serial number, else the MAC address.
2. The columns listed below. A value the reading lacks is `null` (empty in CSV).

CBOR has no key names and writes each number in the smallest form that keeps it exact. That makes it the smallest format, for cellular links: a VE.Direct reading is about 145 bytes as CBOR and 135 as CSV. JSON repeats all 54 column names, null or not, and is about 1.5 kB. Decode a CBOR array by the column order below; the files have the same order after `time`, with `block` after `device`.

A VE.Direct reading as CSV:

```
HQ22294ZGEU;bulk;3;none;0;13.32;-0.01;;24.01;2;0.0;on;;;0.01;0.0;98.47;2;0;;mpp tracker active;none;;343;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
```

and the start of the same reading as JSON:

```json
{"device": "HQ22294ZGEU", "device_state": "bulk", "device_state_code": 3, "charger_error": "none", "charger_error_code": 0, "battery_voltage": 13.32, "battery_current": -0.01, "battery_temperature": null, "panel_voltage": 24.01, "pv_power": 2, "load_current": 0.0, "load_output": "on", "…": "…"}
```

### Rate limit

`mqtt.publish_interval_s` (300 s by default) allows at most one MQTT reading per device in that time.
- The first reading goes out at once.
- Readings within the interval are held back and the best one goes out when the interval ends. VE.Direct beats GATT, which beats the advertisements; within one source the newest wins.
- When the interval is over, a reading goes out at once, unless a better source has delivered within the last interval. Then it waits for that source's next reading, which replaces it. Two sources running side by side therefore do not alternate between a full and a sparse row.
- Nothing is sent twice. With `0`, every reading goes out.

The sources hand their newest reading to the outputs every `advert.publish_interval_s` and `serial.publish_interval_s` (60 s); GATT readings arrive every `gatt.poll_interval_s` in `poll` mode, every `gatt.publish_interval_s` in `hold` mode. Events are not limited, and neither are the CSV files: they get every reading handed to the outputs.

### Events

Columns: `device` (or null), `event`, `detail`; the files add `time` first. `detail` is the event's data as `key=value` pairs, e.g. `name=float_voltage old=13.5 new=13.51 requested=13.51 ok=True`.

Events include `daemon_started`, `device_selected`, `device_switched`, `connected`, `disconnected`, `paired`, `gatt_failed`, `key_fetched`, `key_rejected`, `register_written`, `serial_opened`, `serial_lost`, `serial_device`, `scan_restarted` and `daemon_stopped`.

### Bridging over cellular

Forward the CBOR readings and events; leave CSV and JSON for local consumers:

```
topic +/smartsolar/+/cbor out 1
```

This matches the readings and `events/cbor`, which are on the same level. There is no state topic: a controller or daemon that stops shows as missing readings, and the connection's changes are events.

## Columns

Measurements first, then the settings and identity values (mostly from GATT). Where the sources disagree, the column has one name and one unit:
- GATT's `panel_power` is published as `pv_power`, and `load_output_current` as `load_current`.
- `yield_today` is in kWh; the advertisement's Wh are converted.

| Column | Unit | Meaning | Sources |
|---|---|---|---|
| `device_state` |  | Device state | VE.Direct, GATT, advertisement |
| `device_state_code` |  | Device state code | VE.Direct, advertisement |
| `charger_error` |  | Charger error | VE.Direct, GATT, advertisement |
| `charger_error_code` |  | Charger error code | VE.Direct, advertisement |
| `battery_voltage` | V | Battery voltage | VE.Direct, GATT, advertisement |
| `battery_current` | A | Battery current | VE.Direct, GATT, advertisement |
| `battery_temperature` | °C | Battery temperature | GATT |
| `panel_voltage` | V | Panel voltage | VE.Direct, GATT |
| `pv_power` | W | PV power | VE.Direct, GATT, advertisement (GATT: `panel_power`) |
| `load_current` | A | Load current | VE.Direct, GATT, advertisement (GATT: `load_output_current`) |
| `load_output` |  | Load output | VE.Direct |
| `load_output_status` |  | Load output status | GATT |
| `load_output_voltage` | V | Load output voltage | GATT |
| `yield_today` | kWh | Yield today | VE.Direct, GATT, advertisement (advertisement: Wh, converted) |
| `yield_yesterday` | kWh | Yield yesterday | VE.Direct, GATT |
| `yield_total` | kWh | Yield total | VE.Direct, GATT |
| `max_power_today` | W | Maximum power today | VE.Direct, GATT |
| `max_power_yesterday` | W | Maximum power yesterday | VE.Direct, GATT |
| `charger_temperature` | °C | Charger temperature | GATT |
| `tracker_mode` |  | Tracker mode | VE.Direct |
| `off_reason` |  | Off reason | VE.Direct |
| `device_mode` |  | Device mode | GATT |
| `day_sequence` |  | Day sequence number | VE.Direct |
| `mac_address` |  | MAC address | GATT |
| `firmware_version` |  | Firmware version | GATT |
| `uptime` | s | Uptime | GATT |
| `channel_1_voltage` | V | Channel 1 voltage | GATT |
| `channel_1_current` | A | Channel 1 current | GATT |
| `load_output_offset_voltage` | V | Load output offset voltage | GATT |
| `adjustable_voltage_min` | V | Adjustable voltage minimum | GATT |
| `adjustable_voltage_max` | V | Adjustable voltage maximum | GATT |
| `battery_voltage_setting` | V | Battery voltage setting | GATT |
| `battery_type` |  | Battery type | GATT |
| `battery_max_current` | A | Battery max current | GATT |
| `absorption_voltage` | V | Absorption voltage | GATT |
| `float_voltage` | V | Float voltage | GATT |
| `equalisation_voltage` | V | Equalisation voltage | GATT |
| `temperature_compensation` | mV/K | Temperature compensation | GATT |
| `absorption_time_limit` | h | Absorption time limit | GATT |
| `adaptive_mode` |  | Adaptive mode | GATT |
| `auto_equalise_days` | days | Auto equalise days | GATT |
| `equalisation_current_percent` | % | Equalisation current percent | GATT |
| `equalisation_duration` | h | Equalisation duration | GATT |
| `equalisation_auto_stop` |  | Equalisation auto stop | GATT |
| `rebulk_voltage_offset` | V | Rebulk voltage offset | GATT |
| `tail_current` | A | Tail current | GATT |
| `low_temperature_level` | °C | Low temperature level | GATT |
| `low_temperature_current` | A | Low temperature current | GATT |
| `load_output_control` |  | Load output control | GATT |
| `load_user_high_voltage` | V | Load user high voltage | GATT |
| `load_user_low_voltage` | V | Load user low voltage | GATT |
| `load_aes_timer` | min | Load aes timer | GATT |
| `rx_port_function` |  | Rx port function | GATT |
| `tx_port_function` |  | Tx port function | GATT |

## CSV files

- `<csv.path>/<host>/smartsolar/<host>_<YYYY-MM-DD>_smartsolar.csv`: one file per calendar day (local time) with the readings of all sources. Its columns are `time` (when the source delivered the reading, whole seconds, ISO 8601 with the station's UTC offset), then those of MQTT with `block` (`advert`, `gatt` or `vedirect`) after `device`.
- `<csv.path>/<host>/smartsolar/events/<host>_<YYYY-MM-DD>_smartsolar-events.csv`: the events `connected`, `disconnected`, `device_selected`, `device_switched`, `key_fetched`, `key_rejected`, `paired`, `register_written`, `serial_opened`, `serial_lost` and `serial_device`.

The files are `;` separated with a header row at the top, and are only appended to, never rotated or deleted.

## Clock

A station can run with an untrusted clock for a while (no network, no GNSS, no checked RTC), and `smartsolard` does not wait for it. Rows for the files need a time that can be trusted, so they are only written once the clock is:

- The daemon reads the kernel's clock state with `adjtimex` (read-only, no privileges). chrony clears `STA_UNSYNC` once it has a source: NTP, GNSS, or the RTC that tsscheduled checked (tsOS-base docs/time.md). Once seen synced, the clock counts as synced for the rest of the daemon's run. Where there is no `adjtimex` (macOS, Windows), the clock always counts as synced.
- Before that, readings only go over MQTT; the files get nothing. The start and the end of this are logged once at INFO.
