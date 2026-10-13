import json
import struct
import zlib
from pathlib import Path

import pytest

from smartsolar import vcsf
from smartsolar.errors import ProfileError

SAMPLE = Path(__file__).parent / "data" / "ve-mppt-75-15-lifepo4.vcsf"


def test_the_sample_file_is_read():
    f = vcsf.read_vcsf(SAMPLE)
    assert f.product_id == 0xA053 and f.min_firmware == 0x163 and f.structure_version == 1
    assert f.app_name == "VictronConnect" and f.app_version == "v6.05"
    assert f.data["/Settings/FloatVoltage"] == 13.5 and f.data["/Settings/Load/OperationMode"] == 2 and f.data["/Settings/BatteryType"] == 255


def test_the_container_layout():
    blob = SAMPLE.read_bytes()
    assert blob[:4] == b"VCCF" and blob[4] == 0x48
    assert struct.unpack(">I", blob[5:9])[0] == len(zlib.decompress(blob[9:]))  # the header holds the size of the JSON


def test_build_and_parse_round_trip():
    data = {"/Settings/FloatVoltage": 13.8, "/Settings/Load/OperationMode": 3}
    blob = vcsf.build_vcsf(data, product_id=0xA053, min_firmware=0x164, description="x", app_version="test")
    f = vcsf.parse_vcsf(blob)
    assert f.data == data and f.product_id == 0xA053 and f.min_firmware == 0x164 and f.description == "x" and f.app_name == "smartsolar"
    document = json.loads(zlib.decompress(blob[9:]))
    assert document["productid"] == "0xA053" and document["minfwversion"] == "0x164" and document["structureversion"] == 1
    assert vcsf.parse_vcsf(vcsf.build_vcsf({}, product_id=None, min_firmware=None)).product_id == 0


def good(document, size=None):
    text = json.dumps(document).encode()
    return b"VCCF\x48" + struct.pack(">I", len(text) if size is None else size) + zlib.compress(text)


@pytest.mark.parametrize(
    "blob, message",
    [
        (b"", "VCCF"),
        (b"XXXX\x48\x00\x00\x00\x01x", "VCCF"),
        (b"VCCF\x48\x00\x00\x00\x05notzlib", "decompress"),
        (b"VCCF\x48\xff\xff\xff\xff" + zlib.compress(b"{}"), "refusing"),
        (good({"data": {}}, size=5), "size"),
        (b"VCCF\x48" + struct.pack(">I", 8) + zlib.compress(b"not json"), "not JSON"),
        (good([1, 2]), "no 'data'"),
        (good({"productid": "0xA053"}), "no 'data'"),
    ],
)
def test_parse_refuses(blob, message):
    with pytest.raises(ProfileError, match=message):
        vcsf.parse_vcsf(blob)


def test_a_decompression_bomb_is_not_expanded():
    bomb = zlib.compress(b"a" * (vcsf.MAX_JSON_BYTES + 10))
    with pytest.raises(ProfileError):
        vcsf.parse_vcsf(b"VCCF\x48" + struct.pack(">I", vcsf.MAX_JSON_BYTES) + bomb)


def test_read_errors(tmp_path):
    with pytest.raises(ProfileError, match="cannot read"):
        vcsf.read_vcsf(tmp_path / "missing.vcsf")
    big = tmp_path / "big.vcsf"
    big.write_bytes(b"\0" * (vcsf.MAX_FILE_BYTES + 1))
    with pytest.raises(ProfileError, match="too large"):
        vcsf.read_vcsf(big)
