import asyncio
import sys
import json
import os
import stat
import tempfile
from pathlib import Path

import pytest

from smartsolar import ipc
from smartsolar.errors import DaemonError, DaemonUnreachable, NotConnectedError
from smartsolar.server import ControlServer, error_type
from fakes import run


def test_error_types():
    assert error_type(NotConnectedError("x")) == "not_connected" and error_type(TimeoutError()) == "timeout" and error_type(KeyError()) == "internal"


@pytest.mark.skipif(sys.platform.startswith("win"), reason="no Unix sockets")
def test_requests_answers_errors_and_socket_permissions():
    async def go():
        tmp = Path(tempfile.mkdtemp(dir="/tmp", prefix="ss-"))
        sock = tmp / "run" / "s.sock"

        async def echo(args):
            return {"echo": args}

        async def broken(args):
            raise NotConnectedError("no link")

        async def crash(args):
            raise RuntimeError("boom")

        server = ControlServer(sock, 0o660, {"echo": echo, "broken": broken, "crash": crash})
        await server.start()
        try:
            assert stat.S_IMODE(os.stat(sock).st_mode) == 0o660
            assert await asyncio.to_thread(ipc.request, sock, "echo", {"a": 1}) == {"echo": {"a": 1}}
            for cmd, kind in (("broken", "not_connected"), ("crash", "internal"), ("nope", "unknown_command")):
                with pytest.raises(DaemonError) as err:
                    await asyncio.to_thread(ipc.request, sock, cmd)
                assert err.value.type == kind
            reader, writer = await asyncio.open_unix_connection(str(sock))
            writer.write(b"not json\n")
            await writer.drain()
            assert json.loads(await reader.readline())["error"]["type"] == "bad_request"
            writer.close()
        finally:
            await server.stop()
        assert not sock.exists()

    run(go())


def test_the_server_and_the_client_also_talk_over_loopback_tcp():
    async def go():
        import socket as _socket

        with _socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        endpoint = f"tcp://127.0.0.1:{port}"

        async def echo(args):
            return {"echo": args}

        async def broken(args):
            raise NotConnectedError("no link")

        server = ControlServer(endpoint, 0o660, {"echo": echo, "broken": broken})
        await server.start()
        try:
            assert await asyncio.to_thread(ipc.request, endpoint, "echo", {"a": 1}) == {"echo": {"a": 1}}
            with pytest.raises(DaemonError) as err:
                await asyncio.to_thread(ipc.request, endpoint, "broken")
            assert err.value.type == "not_connected"
        finally:
            await server.stop()
        with pytest.raises(DaemonUnreachable):
            await asyncio.to_thread(ipc.request, endpoint, "echo")

    run(go())


def test_a_bad_endpoint_is_reported_as_unreachable():
    with pytest.raises(DaemonUnreachable, match="loopback"):
        ipc.request("tcp://192.168.1.5:5000", "status")


# -- what goes wrong between the CLI and the daemon ----------------------------------------


def daemon_that(answer, accepted=None):
    """A loopback 'daemon' in a thread that reads one request and answers with ``answer`` (bytes, or None: hang up)."""
    import socket as _socket
    import threading

    listener = _socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    release = threading.Event()

    def serve():
        conn, _ = listener.accept()
        with conn:
            conn.recv(65536)
            if answer == "hang":
                release.wait(5)
            elif answer is not None:
                conn.sendall(answer)
        listener.close()

    threading.Thread(target=serve, daemon=True).start()
    return f"tcp://127.0.0.1:{listener.getsockname()[1]}", release


@pytest.mark.parametrize(
    "answer, message",
    [
        (None, "closed the connection without answering"),
        (b"nonsense\n", "invalid response from daemon"),
        (b"[1, 2]\n", "invalid response from daemon: message must be a JSON object"),
        (b"x" * 300, "response too large"),
        ("hang", "did not answer within 0.2 s"),
    ],
    ids=["hang up", "not json", "not an object", "too large", "no answer"],
)
def test_a_daemon_that_answers_wrongly_is_unreachable(monkeypatch, answer, message):
    monkeypatch.setattr(ipc, "MAX_RESPONSE_BYTES", 100)
    endpoint, release = daemon_that(answer)
    with pytest.raises(DaemonUnreachable, match=message):
        ipc.request(endpoint, "status", timeout=0.2)
    release.set()


