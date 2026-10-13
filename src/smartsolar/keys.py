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

log = logging.getLogger(__name__)

KEYS_FILENAME = "keys.json"
SOURCE_CONFIG = "config"
SOURCE_FETCHED = "fetched"
SOURCE_MISSING = "missing"


class KeyStore:
    """Keys by device address. The config wins; fetched keys live in ``<state_dir>/keys.json`` (0600)."""

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
            log.warning("cannot read %s: %s", self._path, exc)
            return
        if not isinstance(raw, dict):
            log.warning("ignoring %s: not a mapping", self._path)
            return
        for address, text in raw.items():
            try:
                self._fetched[normalise_address(address)] = parse_key(text, "stored key")
            except ConfigError as exc:
                log.warning("ignoring a stored key for %s: %s", address, exc)

    def get(self, address: str) -> tuple[bytes | None, str]:
        address = normalise_address(address)
        if address in self._configured:
            return self._configured[address], SOURCE_CONFIG
        if address in self._fetched:
            return self._fetched[address], SOURCE_FETCHED
        return None, SOURCE_MISSING

    def store(self, address: str, key: bytes) -> None:
        """Remember a fetched key (written atomically, readable by the owner only)."""
        self._fetched[normalise_address(address)] = key
        self._save()

    def forget(self, address: str) -> bool:
        """Drop a fetched key, e.g. because the device no longer accepts it."""
        removed = self._fetched.pop(normalise_address(address), None) is not None
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
