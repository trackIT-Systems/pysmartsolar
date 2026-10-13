import json
import stat
import sys

from fakes import A, OTHER_KEY, TEST_KEY
from smartsolar.keys import SOURCE_CONFIG, SOURCE_FETCHED, SOURCE_MISSING, KeyStore


def test_missing(tmp_path):
    assert KeyStore({}, tmp_path).get(A) == (None, SOURCE_MISSING)


def test_configured_key(tmp_path):
    assert KeyStore({A: TEST_KEY}, tmp_path).get(A.lower()) == (TEST_KEY, SOURCE_CONFIG)


def test_store_persists_with_owner_only_permissions(tmp_path):
    store = KeyStore({}, tmp_path / "state")
    store.store(A, TEST_KEY)
    assert store.get(A) == (TEST_KEY, SOURCE_FETCHED)
    path = tmp_path / "state" / "keys.json"
    assert sys.platform.startswith("win") or stat.S_IMODE(path.stat().st_mode) == 0o600
    assert KeyStore({}, tmp_path / "state").get(A) == (TEST_KEY, SOURCE_FETCHED)  # survives a restart
    assert [p.name for p in (tmp_path / "state").iterdir()] == ["keys.json"]  # no temp files left


def test_the_config_wins_over_a_fetched_key(tmp_path):
    KeyStore({}, tmp_path).store(A, OTHER_KEY)
    assert KeyStore({A: TEST_KEY}, tmp_path).get(A) == (TEST_KEY, SOURCE_CONFIG)


def test_forget(tmp_path):
    store = KeyStore({}, tmp_path)
    store.store(A, TEST_KEY)
    assert store.forget(A) is True and store.forget(A) is False
    assert KeyStore({}, tmp_path).get(A) == (None, SOURCE_MISSING)
    store = KeyStore({A: TEST_KEY}, tmp_path)
    assert store.forget(A) is False and store.get(A)[1] == SOURCE_CONFIG  # a configured key is not ours to drop


def test_a_broken_state_file_is_ignored(tmp_path):
    (tmp_path / "keys.json").write_text("{not json")
    assert KeyStore({}, tmp_path).get(A) == (None, SOURCE_MISSING)
    (tmp_path / "keys.json").write_text(json.dumps({A: "tooshort", "E1:22:33:44:55:66": TEST_KEY.hex()}))
    store = KeyStore({}, tmp_path)
    assert store.get(A)[0] is None and store.get("E1:22:33:44:55:66")[0] == TEST_KEY
    (tmp_path / "keys.json").write_text("[1, 2]")
    assert KeyStore({}, tmp_path).get(A)[0] is None


def test_keys_are_found_by_serial_number_or_address(tmp_path):
    name = "SmartSolar HQ22294ZGEU"
    uuid = "6D2F8A3C-1B4E-4F7A-9C0D-5E8B7A6F1234"  # macOS: no MAC address, a UUID per host
    store = KeyStore({"HQ22294ZGEU": TEST_KEY}, tmp_path)
    assert store.get(uuid, name) == (TEST_KEY, SOURCE_CONFIG)  # by the serial in the name
    assert store.get(A, name) == (TEST_KEY, SOURCE_CONFIG)  # whatever the address
    assert store.get(uuid, "SmartSolar") == (None, SOURCE_MISSING)  # no serial in the name: nothing to go by
    only_mac = KeyStore({A: TEST_KEY}, tmp_path)
    assert only_mac.get(A, name) == (TEST_KEY, SOURCE_CONFIG)  # a key by MAC still works


def test_a_fetched_key_is_stored_under_the_serial_so_it_survives_an_address_change(tmp_path):
    name = "SmartSolar HQ22294ZGEU"
    store = KeyStore({}, tmp_path)
    store.store(A, TEST_KEY, name)
    assert json.loads((tmp_path / "keys.json").read_text()) == {"HQ22294ZGEU": TEST_KEY.hex()}
    again = KeyStore({}, tmp_path)
    assert again.get("6D2F8A3C-1B4E-4F7A-9C0D-5E8B7A6F1234", name) == (TEST_KEY, SOURCE_FETCHED)
    assert again.forget(A, name) and again.get(A, name) == (None, SOURCE_MISSING)
    old = KeyStore({}, tmp_path)
    old.store(A, TEST_KEY)  # a key stored before the name was known stays under the address
    assert old.get(A, name) == (TEST_KEY, SOURCE_FETCHED)
