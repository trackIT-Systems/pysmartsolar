"""Settings profiles: named sets of charge settings that can be saved and loaded.

A profile covers the numeric writable registers (float and absorption voltage, maximum charge
current). Values are validated with the same rules as ``smartsolar write``. This module has no
BLE or daemon dependency.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from smartsolar import __version__
from smartsolar.errors import ProfileError
from smartsolar.vreg import WRITABLE, build_write

# The settings a profile may contain. The operating states (device mode, load output) are not
# part of it: loading a profile must not switch the charger or the load.
PROFILE_PARAMETERS: tuple[str, ...] = tuple(name for name, w in WRITABLE.items() if w.values is None)
FLOAT_TOLERANCE = 0.005


@dataclass(frozen=True)
class Profile:
    name: str
    values: dict[str, Any]
    description: str = ""
    source: str = ""


@dataclass(frozen=True)
class Change:
    name: str
    unit: str
    old: Any
    new: Any


def validate_values(values: Mapping[str, Any]) -> dict[str, Any]:
    """Check names and values; return the values as the device will hold them."""
    if not isinstance(values, Mapping) or not values:
        raise ProfileError("a profile needs a non-empty 'values' mapping")
    unknown = [n for n in values if n not in PROFILE_PARAMETERS]
    if unknown:
        raise ProfileError(f"not a profile setting: {', '.join(map(str, unknown))} (allowed: {', '.join(PROFILE_PARAMETERS)})")
    return {item.name: item.value for item in build_write(dict(values))}


def parse_profile(text: str, *, default_name: str = "profile", source: str = "") -> Profile:
    where = source or "profile"
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ProfileError(f"{where}: invalid YAML: {exc}") from None
    if not isinstance(data, dict):
        raise ProfileError(f"{where}: expected a mapping with 'values'")
    extra = set(data) - {"name", "description", "values"}
    if extra:
        raise ProfileError(f"{where}: unknown key(s): {', '.join(sorted(map(str, extra)))}")
    try:
        values = validate_values(data.get("values"))
    except Exception as exc:  # ProfileError or InvalidWriteError: name the profile
        raise type(exc)(f"{where}: {exc}") from None
    return Profile(str(data.get("name") or default_name), values, str(data.get("description") or ""), source)


def _premade_dir():
    return resources.files("smartsolar") / "premade"


def list_premade() -> list[Profile]:
    profiles = []
    for entry in sorted(_premade_dir().iterdir(), key=lambda e: e.name):
        if entry.name.endswith(".yml"):
            profiles.append(parse_profile(entry.read_text(encoding="utf-8"), default_name=entry.name[:-4], source="premade"))
    return profiles


def is_path(ref: str) -> bool:
    return "/" in ref or ref.endswith((".yml", ".yaml"))


def load_profile(ref: str) -> Profile:
    """A premade profile by name, or a file if the reference looks like a path."""
    if is_path(ref):
        path = Path(ref)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ProfileError(f"cannot read profile {ref}: {exc.strerror or exc}") from None
        return parse_profile(text, default_name=path.stem, source=str(path))
    for profile in list_premade():
        if profile.name == ref:
            return profile
    names = ", ".join(p.name for p in list_premade())
    raise ProfileError(f"no premade profile {ref!r} (available: {names}); use ./{ref}.yml for a file")


def profile_from_values(values: Iterable[Mapping[str, Any]], *, name: str, description: str = "") -> Profile:
    """Build a profile from the values of a ``settings`` result."""
    by_name = {v["name"]: v["value"] for v in values}
    picked = {n: by_name[n] for n in PROFILE_PARAMETERS if by_name.get(n) is not None}
    if not picked:
        raise ProfileError("the device returned none of the profile settings")
    return Profile(name, picked, description)


def dump_profile(profile: Profile, *, device: str | None = None, when: datetime | None = None) -> str:
    when = when or datetime.now().astimezone()
    header = [f"# smartsolar profile, saved {when.isoformat(timespec='seconds')} by smartsolar {__version__}"]
    if device:
        header.append(f"# from device {device}")
    body: dict[str, Any] = {"name": profile.name}
    if profile.description:
        body["description"] = profile.description
    body["values"] = dict(profile.values)
    return "\n".join(header) + "\n" + yaml.safe_dump(body, sort_keys=False, allow_unicode=True)


def same(a: Any, b: Any) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) and not isinstance(b, bool):
        return abs(a - b) < FLOAT_TOLERANCE
    return str(a).strip().casefold() == str(b).strip().casefold()


def diff(profile: Profile, current: Iterable[Mapping[str, Any]]) -> list[Change]:
    """What loading the profile would change, in the order of the settings."""
    now = {v["name"]: v for v in current}
    changes = []
    for name in PROFILE_PARAMETERS:
        if name not in profile.values:
            continue
        have = now.get(name)
        old = None if have is None else have["value"]
        if have is None or not same(old, profile.values[name]):
            changes.append(Change(name, WRITABLE[name].unit, old, profile.values[name]))
    return changes
