import json
import stat

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
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
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
