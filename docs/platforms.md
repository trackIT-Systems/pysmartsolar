# Platforms

`smartsolard` and `smartsolar` are written for the Raspberry Pi (tsOS, Linux), but the code is portable: Bluetooth goes through [bleak](https://github.com/hbldh/bleak) (BlueZ on Linux, CoreBluetooth on macOS, WinRT on Windows) and the cable through pyserial. This page says what differs. The tests run on all three systems in CI against fakes; **only Linux (Raspberry Pi 5) was tried with a real charger**. Treat macOS and Windows as unverified until someone has run them with hardware.

| | Linux (tsOS) | macOS | Windows |
|---|---|---|---|
| VE.Direct cable | yes | yes (`/dev/cu.usbserial-*`) | yes (`COM3`) |
| Instant Readout (advertisements) | yes | should work | should work |
| GATT (hold, poll, settings, key fetch) | yes | should work | should work |
| Pairing with the passkey | automatic (BlueZ agent) | the system asks | the system asks |
| Device names in the config | MAC, serial | UUID, serial | MAC, serial |
| Control socket | Unix socket | Unix socket | `tcp://127.0.0.1:47821` |
| Default paths | `/boot/firmware`, `/var/lib/smartsolar`, `/run/smartsolar`, `/data` | `~/Library/Application Support/smartsolar` | `%PROGRAMDATA%\smartsolar` |

An example config for macOS with the paths and notes below is [config/smartsolar.macos.yml](../config/smartsolar.macos.yml). `~` is expanded in `daemon.socket`, `daemon.state_dir` and `daemon.csv.path`.

## Bluetooth

* **Pairing.** Only BlueZ lets a program answer the passkey request, so the D-Bus agent (`smartsolar.ble.agent`, `dbus-fast`) exists on Linux only. On Windows the library asks the system to pair and Windows shows its dialog; on macOS CoreBluetooth cannot be told to pair at all, the system asks the first time an encrypted characteristic is used. In both cases `gatt.passkey` is not used: type the passkey (default `000000`) in the system dialog. The key fetch (`fetch-key`) therefore needs somebody at the machine once.
* **Device identity.** macOS hides MAC addresses: devices show up with a UUID that is only valid on that Mac. Name devices by **serial number** (`HQ22294ZGEU`, part of the BLE name) in `ble.address` and in `keys:`; that works on every system, and keys fetched over GATT are stored under the serial number for the same reason. MAC addresses and UUIDs keep working where they exist.
* **`ble.adapter`** selects a BlueZ adapter (`hci0`) and is ignored elsewhere (with a warning).
* The Wi-Fi/Bluetooth coexistence problem of the Raspberry Pi ([quirks.md](quirks.md)) does not apply to PCs with separate radios; GATT should be much more reliable there.

## Control socket

CLI and daemon talk newline-delimited JSON. On Linux and macOS that is a Unix socket with file permissions (`daemon.socket_mode`). Windows has no usable Unix sockets in asyncio, so the default there is loopback TCP, `daemon.socket: tcp://127.0.0.1:47821` (`smartsolar --socket tcp://127.0.0.1:47821 status`). Only IPv4 loopback addresses are accepted. There is **no authentication**: every local user can read values and write settings. Use a Unix socket where there is one; on a shared Windows machine think twice.

## Smaller differences

* The daemon stops on SIGTERM, SIGINT (Ctrl+C) and, on Windows, Ctrl+Break.
* Serial ports are opened exclusively on Linux and macOS; Windows opens COM ports exclusively anyway.
* Fetched keys are written with mode 0600; Windows ignores the mode, the file is as private as the folder (`%PROGRAMDATA%` is readable by all users, so set `daemon.state_dir` to a folder of your own).
* There is no systemd unit or boot configuration outside tsOS; run the daemon as a service of your choice (launchd, Windows Service, NSSM, ...).
