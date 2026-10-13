import pytest

from smartsolar import vedirect
from smartsolar.vedirect import HexFrame, StreamParser, TextBlock, decode_text, get_frame, hex_frame, parse_hex_frame, set_frame

FIELDS = [
    ("PID", "0xA053"), ("FW", "164"), ("SER#", "HQ00000TEST"), ("V", "13320"), ("I", "-10"), ("VPV", "24010"),
    ("PPV", "2"), ("CS", "3"), ("MPPT", "2"), ("OR", "0x00000000"), ("ERR", "0"), ("LOAD", "ON"), ("IL", "0"),
    ("H19", "9847"), ("H20", "1"), ("H21", "2"), ("H22", "0"), ("H23", "0"), ("HSDS", "343"),
]


def text_block(fields=FIELDS, *, corrupt=False) -> bytes:
    body = b"".join(b"\r\n" + k.encode() + b"\t" + v.encode() for k, v in fields) + b"\r\nChecksum\t"
    checksum = (-sum(body)) & 0xFF
    if corrupt:
        checksum ^= 0x01
    return body + bytes([checksum])


def blocks(items):
    return [i for i in items if isinstance(i, TextBlock)]


def test_hex_frames_follow_the_documented_checksum_rule():
    # the document's example: ping is ":154\n" (1 + 0x54 = 0x55)
    assert vedirect.ping_frame() == b":154\n"
    assert get_frame(0xEDF1) == hex_frame(7, bytes.fromhex("F1ED00"))
    frame = set_frame(0xEDF6, bytes.fromhex("8a05"))
    parsed = parse_hex_frame(frame[1:-1])
    assert parsed == HexFrame(8, bytes.fromhex("F6ED008A05")) and parsed.vreg == 0xEDF6 and parsed.value == bytes.fromhex("8a05")


def test_damaged_hex_frames_are_refused():
    assert parse_hex_frame(b"7F1ED0000") is None
    assert parse_hex_frame(b"7F1ED00") is None  # odd digits
    assert parse_hex_frame(b"") is None
    assert parse_hex_frame(b"ZZ") is None


def test_one_text_block_is_decoded():
    p = StreamParser()
    out = p.feed(text_block())
    assert out == []  # the first block is only used to find the framing
    out = p.feed(text_block())
    assert len(blocks(out)) == 1 and blocks(out)[0].fields["V"] == "13320"
    assert p.stats.blocks == 1 and p.stats.bad_checksum == 0


def test_garbage_and_split_reads_still_give_every_block():
    stream = b"\x00\xffgarbage" + text_block() * 3
    p = StreamParser()
    got = []
    for i in range(0, len(stream), 7):  # arbitrary chunking
        got += blocks(p.feed(stream[i : i + 7]))
    assert len(got) == 2  # the first one finds the framing


def test_a_bad_checksum_drops_only_that_block():
    p = StreamParser()
    got = blocks(p.feed(text_block() + text_block(corrupt=True) + text_block() + text_block()))
    assert len(got) == 2 and p.stats.bad_checksum == 1


def test_a_hex_frame_inside_a_text_block_does_not_disturb_the_checksum():
    block = text_block()
    cut = block.index(b"\r\nV\t")
    mixed = block[:cut] + get_frame(0x0100) + block[cut:]
    p = StreamParser()
    items = p.feed(text_block() + mixed + text_block())
    assert len(blocks(items)) == 2
    assert [i for i in items if isinstance(i, HexFrame)] == [HexFrame(7, bytes.fromhex("000100"))]


def test_a_colon_as_checksum_byte_is_not_a_hex_frame():
    fields = list(FIELDS)
    for n in range(256):  # find a value that makes the checksum byte ':'
        fields[3] = ("V", str(13000 + n))
        if text_block(fields)[-1] == 0x3A:
            break
    else:
        pytest.skip("no such checksum")
    p = StreamParser()
    got = blocks(p.feed(text_block() + text_block(fields) + text_block()))
    assert len(got) == 2 and p.stats.bad_checksum == 0 and p.stats.bad_hex == 0


def test_bad_hex_is_counted():
    p = StreamParser()
    p.feed(b":7F1ED0000\n")
    assert p.stats.bad_hex == 1 and p.stats.hex_frames == 0
    p.feed(b":" + b"A" * 300)
    assert p.stats.bad_hex == 2


def test_overlong_line_loses_the_framing_and_finds_it_again():
    p = StreamParser()
    got = blocks(p.feed(b"x" * 500 + text_block() + text_block() + text_block()))
    assert len(got) == 2 and p.stats.resyncs >= 1


def test_decode_text_values():
    r = decode_text({k: v for k, v in FIELDS})
    assert r.serial == "HQ00000TEST" and r.product_id == 0xA053 and r.firmware == 0x164
    v = r.values
    assert v["battery_voltage"] == 13.32 and v["battery_current"] == -0.01 and v["panel_voltage"] == 24.01
    assert v["pv_power"] == 2 and v["device_state"] == "bulk" and v["device_state_code"] == 3
    assert v["charger_error"] == "none" and v["tracker_mode"] == "mpp tracker active" and v["off_reason"] == "none"
    assert v["load_output"] == "on" and v["yield_total"] == 98.47 and v["yield_today"] == 0.01 and v["day_sequence"] == 343
    assert r.unknown == {}


def test_decode_text_off_reason_and_unknown_fields():
    r = decode_text({"OR": "0x00000005", "CS": "0", "XYZ": "1", "V": "oops", "ERR": "119", "Checksum": "x"})
    assert r.values["off_reason"] == "no input power, switched off (device mode register)"
    assert "battery_voltage" not in r.values  # not guessed
    assert r.values["charger_error"] == "user settings invalid" and r.unknown == {"XYZ": "1"}
    assert decode_text({"CS": "77"}).values["device_state"] == "unknown (77)"


def test_nul_padding_is_ignored_in_text_and_hex():
    block = text_block()
    cut = block.index(b"\r\nPPV")
    padded = block[:cut] + b"\x00" + block[cut:]
    cut = padded.index(b"9847")
    padded = padded[:cut] + b"\x00" + padded[cut:]
    p = StreamParser()
    frame = hex_frame(0xA, bytes.fromhex("000200") + b"\x01")
    padded_frame = frame[:4] + b"\x00" + frame[4:]
    items = p.feed(text_block() + padded + padded_frame + text_block())
    got = blocks(items)
    assert len(got) == 2 and got[0].fields["PPV"] == "2" and got[0].fields["H19"] == "9847"
    assert p.stats.bad_checksum == 0 and p.stats.bad_hex == 0
    assert [i for i in items if isinstance(i, HexFrame)] == [HexFrame(0xA, bytes.fromhex("00020001"))]


def test_a_block_cut_short_by_a_request_is_dropped_and_the_next_one_is_fine():
    block = text_block()
    cut = block.index(b"\r\nV\t")
    p = StreamParser()
    items = p.feed(text_block() + block[:cut] + b"\n" + get_frame(0x0100) + block + text_block())
    assert len(blocks(items)) == 1 and p.stats.bad_checksum == 1
