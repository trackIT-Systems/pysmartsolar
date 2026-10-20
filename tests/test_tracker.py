from datetime import datetime

from fakes import A, B, OTHER_KEY, TEST_KEY, WRONG_CHECK_KEY, advert
from smartsolar.config import AdvertConfig, BleConfig
from smartsolar.keys import KeyStore
from smartsolar.state import AdvertStats, Cache
from smartsolar.tracker import DeviceTracker


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make(tmp_path, *, keys=None, ble=None, clock=None):
    clock = clock or Clock()
    events, switched, selected = [], [], []
    cache = Cache({"advert": 120.0, "gatt": None}, clock=clock)
    tracker = DeviceTracker(
        ble or BleConfig(select_window_s=10),
        AdvertConfig(),
        KeyStore({A: TEST_KEY} if keys is None else keys, tmp_path),
        cache,
        events.append,
        AdvertStats(started=datetime.now().astimezone()),
        on_switch=lambda: switched.append(1),
        on_select=lambda: selected.append(1),
        clock=clock,
    )
    return tracker, cache, events, switched, selected, clock


def feed(tracker, clock, address, rssi, *, seconds=0.0, **kw):
    clock.now += seconds
    tracker.on_advert(advert(address, rssi=rssi, **kw))


def test_selects_the_strongest_after_the_window(tmp_path):
    tracker, cache, events, _, selected, clock = make(tmp_path, keys={A: TEST_KEY, B: TEST_KEY})
    feed(tracker, clock, A, -70)
    feed(tracker, clock, B, -50, seconds=1)
    tracker.review()
    assert tracker.current_address is None  # still listening
    clock.now += 10
    tracker.review()
    assert tracker.current_address == B and selected == [1]
    assert [e.kind for e in events] == ["device_selected"] and events[0].data["rssi"] == -50


def test_a_pinned_device_is_selected_at_once_and_others_are_ignored(tmp_path):
    tracker, _, _, _, _, clock = make(tmp_path, ble=BleConfig(address=A))
    feed(tracker, clock, B, -30)
    tracker.review()
    assert tracker.current_address is None
    feed(tracker, clock, A, -80)
    tracker.review()
    assert tracker.current_address == A


def test_only_the_selected_device_is_decoded(tmp_path):
    tracker, cache, _, _, _, clock = make(tmp_path, keys={A: TEST_KEY, B: TEST_KEY})
    feed(tracker, clock, A, -50, voltage=12.5)
    clock.now += 10
    tracker.review()
    feed(tracker, clock, B, -40, voltage=14.0)
    feed(tracker, clock, A, -50, voltage=12.6, nonce=2)
    reading = cache.blocks["advert"].reading
    assert reading.device == A and reading.block == "advert"
    assert {v.name: v.value for v in reading.values}["battery_voltage"] == 12.6
    assert tracker.stats.seen == 1 and tracker.stats.decoded == 1


def test_values_include_units_and_none_for_unavailable(tmp_path):
    tracker, cache, *_ , clock = make(tmp_path)
    feed(tracker, clock, A, -50, state=5, voltage=13.6, pv_w=40)
    clock.now += 10
    tracker.review()
    feed(tracker, clock, A, -50, state=5, voltage=13.6, pv_w=40, nonce=2)
    values = {v.name: v.value for v in cache.blocks["advert"].reading.values}
    assert values["device_state"] == "float" and values["battery_voltage"] == 13.6 and values["pv_power"] == 40
    units = {v.name: v.unit for v in cache.blocks["advert"].reading.values}
    assert units["battery_voltage"] == "V" and units["pv_power"] == "W"


def test_switch_needs_the_hysteresis_and_the_review_interval(tmp_path):
    tracker, cache, events, switched, _, clock = make(tmp_path, keys={A: TEST_KEY, B: TEST_KEY}, ble=BleConfig(select_window_s=0, rescan_interval_s=100, switch_hysteresis_db=10))
    feed(tracker, clock, A, -60)
    tracker.review()
    assert tracker.current_address == A
    for _ in range(30):  # B becomes much stronger; the EMA follows
        feed(tracker, clock, B, -40)
        feed(tracker, clock, A, -60)
    clock.now += 50
    tracker.review()
    assert tracker.current_address == A  # not reviewed yet
    clock.now += 60
    feed(tracker, clock, B, -40)
    feed(tracker, clock, A, -60)
    tracker.review()
    assert tracker.current_address == B and switched == [1]
    assert events[-1].kind == "device_switched" and events[-1].data["previous"] == A


