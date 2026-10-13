"""The daemon side of the control socket."""

from __future__ import annotations

import asyncio
import logging
import os
import stat
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from smartsolar import ipc
from smartsolar.errors import BadRequest, NoDataError, NotConnectedError, ProtocolError, SmartsolarError

log = logging.getLogger(__name__)

IDLE_TIMEOUT_S = 30.0

Handler = Callable[[dict[str, Any]], Awaitable[Any]]


ERROR_TYPES: list[tuple[type[BaseException], str]] = [
    (BadRequest, "bad_request"),
    (NotConnectedError, "not_connected"),
    (NoDataError, "no_data"),
    (TimeoutError, "timeout"),
    (ProtocolError, "protocol_error"),
]


def error_type(exc: BaseException) -> str:
    for cls, name in ERROR_TYPES:
        if isinstance(exc, cls):
            return name
    return "internal"


class ControlServer:
    def __init__(self, path: Path, mode: int, handlers: dict[str, Handler]) -> None:
        self.path = Path(path)
        self.mode = mode
        self.handlers = handlers
        self._server: asyncio.AbstractServer | None = None
        self._clients: set[asyncio.Task] = set()

    async def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if stat.S_ISSOCK(self.path.lstat().st_mode):
                self.path.unlink()  # left over from a crashed daemon
        except FileNotFoundError:
            pass
        try:
            self._server = await asyncio.start_unix_server(self._client, str(self.path), limit=ipc.MAX_REQUEST_BYTES)
            os.chmod(self.path, self.mode)
        except OSError as exc:
            raise SmartsolarError(f"cannot create control socket {self.path}: {exc}") from None
        log.info("control socket %s", self.path)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            for task in list(self._clients):
                task.cancel()
            await asyncio.gather(*self._clients, return_exceptions=True)
            await self._server.wait_closed()
            self._server = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._clients.add(task)
        try:
            while True:
                try:
                    line = await asyncio.wait_for(reader.readline(), IDLE_TIMEOUT_S)
                except (TimeoutError, ConnectionError):
                    break
                except (ValueError, asyncio.LimitOverrunError):
                    await self._send(writer, {"id": None, "ok": False, "error": {"type": "bad_request", "message": "request too long"}})
                    break
                if not line:
                    break
                await self._send(writer, await self._dispatch(line))
        except asyncio.CancelledError:
            pass
        finally:
            self._clients.discard(task)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, message: dict[str, Any]) -> None:
        try:
            writer.write(ipc.encode(message))
            await writer.drain()
        except ConnectionError:
            pass

    async def _dispatch(self, line: bytes) -> dict[str, Any]:
        request_id: Any = None
        try:
            try:
                request = ipc.decode(line)
            except ValueError as exc:
                raise BadRequest(f"invalid JSON: {exc}") from None
            request_id = request.get("id")
            cmd = request.get("cmd")
            args = request.get("args") or {}
            if not isinstance(cmd, str):
                raise BadRequest("missing cmd")
            if not isinstance(args, dict):
                raise BadRequest("args must be an object")
            handler = self.handlers.get(cmd)
            if handler is None:
                return {"id": request_id, "ok": False, "error": {"type": "unknown_command", "message": f"unknown command {cmd!r}"}}
            result = await handler(args)
            return {"id": request_id, "ok": True, "result": result}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            kind = error_type(exc)
            if kind == "internal":
                log.exception("command failed")
            return {"id": request_id, "ok": False, "error": {"type": kind, "message": str(exc) or type(exc).__name__}}
