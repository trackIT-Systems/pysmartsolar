import logging
import os

import pytest

from smartsolar import journal
from smartsolar.journal import log_handler, stderr_is_journal


class FakeJournalHandler(logging.Handler):
    """Stands in for python3-systemd's JournalHandler, which does not install off the station."""

    def __init__(self, **fields):
        super().__init__()
        self.fields = fields


def record() -> logging.LogRecord:
    return logging.makeLogRecord({"name": "smartsolar.x", "levelname": "WARNING", "msg": "Careful"})


def test_stderr_is_journal(monkeypatch, tmp_path):
    monkeypatch.delenv("JOURNAL_STREAM", raising=False)
    assert not stderr_is_journal()

    with open(tmp_path / "stderr", "w") as stderr:
        stat = os.fstat(stderr.fileno())
        monkeypatch.setattr(journal.sys, "stderr", stderr)
        monkeypatch.setenv("JOURNAL_STREAM", f"{stat.st_dev}:{stat.st_ino}")
        assert stderr_is_journal()
        monkeypatch.setenv("JOURNAL_STREAM", f"{stat.st_dev}:{stat.st_ino + 1}")
        assert not stderr_is_journal()
        monkeypatch.setenv("JOURNAL_STREAM", "garbage")
        assert not stderr_is_journal()


def test_log_handler_uses_journal_under_systemd(monkeypatch):
    monkeypatch.setattr(journal, "stderr_is_journal", lambda: True)
    monkeypatch.setattr(journal, "JournalHandler", FakeJournalHandler)

    handler = log_handler("smartsolard")

    assert isinstance(handler, FakeJournalHandler)
    assert handler.fields == {"SYSLOG_IDENTIFIER": "smartsolard"}
    assert handler.format(record()) == "smartsolar.x: Careful"


def test_log_handler_on_terminal(monkeypatch):
    monkeypatch.setattr(journal, "stderr_is_journal", lambda: False)
    monkeypatch.setattr(journal, "JournalHandler", FakeJournalHandler)

    handler = log_handler("smartsolard")

    assert type(handler) is logging.StreamHandler
    assert handler.format(record()) == "WARNING smartsolar.x: Careful"


def test_log_handler_without_python_systemd(monkeypatch):
    monkeypatch.setattr(journal, "stderr_is_journal", lambda: True)
    monkeypatch.setattr(journal, "JournalHandler", None)

    handler = log_handler("smartsolard")

    assert type(handler) is logging.StreamHandler


def test_journal_fields(monkeypatch):
    """The fields python3-systemd sends; runs where it is installed (on a station)."""
    pytest.importorskip("systemd.journal")
    monkeypatch.setattr(journal, "stderr_is_journal", lambda: True)
    handler = log_handler("smartsolard")
    sent = []
    handler.send = lambda MESSAGE, **fields: sent.append({"MESSAGE": MESSAGE, **fields})
    logger = logging.getLogger("test.journal")
    monkeypatch.setattr(logger, "handlers", [handler])
    monkeypatch.setattr(logger, "propagate", False)
    logger.setLevel(logging.DEBUG)

    logger.warning("Cannot open %s", "/dev/ttyUSB0")
    try:
        raise ValueError("broken")
    except ValueError:
        logger.exception("Failed")
    warning, error = sent

    assert warning["MESSAGE"] == "test.journal: Cannot open /dev/ttyUSB0"
    assert warning["PRIORITY"] == "4"
    assert warning["SYSLOG_IDENTIFIER"] == "smartsolard"
    assert warning["CODE_FILE"] == __file__
    assert warning["CODE_FUNC"] == "test_journal_fields"
    assert warning["CODE_LINE"] > 0
    # A traceback is part of MESSAGE, one entry
    assert error["PRIORITY"] == "3"
    assert error["MESSAGE"].startswith("test.journal: Failed\nTraceback")
    assert error["MESSAGE"].endswith("ValueError: broken")
