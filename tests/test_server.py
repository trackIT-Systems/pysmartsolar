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
