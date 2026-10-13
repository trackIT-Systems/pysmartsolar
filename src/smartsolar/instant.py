"""Victron Instant Readout: the encrypted manufacturer data in the advertisements.

Layout of the manufacturer data (company ID 0x02E1), as verified against a SmartSolar::

    10 02 | model (u16 LE) | record type (u8) | nonce (u16 LE) | key check (u8) | ciphertext

The ciphertext is AES-128-CTR with the device key. The counter block is the nonce as a 16 byte
little-endian integer. ``key check`` is the first byte of the key. Record type 0x01 is a solar
charger::

    state u8 | charger error u8 | battery voltage s16 (0.01 V) | battery current s16 (0.1 A)
    | yield today u16 (10 Wh) | PV power u16 (W) | load current u9 (0.1 A)
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from smartsolar.errors import AdvertError, KeyMismatch

COMPANY_ID = 0x02E1
PREFIX = 0x10
HEADER_LEN = 8
RECORD_SOLAR_CHARGER = 0x01
KEY_LEN = 16

STATES = {
    0: "off",
    1: "low power",
    2: "fault",
    3: "bulk",
    4: "absorption",
    5: "float",
    6: "storage",
    7: "equalize (manual)",
    9: "inverting",
    11: "power supply",
    245: "starting up",
    246: "repeated absorption",
    247: "auto equalize / recondition",
    248: "battery safe",
    252: "external control",
}

ERRORS = {
    0: "none",
    2: "battery voltage too high",
    17: "charger temperature too high",
    18: "charger over current",
    19: "charger current reversed",
    20: "bulk time limit exceeded",
    21: "current sensor issue",
    26: "terminals overheated",
    28: "converter issue",
    33: "input voltage too high (PV)",
    34: "input current too high (PV)",
    38: "input shutdown (excessive battery voltage)",
    39: "input shutdown (current flow during off mode)",
    65: "lost communication with another device",
    66: "synchronised charging device configuration issue",
    67: "BMS connection lost",
    68: "network misconfigured",
    116: "factory calibration data lost",
    117: "invalid or incompatible firmware",
    119: "user settings invalid",
}


@dataclass(frozen=True)
class Header:
    model: int
    record_type: int
    nonce: int
    key_check: int


@dataclass(frozen=True)
class Decoded:
    header: Header
    values: dict[str, Any]  # name -> value (None = not available)


def parse_header(data: bytes) -> Header:
    if len(data) < HEADER_LEN or data[0] != PREFIX:
        raise AdvertError("not an Instant Readout advertisement")
    model, = struct.unpack_from("<H", data, 2)
    nonce, = struct.unpack_from("<H", data, 5)
    return Header(model=model, record_type=data[4], nonce=nonce, key_check=data[7])


def is_victron(manufacturer_data: dict[int, bytes]) -> bool:
    data = manufacturer_data.get(COMPANY_ID)
    return data is not None and len(data) >= HEADER_LEN and data[0] == PREFIX


def decrypt(key: bytes, data: bytes) -> tuple[Header, bytes]:
    if len(key) != KEY_LEN:
        raise AdvertError(f"the key must be {KEY_LEN} bytes")
    header = parse_header(data)
    if header.key_check != key[0]:
        raise KeyMismatch(f"key check byte 0x{header.key_check:02x} does not match the key (0x{key[0]:02x})")
    decryptor = Cipher(algorithms.AES(key), modes.CTR(header.nonce.to_bytes(16, "little"))).decryptor()
    return header, decryptor.update(data[HEADER_LEN:]) + decryptor.finalize()


def decode(key: bytes, data: bytes) -> Decoded:
    header, plain = decrypt(key, data)
    if header.record_type != RECORD_SOLAR_CHARGER:
        raise AdvertError(f"record type 0x{header.record_type:02x} is not a solar charger")
    return Decoded(header, _solar_charger(plain))


def _solar_charger(plain: bytes) -> dict[str, Any]:
    if len(plain) < 10:
        raise AdvertError(f"solar charger record too short ({len(plain)} bytes)")
    state, error, voltage, current, yield_today, pv_power = struct.unpack_from("<BBhhHH", plain, 0)
    load = None
    if len(plain) >= 12:
        raw = int.from_bytes(plain[10:12], "little") & 0x1FF
        load = None if raw == 0x1FF else raw / 10
    return {
        "device_state": STATES.get(state, f"unknown ({state})"),
        "device_state_code": state,
        "charger_error": ERRORS.get(error, f"unknown ({error})"),
        "charger_error_code": error,
        "battery_voltage": None if voltage == 0x7FFF else voltage / 100,
        "battery_current": None if current == 0x7FFF else current / 10,
        "yield_today": None if yield_today == 0xFFFF else yield_today * 10,
        "pv_power": None if pv_power == 0xFFFF else pv_power,
        "load_current": load,
    }


def encrypt(key: bytes, header: Header, plain: bytes) -> bytes:
    """Build an advertisement. Only used by tests and the fake device."""
    encryptor = Cipher(algorithms.AES(key), modes.CTR(header.nonce.to_bytes(16, "little"))).encryptor()
    body = encryptor.update(plain) + encryptor.finalize()
    return bytes([PREFIX, 0x02]) + struct.pack("<H", header.model) + bytes([header.record_type]) + struct.pack("<H", header.nonce) + bytes([key[0]]) + body


def solar_charger_record(
    *, state: int = 0, error: int = 0, voltage: float = 12.0, current: float = 0.0, yield_wh: int = 0, pv_w: int = 0, load: float = 0.0
) -> bytes:
    """Pack a solar charger record (tests). 0x7FFF / 0xFFFF / 0x1FF mean not available."""
    return struct.pack("<BBhhHH", state, error, round(voltage * 100), round(current * 10), yield_wh // 10, pv_w) + (
        (round(load * 10) & 0x1FF) | 0xFE00
    ).to_bytes(2, "little")
