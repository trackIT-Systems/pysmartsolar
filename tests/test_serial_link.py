"""The pyserial port of a VE.Direct cable: against a pseudo-terminal where there is one, and its failures."""

import asyncio
import os
import sys

import pytest
import serial
from fakes import run

from smartsolar import serial_link
from smartsolar.errors import NotConnectedError


@pytest.fixture
def pty():
    """A pseudo-terminal: the daemon opens the follower like a cable, the test plays the device on the leader."""
    if not hasattr(os, "openpty"):
        pytest.skip("no pseudo-terminals here")
    import tty

    leader, follower = os.openpty()
    tty.setraw(leader)
    tty.setraw(follower)
    yield leader, os.ttyname(follower)
    os.close(leader)
    os.close(follower)


def test_bytes_go_both_ways_until_the_port_is_closed(pty):
    leader, port = pty

    async def go():
        link = await serial_link.open_pyserial(port, 19200)
        os.write(leader, b"\r\nV\t13310\r\n")
        got = b""
        while len(got) < 11:
            got += await asyncio.wait_for(link.read(), 5.0)
        assert got == b"\r\nV\t13310\r\n"
        # the flush waits for the other side to read (macOS): read while writing
        received = asyncio.create_task(asyncio.to_thread(os.read, leader, 64))
        await asyncio.wait_for(link.write(b":154\n"), 5.0)
        assert await asyncio.wait_for(received, 5.0) == b":154\n"
        await link.close()
        with pytest.raises(NotConnectedError, match="closed"):
            await link.read()

    run(go())


def test_a_read_waits_through_quiet_periods(pty, monkeypatch):
    leader, port = pty
    monkeypatch.setattr(serial_link, "READ_TIMEOUT_S", 0.05)

    async def go():
        link = await serial_link.open_pyserial(port, 19200)
        asyncio.get_running_loop().call_later(0.3, os.write, leader, b"x")  # several empty reads first
        assert await asyncio.wait_for(link.read(), 5.0) == b"x"
        await link.close()

    run(go())


def test_the_port_is_opened_exclusively(pty):
    _, port = pty
    if sys.platform.startswith("win"):
        pytest.skip("Windows opens a COM port exclusively anyway")

    async def go():
        first = await serial_link.open_pyserial(port, 19200)
        with pytest.raises(Exception, match="cannot open"):
            await serial_link.open_pyserial(port, 19200)
        await first.close()

    run(go())


class BrokenSerial:
    """A port that fails like an unplugged USB adapter."""

    in_waiting = 0

    def __init__(self, error):
        self.error = error

    def read(self, n):
        raise self.error

    def write(self, data):
        raise self.error

    def flush(self): ...

    def close(self):
        raise OSError("already gone")


@pytest.mark.parametrize("error", [serial.SerialException("device reports readiness to read but returned no data"), OSError(5, "Input/output error"), TypeError("NoneType")])
def test_a_port_that_fails_is_not_connected(error):
    async def go():
        link = serial_link.PySerialLink(BrokenSerial(error))
        with pytest.raises(NotConnectedError, match="serial read failed"):
            await link.read()
        with pytest.raises(NotConnectedError, match="serial write failed"):
            await link.write(b":154\n")
        await link.close()  # a failing close is only logged

    run(go())


def test_the_package_runs_as_a_module(monkeypatch, capsys):
    import runpy

    monkeypatch.setattr(sys, "argv", ["smartsolar", "--version"])
    with pytest.raises(SystemExit) as exit:
        runpy.run_module("smartsolar", run_name="__main__")
    assert exit.value.code == 0 and capsys.readouterr().out.startswith("smartsolar ")
