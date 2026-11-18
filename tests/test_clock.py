"""Whether the kernel considers the clock synchronized (adjtimex), with a stand-in for libc."""

from types import SimpleNamespace

import pytest

from smartsolar import clock


def libc(state):
    calls = []

    def adjtimex(buf):
        calls.append(bytes(buf.raw[:8]))
        return state

    return SimpleNamespace(adjtimex=adjtimex), calls


@pytest.mark.parametrize("state, synced", [(0, True), (1, True), (4, True), (clock.TIME_ERROR, False), (-1, False)], ids=["ok", "ins", "wait", "unsync", "error"])
def test_the_kernel_clock_state(monkeypatch, state, synced):
    fake, calls = libc(state)
    monkeypatch.setattr(clock.ctypes, "CDLL", lambda name, use_errno: fake)
    assert clock.kernel_clock_synced() is synced
    assert calls == [bytes(8)]  # modes 0: only read


@pytest.mark.parametrize("error", [AttributeError("adjtimex"), OSError("no libc"), TypeError("None")])
def test_without_adjtimex_the_clock_is_trusted(monkeypatch, error):
    def missing(name, use_errno):
        raise error

    monkeypatch.setattr(clock.ctypes, "CDLL", missing)
    assert clock.kernel_clock_synced()


def test_once_synced_the_clock_stays_synced(monkeypatch):
    states = iter([False, True, False])
    monkeypatch.setattr(clock, "kernel_clock_synced", lambda: next(states))
    c = clock.Clock()
    assert [c.synced(), c.synced(), c.synced()] == [False, True, True]
    assert abs(c.time() - __import__("time").time()) < 5


def test_the_real_kernel_answers():
    assert clock.kernel_clock_synced() in (True, False)  # on Linux this really calls adjtimex
