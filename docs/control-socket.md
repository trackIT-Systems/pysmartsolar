# Control socket API

`smartsolard` serves a small request/response API on a Unix socket (`daemon.socket`, default `/run/smartsolar/smartsolar.sock` on Linux). The `smartsolar` CLI is only a client of it; anything else that can write a line of JSON can be one too. Where there are no usable Unix sockets (Windows) the endpoint is a loopback TCP address, `tcp://127.0.0.1:47821`; the protocol is the same. See [platforms.md](platforms.md).

**There is no authentication.** Whoever can reach the socket can read the values and **write settings to the charger** (`write`). On Unix the file permissions are the only protection (`daemon.socket_mode`, 0660 on tsOS); a TCP endpoint is open to every local user.

The API is covered by semantic versioning (see [releasing.md](releasing.md)): commands, arguments and result fields are not removed or renamed within a major version; new fields and commands can appear, so ignore keys you do not know.

## Protocol

One JSON object per line (UTF-8, terminated by `\n`), in both directions.

```
request   {"id": 1, "cmd": "read", "args": {"block": "vedirect"}}
success   {"id": 1, "ok": true,  "result": {...}}
failure   {"id": 1, "ok": false, "error": {"type": "no_data", "message": "..."}}
```

* `id` is any JSON value; the daemon echoes it. `args` is optional (an object).
* A connection can carry several requests, answered in order, one at a time. The daemon closes a connection that is idle for 30 s. A request line may be at most 64 KB (`bad_request` if longer), a response can be up to 16 MB (`read --raw` is the largest).
* Some commands take long (see below): a client should wait minutes, not seconds. The CLI waits 180 to 300 s for `write`, `settings` and `fetch_key`.
* Invalid JSON, a missing `cmd` or `args` that is not an object give `bad_request`; an unknown command `unknown_command`.

From a shell: `echo '{"cmd":"status"}' | socat - UNIX-CONNECT:/run/smartsolar/smartsolar.sock`. From Python, `smartsolar.ipc.request(socket, cmd, args, timeout=60)` returns the `result` or raises `DaemonError` (with `.type` and `.message`) or `DaemonUnreachable`.

## Errors

`error.type` is one of:

| type | meaning | CLI exit code |
|---|---|---|
| `bad_request` | malformed request, unknown block, bad argument | 1 |
| `unknown_command` | no such command | 1 |
| `not_connected` | the link needed for the request is not open (no GATT connection, serial port closed, `serial.hex` off) | 1 |
| `no_data` | there is nothing (recent) to return, the interface is disabled or no way to the registers exists | 1 |
| `invalid_write` | the write was refused before anything was sent (unknown or read-only register, value out of range, error 119 protection) | 1 |
| `write_failed` | the device did not confirm the write or reads a different value back | 1 |
| `protocol_error` | the device sent something unusable, or did not answer in time | 1 |
| `timeout` | the request ran into a timeout inside the daemon | 1 |
| `internal` | a bug; the daemon logs a traceback | 1 |

CLI exit codes: 0 ok, 1 the daemon answered with an error, 2 usage error (including `invalid_write` found by the CLI itself before sending, and profile problems), 3 the daemon is unreachable.

## Commands

### `status`

No arguments. The state of everything the daemon does, without any radio or cable traffic.

```json
{"version": "1.2.0", "uptime_s": 1, "id": "HQ00000TEST", "ble": true,
 "serial": {"port": "/dev/ttyUSBvd*", "state": "open", "connected": true, "hex": true, "serial": "HQ00000TEST",
            "product_id": "0xA053", "firmware": "1.64", "last_block_age_s": 0.0, "opens": 1, "losses": 0,
            "blocks": 120, "bad_checksum": 1, "resyncs": 0, "hex_frames": 0, "bad_hex": 0, "hex_requests": 0,
            "hex_timeouts": 0, "last_error": null, "backoff_s": null, "unknown_fields": {}},
 "device": {"address": "F7:48:18:8A:77:9D", "name": "SmartSolar HQ00000TEST", "rssi": -60, "model": "0xa053",
            "pinned": false, "key": {"source": "config", "state": "ok"}, "last_advert_age_s": 0.0, "adverts_per_min": 238.2},
 "devices_seen": 1,
 "blocks": {"advert":   {"enabled": true, "stale_after_s": 120, "last_ok": "2026-10-14T09:12:29.429Z", "age_s": 0.3, "stale": false, "last_error": null},
            "gatt":     {"enabled": false, ...},
            "vedirect": {"enabled": true, ...}},
 "advert": {"seen": 1200, "decoded": 1195, "no_key": 0, "rejected": 5, "undecodable": 0},
 "gatt": {"mode": "off", "state": "idle", "connected": false, "connected_since": null, "connect_attempts": 0,
          "connects": 0, "disconnects": 0, "pairings": 0, "polls": 0, "last_poll": null, "next_poll_in_s": null,
          "last_error": null, "backoff_s": null, "last_key_fetch": null}}
```

