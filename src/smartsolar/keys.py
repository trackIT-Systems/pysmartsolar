"""Where the Instant Readout keys come from: the config, else what the daemon fetched over GATT."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

from smartsolar.config import normalise_address, parse_key
from smartsolar.errors import ConfigError
from smartsolar.reading import serial_from_name

log = logging.getLogger(__name__)

KEYS_FILENAME = "keys.json"
SOURCE_CONFIG = "config"
SOURCE_FETCHED = "fetched"
SOURCE_MISSING = "missing"


class KeyStore:
    """Keys by device: the serial number (from the BLE name) or the address (a MAC, or a UUID on macOS).

    The config wins; fetched keys live in ``<state_dir>/keys.json`` (0600) under the serial number
    when the name has one, so they survive a change of address.
    """

    def __init__(self, configured: Mapping[str, bytes], state_dir: Path) -> None:
        self._configured = {normalise_address(a): k for a, k in configured.items()}
        self._path = Path(state_dir) / KEYS_FILENAME
        self._fetched: dict[str, bytes] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("Cannot read %s: %s", self._path, exc)
            return
        if not isinstance(raw, dict):
            log.warning("Ignoring %s: not a mapping", self._path)
            return
        for address, text in raw.items():
            try:
                self._fetched[normalise_address(address)] = parse_key(text, "stored key")
            except ConfigError as exc:
                log.warning("Ignoring the stored key for %s: %s", address, exc)

    @staticmethod
    def _names(address: str, name: str | None) -> list[str]:
        """The ways this device may be called, in order of preference."""
        serial = serial_from_name(name)
        names = [normalise_address(serial)] if serial else []
        names.append(normalise_address(address))
        return names

    def get(self, address: str, name: str | None = None) -> tuple[bytes | None, str]:
        names = self._names(address, name)
        for ident in names:
            if ident in self._configured:
                return self._configured[ident], SOURCE_CONFIG
        for ident in names:
            if ident in self._fetched:
                return self._fetched[ident], SOURCE_FETCHED
        return None, SOURCE_MISSING

    def store(self, address: str, key: bytes, name: str | None = None) -> None:
        """Remember a fetched key (written atomically, readable by the owner only)."""
        self._fetched[self._names(address, name)[0]] = key
        self._save()

    def forget(self, address: str, name: str | None = None) -> bool:
        """Drop a fetched key, e.g. because the device no longer accepts it."""
        removed = False
        for ident in self._names(address, name):
            removed = self._fetched.pop(ident, None) is not None or removed
        if removed:
            self._save()
        return removed

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".keys-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({a: k.hex() for a, k in self._fetched.items()}, handle)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
