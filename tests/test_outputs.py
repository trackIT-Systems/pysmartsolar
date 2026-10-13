import csv
import json
from datetime import datetime, timezone

import cbor2

from fakes import FakeMqtt
from smartsolar.config import MqttConfig
from smartsolar.outputs import Outputs
from smartsolar.outputs.csv import CsvOutput, csv_session_path
from smartsolar.outputs.mqtt import MqttOutput
from smartsolar.reading import Event, Reading, Value

WHEN = datetime(2026, 10, 13, 10, 3, 0, 123000, tzinfo=timezone.utc)
MAC = "F7:48:18:8A:77:9D"


def reading(block="advert"):
    values = (Value("battery_voltage", "Battery voltage", "V", 13.31), Value("device_state", "Device state", "", "off"), Value("pv_power", "PV power", "W", None))
    return Reading(MAC, block, WHEN, values)


def test_csv_session_path():
    path = csv_session_path("/data", "tracker", WHEN)
    assert str(path) == "/data/tracker/smartsolar/tracker_2026-10-13T100300-smartsolar.csv"


def test_csv_rows_and_events(tmp_path):
    out = CsvOutput(tmp_path / "x" / "s.csv")
    out.emit_reading(reading())
    out.emit_reading(reading("gatt"))
    out.emit_event(Event(WHEN, "key_fetched", MAC))
    out.emit_event(Event(WHEN, "gatt_failed", MAC, {"error": "timeout"}))  # not logged
    out.emit_state({"state": "running"})
    with (tmp_path / "x" / "s.csv").open() as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0] == {"ts": "2026-10-13T10:03:00.123Z", "device": MAC, "block": "advert", "key": "battery_voltage", "value": "13.31", "unit": "V"}
    assert rows[2]["value"] == "" and rows[2]["key"] == "pv_power"  # not available: empty
    assert rows[3]["block"] == "gatt"
    assert rows[-1] == {"ts": "2026-10-13T10:03:00.123Z", "device": MAC, "block": "event", "key": "key_fetched", "value": "", "unit": ""}
    assert len(rows) == 7


def test_mqtt_publishes_json_and_cbor_with_the_source():
    client = FakeMqtt()
    out = MqttOutput(MqttConfig(), hostname="tracker", client=client)
    out.emit_reading(reading())
    topics = {t: p for t, p, _ in client.published}
    assert set(topics) == {"tracker/smartsolar/f748188a779d/json", "tracker/smartsolar/f748188a779d/cbor"}
    as_json = json.loads(topics["tracker/smartsolar/f748188a779d/json"])
    assert as_json == cbor2.loads(topics["tracker/smartsolar/f748188a779d/cbor"])
    assert as_json["Battery voltage (V)"] == 13.31 and as_json["source"] == "advert" and as_json["PV power (W)"] is None


def test_mqtt_state_is_retained_and_the_will_marks_offline():
    client = FakeMqtt()
    out = MqttOutput(MqttConfig(), hostname="tracker", client=client)
    assert {(t, json.loads(p)["state"], r) for t, p, r in client.wills if t.endswith("json")} == {("tracker/smartsolar/state/json", "offline", True)}
    out.emit_state({"state": "running"})
    out.emit_event(Event(WHEN, "device_selected", MAC))
    retained = {t: r for t, _, r in client.published}
    assert retained["tracker/smartsolar/state/json"] is True and retained["tracker/smartsolar/events/json"] is False
    out.close()


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


def test_a_serial_number_is_the_topic_and_the_csv_device(tmp_path):
    from dataclasses import replace

    r = replace(reading(), device="HQ22294ZGEU")
    client = FakeMqtt()
    MqttOutput(MqttConfig(), hostname="tracker", client=client).emit_reading(r)
    assert {t for t, _, _ in client.published} == {"tracker/smartsolar/HQ22294ZGEU/json", "tracker/smartsolar/HQ22294ZGEU/cbor"}
    out = CsvOutput(tmp_path / "s.csv")
    out.emit_reading(r)
    with (tmp_path / "s.csv").open() as handle:
        assert {row["device"] for row in csv.DictReader(handle)} == {"HQ22294ZGEU"}


def test_device_ids():
    from smartsolar.reading import device_id, serial_from_name, topic_id

    assert serial_from_name("SmartSolar HQ22294ZGEU") == "HQ22294ZGEU" and serial_from_name("SmartSolar") is None and serial_from_name(None) is None
    assert device_id("F7:48:18:8A:77:9D", "SmartSolar HQ22294ZGEU") == "HQ22294ZGEU" and device_id("F7:48:18:8A:77:9D", "SmartSolar") == "F7:48:18:8A:77:9D"
    assert topic_id("F7:48:18:8A:77:9D") == "f748188a779d" and topic_id("HQ22294ZGEU") == "HQ22294ZGEU"
