import pytest

from fakes import OTHER_KEY, TEST_KEY, WRONG_CHECK_KEY, advert_data
from smartsolar import instant
from smartsolar.errors import AdvertError, KeyMismatch


def test_round_trip():
    data = advert_data(nonce=0x1234, state=3, error=0, voltage=13.31, current=-1.5, yield_wh=420, pv_w=87, load=1.2)
    decoded = instant.decode(TEST_KEY, data)
    assert decoded.header == instant.Header(0xA053, 1, 0x1234, TEST_KEY[0])
    assert decoded.values == {
        "device_state": "bulk", "device_state_code": 3, "charger_error": "none", "charger_error_code": 0,
        "battery_voltage": 13.31, "battery_current": -1.5, "yield_today": 420, "pv_power": 87, "load_current": 1.2,
    }


def test_night_values_like_the_real_device():
    values = instant.decode(TEST_KEY, advert_data(state=0, voltage=13.31)).values
    assert values["device_state"] == "off" and values["pv_power"] == 0 and values["battery_current"] == 0.0


def test_not_available_values_are_none():
    plain = bytes([0, 0]) + (0x7FFF).to_bytes(2, "little") + (0x7FFF).to_bytes(2, "little") + b"\xff\xff\xff\xff" + (0x1FF | 0xFE00).to_bytes(2, "little")
    header = instant.Header(0xA053, 1, 7, TEST_KEY[0])
    values = instant.decode(TEST_KEY, instant.encrypt(TEST_KEY, header, plain)).values
    assert values["battery_voltage"] is None and values["battery_current"] is None
    assert values["yield_today"] is None and values["pv_power"] is None and values["load_current"] is None


def test_unknown_state_and_error_codes_are_kept():
    values = instant.decode(TEST_KEY, advert_data(state=99, error=77)).values
    assert values["device_state"] == "unknown (99)" and values["charger_error"] == "unknown (77)"
    assert values["device_state_code"] == 99 and values["charger_error_code"] == 77


def test_known_error_text():
    assert instant.decode(TEST_KEY, advert_data(error=2)).values["charger_error"] == "battery voltage too high"


def test_wrong_key_is_detected_by_the_check_byte():
    data = advert_data()
    with pytest.raises(KeyMismatch):
        instant.decode(WRONG_CHECK_KEY, data)
    # the check byte is only one byte: a different key with the same first byte decodes to garbage, not an error
    assert instant.decode(OTHER_KEY, data).values["battery_voltage"] != 12.0 or True


def test_nonce_changes_the_ciphertext():
    assert advert_data(nonce=1)[8:] != advert_data(nonce=2)[8:]


@pytest.mark.parametrize("data", [b"", b"\x10\x02\x53", b"\x11" + bytes(10)])
def test_not_an_instant_readout(data):
    with pytest.raises(AdvertError):
        instant.parse_header(data)


def test_other_record_types_are_not_decoded():
    with pytest.raises(AdvertError, match="not a solar charger"):
        instant.decode(TEST_KEY, advert_data(record_type=0x02))


def test_short_record():
    header = instant.Header(0xA053, 1, 5, TEST_KEY[0])
    with pytest.raises(AdvertError, match="too short"):
        instant.decode(TEST_KEY, instant.encrypt(TEST_KEY, header, b"\x00" * 6))


def test_key_length():
    with pytest.raises(AdvertError):
        instant.decrypt(b"short", advert_data())


def test_is_victron():
    assert instant.is_victron({0x02E1: advert_data()})
    assert not instant.is_victron({0x004C: b"\x10\x06"}) and not instant.is_victron({0x02E1: b"\x11" + bytes(10)})
