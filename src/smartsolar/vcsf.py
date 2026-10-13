"""VictronConnect settings files (``.vcsf``).

A settings file is a container around a JSON document that holds VictronConnect's model of one
device: the settings, but also live values, history and the serial number::

    "VCCF"  0x48  <u32 big endian: size of the JSON>  <zlib stream of the JSON>

The JSON has ``productid`` ("0xA053"), ``minfwversion``, ``structureversion``, ``appName``,
``appversion``, ``description`` and ``data``, a flat object with keys such as
``/Settings/FloatVoltage``. Only the ``/Settings/`` keys are of interest here.
"""

from __future__ import annotations

import json
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from smartsolar.errors import ProfileError

MAGIC = b"VCCF"
FORMAT_BYTE = 0x48
HEADER_LEN = 9
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_FILE_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class VcsfFile:
    product_id: int | None
    min_firmware: int | None
    structure_version: int | None
    app_name: str
    app_version: str
    description: str
    data: dict[str, Any]


def _hex_number(text: Any) -> int | None:
    if isinstance(text, int) and not isinstance(text, bool):
        return text
    if isinstance(text, str):
        try:
            return int(text, 16) if text.lower().startswith("0x") else int(text)
        except ValueError:
            return None
    return None


def parse_vcsf(blob: bytes, source: str = "settings file") -> VcsfFile:
    if len(blob) < HEADER_LEN or blob[:4] != MAGIC:
        raise ProfileError(f"{source}: not a VictronConnect settings file (no VCCF header)")
    size = struct.unpack(">I", blob[5:9])[0]
    if size > MAX_JSON_BYTES:
        raise ProfileError(f"{source}: the settings data claims {size} bytes, refusing it")
    try:
        decompressor = zlib.decompressobj()
        text = decompressor.decompress(blob[HEADER_LEN:], MAX_JSON_BYTES + 1)
    except zlib.error as exc:
        raise ProfileError(f"{source}: cannot decompress the settings data: {exc}") from None
    if len(text) > MAX_JSON_BYTES or len(text) != size:
        raise ProfileError(f"{source}: the settings data does not have the size its header says")
    try:
        document = json.loads(text)
    except ValueError as exc:
        raise ProfileError(f"{source}: the settings data is not JSON: {exc}") from None
    data = document.get("data") if isinstance(document, dict) else None
    if not isinstance(data, dict):
        raise ProfileError(f"{source}: no 'data' object in the settings file")
    return VcsfFile(
        product_id=_hex_number(document.get("productid")),
        min_firmware=_hex_number(document.get("minfwversion")),
        structure_version=document.get("structureversion") if isinstance(document.get("structureversion"), int) else None,
        app_name=str(document.get("appName", "")),
        app_version=str(document.get("appversion", "")),
        description=str(document.get("description", "")),
        data=data,
    )


def read_vcsf(path: str | Path) -> VcsfFile:
    path = Path(path)
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ProfileError(f"{path}: file too large for a settings file")
        blob = path.read_bytes()
    except OSError as exc:
        raise ProfileError(f"cannot read {path}: {exc.strerror or exc}") from None
    return parse_vcsf(blob, str(path))


def build_vcsf(
    data: dict[str, Any],
    *,
    product_id: int | None,
    min_firmware: int | None,
    description: str = "",
    app_name: str = "smartsolar",
    app_version: str = "",
) -> bytes:
    document = {
        "appName": app_name,
        "appversion": app_version,
        "data": data,
        "description": description,
        "minfwversion": f"0x{min_firmware:x}" if min_firmware is not None else "0x0",
        "productid": f"0x{product_id:X}" if product_id is not None else "0x0",
        "structureversion": 1,
    }
    text = json.dumps(document, indent=4, sort_keys=True).encode("utf-8")
    return MAGIC + bytes([FORMAT_BYTE]) + struct.pack(">I", len(text)) + zlib.compress(text)