def test_no_switch_within_the_hysteresis(tmp_path):
    tracker, _, _, switched, _, clock = make(tmp_path, ble=BleConfig(select_window_s=0, rescan_interval_s=10, switch_hysteresis_db=10))
    for _ in range(30):
        feed(tracker, clock, A, -60)
        feed(tracker, clock, B, -55)
    tracker.review()
    first = tracker.current_address
    for _ in range(2):
        clock.now += 20
        for _ in range(30):
            feed(tracker, clock, A, -60)
            feed(tracker, clock, B, -55)
        tracker.review()
    assert tracker.current_address == first and switched == []


def test_a_review_without_any_eligible_device_does_nothing(tmp_path):
    tracker, _, _, switched, _, clock = make(tmp_path, ble=BleConfig(select_window_s=0, rescan_interval_s=10))
    feed(tracker, clock, A, -60)
    tracker.review()
    clock.now += 100  # the current device went quiet
    tracker.review()
    assert tracker.current_address == A and switched == []


def test_reselect_ignores_the_hysteresis(tmp_path):
    tracker, _, _, switched, _, clock = make(tmp_path, ble=BleConfig(select_window_s=0, switch_hysteresis_db=50))
    feed(tracker, clock, A, -60)
    tracker.review()
    for _ in range(30):
        feed(tracker, clock, B, -50)
        feed(tracker, clock, A, -60)
    tracker.reselect()
    assert tracker.current_address == B and switched == [1]


def test_other_victron_devices_are_listed_but_never_selected(tmp_path):
    tracker, _, _, _, _, clock = make(tmp_path, ble=BleConfig(select_window_s=0))
    feed(tracker, clock, B, -30, record_type=0x02)  # e.g. a battery monitor
    tracker.review()
    assert tracker.current_address is None
    row = tracker.scan_table()[0]
    assert row["address"] == B and row["solar_charger"] is False and row["record_type"] == 2 and row["current"] is False


def test_devices_that_went_quiet_are_not_eligible(tmp_path):
    tracker, _, _, _, _, clock = make(tmp_path, ble=BleConfig(select_window_s=0))
    feed(tracker, clock, A, -50)
    clock.now += 60
    tracker.review()
    assert tracker.current_address is None


def test_no_key_is_counted_and_nothing_is_decoded(tmp_path):
    tracker, cache, _, _, _, clock = make(tmp_path, keys={}, ble=BleConfig(select_window_s=0))
    feed(tracker, clock, A, -50)
    tracker.review()
    feed(tracker, clock, A, -50, nonce=2)
    assert tracker.stats.no_key == 1 and cache.blocks["advert"].reading is None


def test_a_wrong_configured_key_is_rejected_but_kept(tmp_path):
    tracker, cache, events, _, _, clock = make(tmp_path, keys={A: WRONG_CHECK_KEY}, ble=BleConfig(select_window_s=0))
    feed(tracker, clock, A, -50)
    tracker.review()
    for n in range(10):
        feed(tracker, clock, A, -50, nonce=n)
    assert tracker.stats.rejected == 10 and cache.blocks["advert"].reading is None
    assert tracker.keys.get(A)[0] == WRONG_CHECK_KEY and not any(e.kind == "key_rejected" for e in events)


def test_a_fetched_key_that_stops_matching_is_forgotten(tmp_path):
    store = KeyStore({}, tmp_path)
    store.store(A, WRONG_CHECK_KEY)
    tracker, _, events, _, _, clock = make(tmp_path, keys={}, ble=BleConfig(select_window_s=0))
    feed(tracker, clock, A, -50)
    tracker.review()
    for n in range(6):
        feed(tracker, clock, A, -50, nonce=n)
    assert tracker.keys.get(A)[0] is None
    assert [e.kind for e in events if e.kind == "key_rejected"] == ["key_rejected"]