* `id` is the device as readings call it: the serial number, else the MAC address. `ble` is false when `ble.enabled` is off; `serial` is `null` when the cable is not enabled; `device` is `null` until a device is selected.
* `device.key.source` is `config`, `fetched` or `missing`; `state` is `ok`, `mismatch` or `unknown`. The key itself is never returned.
* `blocks.<name>.enabled` is false for an interface that is not in use (never stale); `stale` is true when the age exceeds `stale_after_s` or there is no reading yet. Times are UTC ISO 8601.

### `cached`

`{"block": "all" | "advert" | "gatt" | "vedirect"}` (default `all`). The last readings the daemon has, without touching the device.

```json
{"device": "HQ00000TEST",
 "blocks": {"advert": {"enabled": true, "time": "2026-10-14T09:12:29.429Z", "age_s": 0.3, "stale": false,
                       "values": [{"name": "battery_voltage", "friendly": "Battery voltage", "unit": "V", "value": 13.32}, ...]}}}
```

A value that the device reports as not available is `null`. `time` and `age_s` are `null` before the first reading.

### `read`

`{"block": "all" | "advert" | "gatt" | "vedirect", "raw": false}`. A fresh reading, or an error when there is none:

* `advert`: the newest decoded advertisement; `no_data` if none arrived within `advert.stale_after_s` (key known? device in range?).
* `vedirect`: the newest valid text block of the cable; `no_data` when the cable is disabled or silent.
* `gatt`: in `hold` mode what the open link has received (`not_connected` without a link); in `poll` mode it connects now and waits, which can take `connect_timeout_s + pair_timeout_s + collect_s + 30` seconds; `no_data` in `off` mode.

```json
{"device": "HQ00000TEST", "blocks": {"vedirect": {"time": "2026-10-14T09:12:29.429Z", "values": [{"name": "...", "friendly": "...", "unit": "...", "value": ...}]}}}
```

With `block: "all"` a block that cannot be read has `{"error": "..."}` instead of `time` and `values`. With `raw: true` and a GATT block the result also has `"raw": {"0xEDF6": "4605", ...}`, every register received (the key excluded) as hex.

### `scan`, `rescan`

No arguments. `scan` lists the Victron devices seen in the advertisements; `rescan` chooses the device again now (without hysteresis) and then lists them. Both need Bluetooth (`no_data` if `ble.enabled` is false).

```json
{"devices": [{"address": "F7:48:18:8A:77:9D", "name": "SmartSolar HQ00000TEST", "rssi": -60, "adverts": 1200, "age_s": 0.3,
              "model": "0xa053", "record_type": 1, "solar_charger": true, "key": "config", "current": true}]}
```

### `reconnect`

No arguments. Drops the GATT connection and connects again. Returns `{"requested": true}` at once; `no_data` if `gatt.mode` is `off` or Bluetooth is disabled.

### `fetch_key`

`{"show": false}`. Reads the Instant Readout key over GATT (connecting and pairing if needed) and stores it in `daemon.state_dir`. Can take up to `connect_timeout_s + pair_timeout_s + request_timeout_s + 30` seconds. Returns `{"address": "...", "fetched": true}`, plus `"key": "<32 hex>"` only with `"show": true`.

### `settings`

`{"names": [...], "via": "auto" | "serial" | "gatt"}`. Every writable setting (or the listed ones, names as in `smartsolar vregs --writable`), read live from the charger, with what a profile is checked against. Needs a way to the registers: the VE.Direct cable with `serial.hex`, or Bluetooth (which connects on demand).

```json
{"device": "HQ00000TEST", "via": "serial", "product_id": 41043, "firmware": 356, "system_voltage": 12,
 "voltage_range": [8.0, 17.4],
 "values": [{"name": "float_voltage", "friendly": "Float voltage", "unit": "V", "value": 13.5}, ...]}
```

`product_id` is the number (`0xA053` is 41043) and `firmware` the version as VictronConnect shows it (`0x0164` is 356, i.e. 1.64); either can be `null`. Settings this model or firmware does not have are left out. `via` says which way answered.

### `write`

`{"values": {"float_voltage": 13.8, ...}, "via": "auto" | "serial" | "gatt"}`. Writes the registers in the order given, each one checked first, read back afterwards. **This changes the charger's non-volatile settings.**

```json
{"device": "HQ00000TEST", "via": "serial",
 "written": [{"name": "float_voltage", "requested": 13.8, "old": 13.5, "value": 13.8, "ok": true}]}
```

* Names and values are validated before anything is sent (`invalid_write`); the writable names are in `smartsolar vregs --writable` and in the error message for an unknown one. Named values are strings (`"load_output_control": "conventional2"`), numbers are in the unit of the setting.
* Before each write the daemon refuses values the device would turn into error 119 (`invalid_write`), charge voltages while the battery type is not `user`, and, over the cable, the VE.Direct port functions (`write_failed`).
* The sequence stops at the first value that does not read back as written (`write_failed`); the ones before it stay written. `value` is what the device reports after the write, `ok` whether it equals the request.
* A write is also published as a `register_written` event (MQTT `events`, CSV).

### The `via` argument

`auto` (the default, `daemon.registers`) takes the VE.Direct cable if it is connected and `serial.hex` is on, and falls back to Bluetooth if that fails for lack of a link or an answer. `serial` and `gatt` insist on one way. An error from `auto` names every way that was tried.
