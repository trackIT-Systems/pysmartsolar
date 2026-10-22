import logging

from smartsolar.failures import Failures

log = logging.getLogger("test.failures")


def levels(caplog, text):
    return [r.levelname for r in caplog.records if text in r.getMessage()]


def test_a_repeated_failure_is_logged_once_and_its_recovery_once(caplog):
    failures = Failures(log)
    with caplog.at_level(logging.DEBUG, logger="test.failures"):
        assert not failures.succeeded("modem", "Modem works again")  # nothing failed: nothing to say
        for _ in range(3):
            failures.failed("modem", "timeout", "Cannot reach the modem: %s", "timeout")
        assert failures.failing("modem")
        assert failures.succeeded("modem", "Modem works again")
        assert not failures.succeeded("modem", "Modem works again")
    assert levels(caplog, "Cannot reach the modem") == ["WARNING", "DEBUG", "DEBUG"]
    assert levels(caplog, "works again") == ["INFO"]
    assert not failures.failing("modem")


def test_a_changed_error_is_logged_again_and_a_new_failure_after_a_recovery(caplog):
    failures = Failures(log)
    with caplog.at_level(logging.DEBUG, logger="test.failures"):
        for error in ("timeout", "timeout", "refused", "refused"):
            failures.failed("modem", error, "Cannot reach the modem: %s", error, level=logging.INFO)
        failures.succeeded("modem", "Modem works again")
        failures.failed("modem", "refused", "Cannot reach the modem: %s", "refused", level=logging.INFO)
    assert levels(caplog, "Cannot reach the modem") == ["INFO", "DEBUG", "INFO", "DEBUG", "INFO"]


def test_kinds_are_kept_apart(caplog):
    failures = Failures(log)
    with caplog.at_level(logging.DEBUG, logger="test.failures"):
        failures.failed("a", "timeout", "A failed")
        failures.failed("b", "timeout", "B failed")
    assert [r.levelname for r in caplog.records] == ["WARNING", "WARNING"]


def test_an_exception_has_a_traceback_only_the_first_time(caplog):
    failures = Failures(log)
    with caplog.at_level(logging.DEBUG, logger="test.failures"):
        for message in ("boom", "boom", "bang"):
            try:
                raise RuntimeError(message)
            except RuntimeError:
                failures.exception("callback", "Cannot emit the %s event", "connected")
    assert [(r.levelname, r.exc_info is not None) for r in caplog.records] == [("ERROR", True), ("DEBUG", False), ("ERROR", True)]
    assert caplog.records[1].getMessage() == "Cannot emit the connected event again: RuntimeError: boom"


def test_the_location_is_the_caller(caplog):
    failures = Failures(log)
    with caplog.at_level(logging.DEBUG, logger="test.failures"):
        failures.failed("a", "timeout", "A failed")
    assert caplog.records[0].funcName == "test_the_location_is_the_caller"