def test_a_good_advert_resets_the_mismatch_count(tmp_path):
    store = KeyStore({}, tmp_path)
    store.store(A, TEST_KEY)
    tracker, cache, _, _, _, clock = make(tmp_path, keys={}, ble=BleConfig(select_window_s=0))
    feed(tracker, clock, A, -50)
    tracker.review()
    tracker._mismatches = 4
    feed(tracker, clock, A, -50, nonce=9)
    assert tracker._mismatches == 0 and cache.blocks["advert"].reading is not None


def test_an_undecodable_record_type_is_counted(tmp_path):
    tracker, _, _, _, _, clock = make(tmp_path, ble=BleConfig(select_window_s=0))
    feed(tracker, clock, A, -50)
    tracker.review()
    feed(tracker, clock, A, -50, record_type=0x07)
    assert tracker.stats.undecodable == 1


def test_advert_rate(tmp_path):
    tracker, _, _, _, _, clock = make(tmp_path, ble=BleConfig(select_window_s=0))
    feed(tracker, clock, A, -50)
    tracker.review()
    for _ in range(40):
        feed(tracker, clock, A, -50, seconds=0.25)
    assert 3.5 * 60 <= tracker.rate_per_min() <= 4.5 * 60
    clock.now += 120
    assert tracker.rate_per_min() == 0.0


def test_scan_table_marks_the_selected_device_and_the_key(tmp_path):
    tracker, _, _, _, _, clock = make(tmp_path, ble=BleConfig(select_window_s=0))
    feed(tracker, clock, A, -50)
    feed(tracker, clock, B, -70)
    tracker.review()
    rows = {r["address"]: r for r in tracker.scan_table()}
    assert rows[A]["current"] and rows[A]["key"] == "config" and rows[B]["key"] == "missing"
    assert rows[A]["model"] == "0xa053" and rows[A]["solar_charger"] is True


def test_the_serial_number_from_the_cable_selects_the_matching_device_whatever_its_signal(tmp_path):
    clock = Clock()
    tracker, cache, events, switched = make(tmp_path, keys={A: TEST_KEY, B: TEST_KEY}, clock=clock)[:4]
    tracker.on_advert(advert(A, rssi=-40, name="SmartSolar HQ99999OTHR", nonce=1, at=clock.now))
    tracker.on_advert(advert(B, rssi=-80, name="SmartSolar HQ00000TEST", nonce=1, at=clock.now))
    clock.now += 11
    tracker.review()
    assert tracker.current_address == A
    tracker.set_serial("HQ00000TEST")
    assert tracker.current_address == B and switched and events[-1].kind == "device_switched"
    tracker.on_advert(advert(B, rssi=-80, name="SmartSolar HQ00000TEST", nonce=2, at=clock.now))
    assert cache.blocks["advert"].reading.device == "HQ00000TEST"
    clock.now += 4000
    tracker.on_advert(advert(A, rssi=-30, name="SmartSolar HQ99999OTHR", nonce=3, at=clock.now))
    tracker.on_advert(advert(B, rssi=-80, name="SmartSolar HQ00000TEST", nonce=3, at=clock.now))
    tracker.review()
    assert tracker.current_address == B  # the strongest is not the right one


def test_a_serial_number_nobody_advertises_changes_nothing(tmp_path):
    tracker, _, _, _, _, clock = make(tmp_path)
    feed(tracker, clock, A, -40, name="SmartSolar HQ99999OTHR")
    clock.now += 11
    tracker.review()
    tracker.set_serial("HQ00000TEST")
    assert tracker.current_address == A


def test_a_pin_can_be_a_serial_number_or_a_macos_uuid(tmp_path):
    uuid = "6D2F8A3C-1B4E-4F7A-9C0D-5E8B7A6F1234"
    for pin, wanted in (("HQ00000TEST", B), (uuid, uuid)):
        tracker, _, _, _, _, clock = make(tmp_path, keys={A: TEST_KEY, B: TEST_KEY, uuid: TEST_KEY}, ble=BleConfig(address=pin, select_window_s=0))
        feed(tracker, clock, A, -30, name="SmartSolar HQ99999OTHR")
        feed(tracker, clock, B, -80, name="SmartSolar HQ00000TEST")
        feed(tracker, clock, uuid, -85, name="SmartSolar HQ11111MAC")
        tracker.review()
        assert tracker.current_address == wanted
