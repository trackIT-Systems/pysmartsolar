import cbor2

from fakes import TEST_KEY, record8
from smartsolar.vreg import PING, VREG_KEY, RecordParser, decode_vreg, get_request, vreg_table, INIT_SEQUENCE


def test_get_request_bytes_match_the_captured_ones():
    assert get_request(0xEC65) == bytes.fromhex("05008119ec65")
    assert get_request(0x0102, ctx=3) == bytes.fromhex("050381190102")
    assert get_request(0x90) == bytes.fromhex("0500811890")  # one byte CBOR ids work too
    assert get_request(0xEC66, 0xEC65) == bytes.fromhex("05008219ec6619ec65")  # several registers in one request


def test_init_sequence_contains_the_handshake_and_the_ping():
    assert INIT_SEQUENCE[0] == ("0021", "fa80ff") and INIT_SEQUENCE[-1] == ("0021", "f941")
    assert any(h.startswith(PING.hex()) for _, h in INIT_SEQUENCE)


def parse(*chunks):
    parser = RecordParser()
    out = []
    for chunk in chunks:
        out += parser.feed(bytes.fromhex(chunk))
    return out, parser


def test_captured_value_records():
    records, parser = parse("080319ed8d423305")  # channel 1 voltage 13.31 V
    assert [(r.kind, r.ctx, r.vreg, r.value) for r in records] == [(8, 3, 0xED8D, b"\x33\x05")]
    assert parser.dropped == 0


def test_one_byte_register_ids_and_error_records():
    records, _ = parse("08001893421027", "0900189001", "090019edd501")
    assert [(r.kind, r.vreg, r.value) for r in records] == [(8, 0x93, b"\x10\x27"), (9, 0x90, 1), (9, 0xEDD5, 1)]


def test_several_records_in_one_notification():
    records, _ = parse("080319ed8f420000" + "080319ed8c44d8ffffff")
    assert [(r.vreg, r.value) for r in records] == [(0xED8F, b"\x00\x00"), (0xED8C, bytes.fromhex("d8ffffff"))]


def test_a_record_split_across_notifications():
    full = "080019ec6646f748188a779d"
    for cut in range(1, len(full) // 2):
        records, parser = parse(full[: cut * 2], full[cut * 2 :])
        assert [(r.vreg, r.value) for r in records] == [(0xEC66, bytes.fromhex("f748188a779d"))], cut
        assert parser.dropped == 0


def test_key_record_split_in_the_middle_of_the_key():
    raw = record8(VREG_KEY, TEST_KEY)
    parser = RecordParser()
    assert parser.feed(raw[:11]) == []
    (record,) = parser.feed(raw[11:])
    assert record.value == TEST_KEY


def test_handshake_answers():
    records, _ = parse("029f000001000301ff", "07000300" + "07000300")
    assert records[0].kind == 2 and records[0].value == [0, 0, 1, 0, 3, 1]
    assert [(r.kind, r.vreg, r.value) for r in records[1:]] == [(7, 3, 0), (7, 3, 0)]


def test_unknown_record_kind_drops_the_rest_of_the_chunk_but_recovers():
    parser = RecordParser()
    assert parser.feed(bytes.fromhex("ee0102030405")) == []
    assert parser.dropped == 6
    assert [r.vreg for r in parser.feed(bytes.fromhex("080319ed8d423305"))] == [0xED8D]


def test_garbage_cbor_does_not_raise():
    parser = RecordParser()
    assert parser.feed(bytes.fromhex("0800ff")) == []  # a CBOR break byte where an id should be


def test_decode_registers():
    assert decode_vreg(0xED8D, b"\x33\x05").value == 13.31
    assert decode_vreg(0xEC5A, (55501134).to_bytes(4, "little")).value == 55501134
    assert decode_vreg(0xEC66, bytes.fromhex("f748188a779d")).value == "f7:48:18:8a:77:9d"
    assert decode_vreg(0x0102, bytes.fromhex("00640100")).value == "00640100"
    assert decode_vreg(0x2211, bytes.fromhex("2003")).value == 8.0 and decode_vreg(0x2212, bytes.fromhex("cc06")).value == 17.4


def test_temperature_not_available_and_value():
    assert decode_vreg(0xEDEC, bytes.fromhex("ffff")).value is None
    assert decode_vreg(0xEDEC, (29815).to_bytes(2, "little")).value == 25.0  # 298.15 K


def test_the_key_is_never_decoded_into_a_value():
    assert decode_vreg(VREG_KEY, TEST_KEY) is None


def test_unknown_registers_are_kept_raw():
    value = decode_vreg(0xED8C, bytes.fromhex("d8ffffff"))
    assert (value.name, value.value) == ("vreg_ed8c", "d8ffffff")
    assert decode_vreg(0xBEEF, 5).value == 5


def test_values_that_cannot_be_decoded_are_skipped():
    assert decode_vreg(0xED8D, "text") is None


def test_vreg_table():
    rows = vreg_table()
    assert rows == sorted(rows, key=lambda r: r["id"]) and all(set(r) == {"id", "name", "friendly", "unit", "verified"} for r in rows)
    assert not any(r["id"] == "0xec65" for r in rows)