def test_no_daemon_listening_is_unreachable():
    from fakes import free_port

    with pytest.raises(DaemonUnreachable, match="not accepting connections"):
        ipc.request(f"tcp://127.0.0.1:{free_port()}", "status")
    if not hasattr(__import__("socket"), "AF_UNIX"):
        return
    with pytest.raises(DaemonUnreachable, match="no daemon socket"):
        ipc.request(Path(tempfile.mkdtemp(dir="/tmp", prefix="ss-")) / "missing.sock", "status")  # short: macOS limits the path
    with pytest.raises(DaemonUnreachable, match="cannot connect to"):
        ipc.request("/tmp/" + "x" * 200 + ".sock", "status")  # longer than a Unix socket path may be


def test_an_endpoint_prints_as_it_is_written():
    assert str(ipc.parse_endpoint("tcp://127.0.0.1:7777")) == "tcp://127.0.0.1:7777"
    assert str(ipc.parse_endpoint("/run/smartsolar.sock")) == str(Path("/run/smartsolar.sock"))


@pytest.mark.skipif(sys.platform.startswith("win"), reason="no Unix sockets")
def test_bad_requests_long_requests_and_idle_clients(monkeypatch):
    import smartsolar.server as server_module

    monkeypatch.setattr(server_module, "IDLE_TIMEOUT_S", 0.2)

    async def go():
        tmp = Path(tempfile.mkdtemp(dir="/tmp", prefix="ss-"))
        sock = tmp / "s.sock"
        server = ControlServer(sock, 0o600, {})
        await server.start()
        try:
            reader, writer = await asyncio.open_unix_connection(str(sock))
            for line, expected in ((b'{"id": 3}\n', "missing cmd"), (b'{"cmd": "status", "args": [1]}\n', "args must be an object")):
                writer.write(line)
                await writer.drain()
                answer = json.loads(await reader.readline())
                assert answer["error"] == {"type": "bad_request", "message": expected}
            assert await asyncio.wait_for(reader.read(), 2.0) == b""  # idle: the server hangs up
            writer.close()

            reader, writer = await asyncio.open_unix_connection(str(sock))
            writer.write(b"x" * (ipc.MAX_REQUEST_BYTES + 10) + b"\n")
            await writer.drain()
            assert json.loads(await reader.readline())["error"]["message"] == "request too long"
            writer.close()
        finally:
            await server.stop()

    run(go())


@pytest.mark.skipif(sys.platform.startswith("win"), reason="no Unix sockets")
def test_a_stale_socket_is_replaced_and_an_unusable_path_is_an_error(monkeypatch):
    import socket as _socket

    from smartsolar.errors import SmartsolarError

    async def go():
        tmp = Path(tempfile.mkdtemp(dir="/tmp", prefix="ss-"))
        sock = tmp / "s.sock"
        stale = _socket.socket(_socket.AF_UNIX)
        stale.bind(str(sock))  # left over from a crashed daemon
        stale.close()
        server = ControlServer(sock, 0o600, {})
        await server.start()
        await server.stop()

        blocked = tmp / "dir.sock"
        blocked.mkdir()  # not a socket: not removed, and the server cannot bind there
        with pytest.raises(SmartsolarError, match="cannot create control socket"):
            await ControlServer(blocked, 0o600, {}).start()

        monkeypatch.delattr(asyncio, "start_unix_server")
        with pytest.raises(SmartsolarError, match="no Unix sockets"):
            await ControlServer(tmp / "other.sock", 0o600, {}).start()

    run(go())


def test_a_tcp_port_in_use_is_an_error():
    import socket as _socket

    from smartsolar.errors import SmartsolarError

    async def go():
        with _socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen(1)
            with pytest.raises(SmartsolarError, match="cannot listen on tcp://127.0.0.1"):
                await ControlServer(f"tcp://127.0.0.1:{taken.getsockname()[1]}", 0o600, {}).start()

    run(go())
