import pytest

from smartsolar import daemon, gatt_manager, gatt_session


@pytest.fixture(autouse=True)
def fast_collect(monkeypatch):
    monkeypatch.setattr(gatt_manager, "MIN_COLLECT_S", 0.05)


@pytest.fixture(autouse=True)
def fast_init(monkeypatch):
    monkeypatch.setattr(gatt_session, "INIT_WRITE_GAP_S", 0.0)


@pytest.fixture(autouse=True)
def fast_daemon(monkeypatch):
    monkeypatch.setattr(daemon, "SELECTION_INTERVAL_S", 0.02)
    monkeypatch.setattr(daemon, "SCAN_CHECK_S", 0.05)
    monkeypatch.setattr(daemon, "SCAN_RETRY_S", 0.05)
