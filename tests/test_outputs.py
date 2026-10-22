import json
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import cbor2

from fakes import FakeClock, FakeMqtt, FakeScheduler
from smartsolar.columns import COLUMNS
from smartsolar.config import MqttConfig
from smartsolar.outputs import Outputs
from smartsolar.outputs.csv import CsvOutput
from smartsolar.outputs.formats import encode_cbor, encode_csv, encode_json, reading_record
from smartsolar.outputs.mqtt import MqttOutput
from smartsolar.reading import Event, Reading, Value

TZ = timezone(timedelta(hours=2))
WHEN = datetime(2026, 10, 13, 12, 3, 0, 123000, tzinfo=TZ)
# How the outputs write WHEN: in the local time zone (the runner's), whole seconds
LOCAL = WHEN.astimezone().replace(microsecond=0).isoformat()
MAC = "F7:48:18:8A:77:9D"
TOPIC = "tracker/smartsolar/f748188a779d"


def reading(block="advert", voltage=13.31, when=WHEN):
    values = (
        Value("battery_voltage", "Battery voltage", "V", voltage),
        Value("device_state", "Device state", "", "off"),
        Value("pv_power", "PV power", "W", None),
    )
    return Reading(MAC, block, when, values, boottime=50.0)


def mqtt(**config):
    client, scheduler, now = FakeMqtt(), FakeScheduler(), [1000.0]
    out = MqttOutput(
        MqttConfig(**config), hostname="tracker", client=client, clock=FakeClock(synced=True),
        monotonic=lambda: now[0], scheduler=scheduler,
    )
    return out, client, scheduler, now


def sent(client):
    """battery_voltage of every JSON reading published, in order."""
    return [json.loads(p)["battery_voltage"] for t, p, _ in client.published if t == f"{TOPIC}/json"]


# --- encodings


def test_all_formats_carry_the_same_columns_in_the_same_order():
    record = reading_record(reading(), synced=True)
    as_json = json.loads(encode_json(record))
    as_cbor = cbor2.loads(encode_cbor(record))
    as_csv = encode_csv(record).decode().split(";")
    assert list(as_json)[:4] == ["time", "synced", "device", COLUMNS[0]]
    assert len(as_json) == len(as_cbor) == len(as_csv) == 3 + len(COLUMNS)
    assert list(as_json.values())[1:] == as_cbor[1:]
    assert as_json["battery_voltage"] == 13.31 and as_json["pv_power"] is None
    assert "block" not in as_json and "source" not in as_json


def test_time_has_its_offset_and_whole_seconds_and_cbor_uses_tag_0():
    record = reading_record(reading(), synced=False)
    assert json.loads(encode_json(record))["time"] == LOCAL
    assert encode_csv(record).startswith(f"{LOCAL};False;F7:48:18:8A:77:9D;".encode())
    raw = encode_cbor(record)
    assert raw[:3] == bytes([0x98, 3 + len(COLUMNS), 0xC0])  # array header (2 bytes), then tag 0
    assert cbor2.loads(raw)[0].utcoffset() == WHEN.astimezone().utcoffset()


# --- MQTT


def test_mqtt_publishes_csv_json_and_cbor_per_device_without_the_source():
    out, client, _, _ = mqtt()
    assert client.connected_to == ("localhost", 1883) and client.looping
    out.emit_reading(reading())
    assert [t for t, _, _ in client.published] == [f"{TOPIC}/{fmt}" for fmt in ("csv", "json", "cbor")]
    assert not any(retain for _, _, retain in client.published)


def test_a_serial_number_is_the_topic():
    out, client, _, _ = mqtt()
    out.emit_reading(replace(reading(), device="HQ22294ZGEU"))
    assert {t for t, _, _ in client.published} == {f"tracker/smartsolar/HQ22294ZGEU/{fmt}" for fmt in ("csv", "json", "cbor")}


def test_mqtt_holds_back_readings_within_the_interval_and_sends_the_newest():
    out, client, scheduler, now = mqtt(publish_interval_s=300)
    out.emit_reading(reading(voltage=1.0))  # first: at once
    now[0] += 60
    out.emit_reading(reading(voltage=2.0))
    now[0] += 60
    out.emit_reading(reading(voltage=3.0))
    assert sent(client) == [1.0]
    assert [round(h.delay) for h in scheduler.calls] == [240]
    now[0] += 180
    scheduler.run_due()
    assert sent(client) == [1.0, 3.0]
    scheduler.run_due()  # nothing pending: nothing sent again
    assert sent(client) == [1.0, 3.0]


def test_mqtt_prefers_the_best_source_held_back():
    out, client, scheduler, now = mqtt(publish_interval_s=300)
    out.emit_reading(reading("advert", 1.0))
    now[0] += 60
    out.emit_reading(reading("vedirect", 2.0))
    now[0] += 30
    out.emit_reading(reading("advert", 3.0))  # newer, but a worse source
    now[0] += 210
    scheduler.run_due()
    assert sent(client) == [1.0, 2.0]


