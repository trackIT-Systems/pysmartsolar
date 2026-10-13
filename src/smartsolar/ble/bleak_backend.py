"""BleBackend on top of bleak (BlueZ on the Pi).

Only API that exists in bleak 0.22 (Debian) and 1.x is used.

* One scanner runs all the time for the advertisements. A connect stops it, looks the device
  up again (BlueZ forgets devices it is not scanning for) and restarts it afterwards: BlueZ
  rejects a connect with ``InProgress`` while the same client scans.
* A failed or cancelled connect always ends with a disconnect, so BlueZ is not left with a
  half-open connection that makes the next attempt fail.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakDBusError, BleakDeviceNotFoundError, BleakError

from smartsolar.ble import Advertisement
from smartsolar.errors import ConnectError, LinkError, NotConnectedError, PairingError
from smartsolar.instant import COMPANY_ID, PREFIX
from smartsolar.vreg import CHAR_UUIDS

log = logging.getLogger(__name__)

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
    def __init__(self, address: str, client: Any) -> None:
        self.address = address
        self._client = client

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

    async def pair(self, passkey: str, timeout_s: float) -> None:
        from smartsolar.ble.agent import registered_agent

        try:
            async with registered_agent(int(passkey)):
                await asyncio.wait_for(self._client.pair(), timeout_s)
        except TimeoutError:
            raise PairingError(f"pairing did not finish within {timeout_s:g} s") from None
        except PairingError:
            raise
        except Exception as exc:
            if dbus_error_name(exc).endswith("AlreadyExists") or "already exists" in str(exc).lower():
                return  # already bonded
            raise map_pair_error(exc) from exc

    async def disconnect(self) -> None:
        try:
            await self._client.disconnect()
        except EOFError:
            pass  # bleak/dbus-fast raise this if the connection is already gone


class BleakBackend:
    def __init__(self, adapter: str | None = None) -> None:
        self._adapter = adapter
        self._scanner: Any = None
        self._on_advert: Callable[[Advertisement], None] | None = None

    def _kwargs(self) -> dict[str, Any]:
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
            log.warning("stopping the scanner failed: %s", exc)

    async def stop_scan(self) -> None:
        self._on_advert = None
        await self._stop_scanner()

    # -- connecting ---------------------------------------------------------

    async def connect(self, adv: Advertisement, *, timeout_s: float, on_disconnect: Callable[[], None]) -> BleakLink:
        scanning = self._scanner is not None
        await self._stop_scanner()
        client = None
        try:
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
            raise
        except ConnectError:
            raise
        except Exception as exc:
            await self._cleanup(client)
            raise map_connect_error(exc) from exc
        finally:
            if scanning and self._on_advert is not None:
                try:
                    await self._start_scanner()
                except Exception as exc:
                    log.warning("restarting the scanner failed: %s", exc)
        return BleakLink(adv.address, client)

    @staticmethod
    async def _cleanup(client: Any) -> None:
        if client is None:
            return
        try:
            await asyncio.wait_for(client.disconnect(), DISCONNECT_TIMEOUT_S)
        except BaseException as exc:  # best effort, also when cancelled again
            log.debug("cleanup disconnect: %r", exc)
