"""BleBackend on top of bleak: BlueZ (Linux, the Pi), CoreBluetooth (macOS), WinRT (Windows).

Only API that exists in bleak 0.22 (Debian) and 1.x is used.

Platform differences, all handled here:

* Only BlueZ lets us answer the passkey request (a D-Bus agent, Linux only). On macOS and
  Windows the operating system pairs by itself and shows its own dialog the first time an
  encrypted characteristic is used; the passkey from the config is not used there.
* macOS gives devices a per-host UUID instead of a MAC address (``address`` is that UUID).
* ``adapter`` selects a BlueZ adapter and means nothing elsewhere.

* One scanner runs all the time for the advertisements. A connect stops it, looks the device
  up again (BlueZ forgets devices it is not scanning for) and restarts it afterwards: BlueZ
  rejects a connect with ``InProgress`` while the same client scans.
* A failed or cancelled connect always ends with a disconnect, so BlueZ is not left with a
  half-open connection that makes the next attempt fail.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from collections.abc import Callable
from typing import Any

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakDBusError, BleakDeviceNotFoundError, BleakError

from smartsolar.ble import Advertisement
from smartsolar.errors import ConnectError, LinkError, NotConnectedError, PairingError
from smartsolar.failures import Failures
from smartsolar.instant import COMPANY_ID, PREFIX
from smartsolar.vreg import CHAR_UUIDS

log = logging.getLogger(__name__)

HAS_AGENT = sys.platform.startswith("linux")  # a BlueZ pairing agent over D-Bus

SCANNER_STOP_TIMEOUT_S = 5.0
DISCONNECT_TIMEOUT_S = 10.0
CONNECT_GRACE_S = 5.0
LOOKUP_TIMEOUT_S = 10.0


def dbus_error_name(exc: BaseException) -> str:
    return getattr(exc, "dbus_error", None) or ""


def map_connect_error(exc: BaseException) -> ConnectError:
    """Translate whatever bleak raised while connecting."""
    if isinstance(exc, BleakDeviceNotFoundError):
        return ConnectError(f"device not found: {exc}", not_found=True)
    if isinstance(exc, BleakDBusError):
        name = dbus_error_name(exc)
        in_progress = name.endswith("InProgress") or "already in progress" in str(exc).lower()
        return ConnectError(f"{name or 'dbus error'}: {exc}", in_progress=in_progress)
    if isinstance(exc, TimeoutError):
        return ConnectError("connect timed out")
    return ConnectError(f"{type(exc).__name__}: {exc}")


def map_write_error(exc: BaseException, connected: bool) -> Exception:
    name = dbus_error_name(exc)
    if not connected or name.endswith("NotConnected") or "not connected" in str(exc).lower():
        return NotConnectedError(str(exc) or "not connected")
    return LinkError(f"{type(exc).__name__}: {exc}")


def map_pair_error(exc: BaseException) -> PairingError:
    name = dbus_error_name(exc)
    text = f"{name or type(exc).__name__}: {exc}"
    if "Authentication" in name or "AuthenticationFailed" in text:
        return PairingError(f"pairing rejected, is the passkey right? ({text})")
    return PairingError(text)


class BleakLink:
    system_pairing_logged = False  # said once per process: every connection asks again

    def __init__(self, address: str, client: Any, agent: Any = None) -> None:
        self.address = address
        self._client = client
        self._agent = agent  # the registered pairing agent, until pairing is done

    @property
    def is_connected(self) -> bool:
        return bool(self._client.is_connected)

    async def start_notify(self, callback: Callable[[str, bytes], None]) -> None:
        for name, uuid in CHAR_UUIDS.items():
            try:
                await self._client.start_notify(uuid, lambda _char, data, name=name: callback(name, bytes(data)))
            except Exception as exc:
                raise LinkError(f"cannot enable notifications on {name}: {type(exc).__name__}: {exc}") from exc

    async def write(self, char: str, data: bytes) -> None:
        try:
            await self._client.write_gatt_char(CHAR_UUIDS[char], data, response=False)
        except (BleakError, EOFError, OSError) as exc:
            raise map_write_error(exc, self.is_connected) from exc

    async def _release_agent(self) -> None:
        agent, self._agent = self._agent, None
        if agent is not None:
            await agent.__aexit__(None, None, None)

    async def pair(self, passkey: str, timeout_s: float) -> None:
        """Pair, or confirm an existing bond. Uses the agent registered by ``connect`` (BlueZ)."""
        if not HAS_AGENT:
            await self._pair_by_the_system(timeout_s)
            return
        if self._agent is None:
            from smartsolar.ble.agent import registered_agent

            self._agent = registered_agent(int(passkey))
            await self._agent.__aenter__()
        try:
            await asyncio.wait_for(self._client.pair(), timeout_s)
        except TimeoutError:
            raise PairingError(f"pairing did not finish within {timeout_s:g} s") from None
        except PairingError:
            raise
        except Exception as exc:
            if dbus_error_name(exc).endswith("AlreadyExists") or "already exists" in str(exc).lower():
                return  # already bonded
            raise map_pair_error(exc) from exc
        finally:
            await self._release_agent()

    async def _pair_by_the_system(self, timeout_s: float) -> None:
        """macOS and Windows: ask the system to pair. It shows its own dialog; our passkey is not used."""
        try:
            await asyncio.wait_for(self._client.pair(), timeout_s)
        except NotImplementedError:
            # CoreBluetooth cannot be told to pair: the system asks when an encrypted characteristic is first used.
            level = logging.DEBUG if BleakLink.system_pairing_logged else logging.INFO
            BleakLink.system_pairing_logged = True
            log.log(level, "Pairing with %s is left to the operating system (it asks for the passkey when needed)", self.address)
        except TimeoutError:
            raise PairingError(f"pairing did not finish within {timeout_s:g} s (enter the passkey in the system dialog)") from None
        except Exception as exc:
            if "already" in str(exc).lower():
                return
            raise map_pair_error(exc) from exc

    async def disconnect(self) -> None:
        try:
            await self._client.disconnect()
        except EOFError:
            pass  # bleak/dbus-fast raise this if the connection is already gone
        finally:
            await self._release_agent()


class BleakBackend:
    def __init__(self, adapter: str | None = None) -> None:
        self._adapter = adapter
        self._scanner: Any = None
        self._on_advert: Callable[[Advertisement], None] | None = None
        self._failures = Failures(log)
        self._adapter_warned = False

    def _kwargs(self) -> dict[str, Any]:
        if self._adapter and not HAS_AGENT:
            if not self._adapter_warned:  # asked at every scan and connection
                self._adapter_warned = True
                log.warning("The ble.adapter setting is only used with BlueZ on Linux, ignored here")
            return {}
        return {"adapter": self._adapter} if self._adapter else {}

    # -- scanning -----------------------------------------------------------

    def _detected(self, device: Any, adv: Any) -> None:
        data = (adv.manufacturer_data or {}).get(COMPANY_ID)
        if data is None or not data or data[0] != PREFIX or self._on_advert is None:
            return
        self._on_advert(
            Advertisement(
                address=device.address.upper(),
                name=adv.local_name or device.name,
                rssi=float(adv.rssi) if adv.rssi is not None else -127.0,
                seen_at=time.monotonic(),
                data=bytes(data),
                handle=device,
            )
        )

    async def start_scan(self, on_advert: Callable[[Advertisement], None]) -> None:
        self._on_advert = on_advert
        await self._start_scanner()

    async def _start_scanner(self) -> None:
        if self._scanner is not None:
            return
        scanner = BleakScanner(detection_callback=self._detected, **self._kwargs())
        await scanner.start()
        self._scanner = scanner

    async def _stop_scanner(self) -> None:
        scanner, self._scanner = self._scanner, None
        if scanner is None:
            return
        try:
            await asyncio.wait_for(scanner.stop(), SCANNER_STOP_TIMEOUT_S)
        except Exception as exc:
            error = str(exc) or type(exc).__name__
            self._failures.failed("scanner stop", error, "Cannot stop the BLE scanner: %s", error)
        else:
            self._failures.succeeded("scanner stop", "Stopping the BLE scanner works again")

    async def stop_scan(self) -> None:
        self._on_advert = None
        await self._stop_scanner()

    # -- connecting ---------------------------------------------------------

    async def connect(
        self,
        adv: Advertisement,
        *,
        timeout_s: float,
        on_disconnect: Callable[[], None],
        passkey: str | None = None,
    ) -> BleakLink:
        scanning = self._scanner is not None
        await self._stop_scanner()
        client = None
        agent = None
        try:
            if passkey is not None and HAS_AGENT:
                # BlueZ starts pairing by itself while it discovers the services of a device that
                # requires it, so the agent must be there before the connection is opened.
                from smartsolar.ble.agent import registered_agent

                agent = registered_agent(int(passkey))
                await agent.__aenter__()
            handle = await BleakScanner.find_device_by_address(adv.address, timeout=LOOKUP_TIMEOUT_S, **self._kwargs())
            if handle is None:
                raise ConnectError("device not found", not_found=True)
            loop = asyncio.get_running_loop()
            client = BleakClient(
                handle,
                disconnected_callback=lambda _client: loop.call_soon_threadsafe(on_disconnect),
                timeout=timeout_s,
                **self._kwargs(),
            )
            async with asyncio.timeout(timeout_s + CONNECT_GRACE_S):
                await client.connect()
        except asyncio.CancelledError:
            await self._cleanup(client)
            await self._drop_agent(agent)
            raise
        except ConnectError:
            await self._drop_agent(agent)
            raise
        except Exception as exc:
            await self._cleanup(client)
            await self._drop_agent(agent)
            raise map_connect_error(exc) from exc
        finally:
            if scanning and self._on_advert is not None:
                try:
                    await self._start_scanner()
                except Exception as exc:
                    error = str(exc) or type(exc).__name__
                    self._failures.failed("scanner restart", error, "Cannot restart the BLE scanner after connecting: %s", error)
                else:
                    self._failures.succeeded("scanner restart", "Restarting the BLE scanner after connecting works again")
        return BleakLink(adv.address, client, agent)

    @staticmethod
    async def _drop_agent(agent: Any) -> None:
        if agent is not None:
            try:
                await agent.__aexit__(None, None, None)
            except Exception as exc:
                log.debug("Cannot unregister the pairing agent after a failed connection: %r", exc)

    @staticmethod
    async def _cleanup(client: Any) -> None:
        if client is None:
            return
        try:
            await asyncio.wait_for(client.disconnect(), DISCONNECT_TIMEOUT_S)
        except BaseException as exc:  # best effort, also when cancelled again
            log.debug("Cannot close the BLE connection after a failed connect: %r", exc)