def test_mqtt_waits_for_an_active_better_source_when_due():
    out, client, scheduler, now = mqtt(publish_interval_s=300)
    out.emit_reading(reading("vedirect", 1.0))
    now[0] += 400  # due
    out.emit_reading(reading("advert", 2.0))  # due, but VE.Direct delivered 400 s ago: gone, so at once
    assert sent(client) == [1.0, 2.0]
    now[0] += 310
    out.emit_reading(reading("vedirect", 3.0))
    now[0] += 5
    out.emit_reading(reading("advert", 4.0))  # VE.Direct is active: held back
    assert sent(client) == [1.0, 2.0, 3.0]
    now[0] += 300
    out.emit_reading(reading("vedirect", 5.0))  # due, the best source: at once, replacing the advert
    assert sent(client) == [1.0, 2.0, 3.0, 5.0]
    assert all(h.cancelled for h in scheduler.calls)


def test_mqtt_interval_0_publishes_every_reading():
    out, client, scheduler, _ = mqtt(publish_interval_s=0)
    for block in ("advert", "gatt", "vedirect"):
        out.emit_reading(reading(block))
    assert len(sent(client)) == 3 and not scheduler.calls


def test_mqtt_close_sends_what_is_held_back():
    out, client, scheduler, now = mqtt(publish_interval_s=300)
    out.emit_reading(reading(voltage=1.0))
    now[0] += 10
    out.emit_reading(reading(voltage=2.0))
    out.close()
    assert sent(client) == [1.0, 2.0] and client.looping is False


def test_mqtt_events_in_three_formats_state_retained_as_map_and_will_offline():
    out, client, _, _ = mqtt()
    assert {(t, r) for t, _, r in client.wills} == {("tracker/smartsolar/state/json", True), ("tracker/smartsolar/state/cbor", True)}
    out.emit_state({"state": "running"})
    out.emit_event(Event(WHEN, "device_selected", MAC, {"rssi": -60}))
    retained = {t: r for t, _, r in client.published}
    assert retained["tracker/smartsolar/state/json"] is True and "tracker/smartsolar/state/csv" not in retained
    assert retained["tracker/smartsolar/events/cbor"] is False
    event = json.loads({t: p for t, p, _ in client.published}["tracker/smartsolar/events/json"])
    assert event == {"time": LOCAL, "synced": True, "device": MAC, "event": "device_selected", "detail": "rssi=-60"}


# --- CSV files


def test_csv_one_file_per_day_for_all_sources_with_the_block(tmp_path):
    out = CsvOutput(tmp_path, "tracker", clock=FakeClock(synced=True))
    out.emit_reading(reading("advert"))
    out.emit_reading(reading("vedirect"))
    out.emit_reading(reading("gatt", when=WHEN + timedelta(days=1)))
    base = tmp_path / "tracker" / "smartsolar"
    day1 = (base / "tracker_2026-10-13_smartsolar.csv").read_text(encoding="utf-8").splitlines()
    assert day1[0] == ";".join(["time", "synced", "device", "block", *COLUMNS])
    assert day1[1].startswith(f"{LOCAL};True;F7:48:18:8A:77:9D;advert;")
    assert day1[2].split(";")[3] == "vedirect" and len(day1) == 3
    assert (base / "tracker_2026-10-14_smartsolar.csv").exists()


def test_csv_logs_the_listed_events_in_their_own_file(tmp_path):
    out = CsvOutput(tmp_path, "tracker", clock=FakeClock(synced=True))
    out.emit_event(Event(WHEN, "key_fetched", MAC))
    out.emit_event(Event(WHEN, "gatt_failed", MAC, {"error": "timeout"}))  # not logged
    out.emit_state({"state": "running"})
    lines = (tmp_path / "tracker/smartsolar/events/tracker_2026-10-13_smartsolar-events.csv").read_text(encoding="utf-8").splitlines()
    assert lines == ["time;synced;device;event;detail", f"{LOCAL};True;{MAC};key_fetched;"]


def test_a_failing_sink_does_not_affect_the_others():
    class Broken:
        def emit_reading(self, r):
            raise RuntimeError("boom")

        emit_event = emit_state = close = emit_reading

    got = []

    class Good:
        def emit_reading(self, r):
            got.append(r)

        def emit_event(self, e): ...
        def emit_state(self, s): ...
        def close(self): ...

    Outputs([Broken(), Good()]).emit_reading(reading())
    assert len(got) == 1


def test_a_failing_sink_is_logged_with_a_traceback_once_and_its_recovery_once(caplog):
    class Flaky:
        broken = True

        def emit_reading(self, r):
            if self.broken:
                raise RuntimeError("boom")

        emit_event = emit_state = close = emit_reading

    sink = Flaky()
    outputs = Outputs([sink])
    with caplog.at_level(logging.DEBUG, logger="smartsolar.outputs"):
        for _ in range(3):
            outputs.emit_reading(reading())
        sink.broken = False
        outputs.emit_reading(reading())
        outputs.emit_reading(reading())
    assert [(r.levelname, r.exc_info is not None) for r in caplog.records] == [("ERROR", True), ("DEBUG", False), ("DEBUG", False), ("INFO", False)]
    assert caplog.records[0].getMessage() == "Output Flaky failed in emit_reading"
    assert caplog.records[-1].getMessage() == "Output Flaky works again in emit_reading"
