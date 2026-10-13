"""Transport interface between the daemon and the BLE stack.

Only ``smartsolar.ble.bleak_backend`` (and ``agent``) import bleak and dbus-fast. Everything
else talks to these protocols, so the daemon can be tested against a fake backend.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from smartsolar.errors import ConnectError

__all__ = ["Advertisement", "BleBackend", "ConnectError", "GattLink"]


@dataclass(frozen=True)
class Advertisement:
    """One advertisement of a Victron device (manufacturer data of company 0x02E1)."""

    address: str
    name: str | None
    rssi: float
    seen_at: float  # time.monotonic()
    data: bytes  # the manufacturer data
    handle: object | None = None  # backend specific, e.g. a bleak BLEDevice


class GattLink(Protocol):
    """An open GATT connection."""

    address: str

    @property
    def is_connected(self) -> bool: ...

    async def start_notify(self, callback: Callable[[str, bytes], None]) -> None:
        """Enable notifications on the three characteristics ("0021", "0024", "0027").

        ``callback(char, data)`` runs on the event loop thread.
        """

    async def write(self, char: str, data: bytes) -> None:
        """Write without response. Raises NotConnectedError if the link is gone, LinkError otherwise."""

    async def pair(self, passkey: str, timeout_s: float) -> None:
        """Pair (or confirm an existing bond) with the passkey. Raises PairingError."""

    async def disconnect(self) -> None: ...


class BleBackend(Protocol):
    async def start_scan(self, on_advert: Callable[[Advertisement], None]) -> None:
        """Scan continuously. ``on_advert`` is called for every Victron advertisement."""

    async def stop_scan(self) -> None: ...

    async def connect(
        self,
        adv: Advertisement,
        *,
        timeout_s: float,
        on_disconnect: Callable[[], None],
    ) -> GattLink:
        """Open a GATT connection. Suspends the scan while connecting and resumes it afterwards.

        Raises ConnectError. Must not leave a half-open connection behind.
        """
