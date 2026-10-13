"""Newline-delimited JSON over a Unix socket: the protocol between CLI and daemon.

Request:  {"id": 1, "cmd": "read", "args": {"block": "status"}}
Response: {"id": 1, "ok": true, "result": ...}
          {"id": 1, "ok": false, "error": {"type": "not_connected", "message": "..."}}
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

from smartsolar.errors import DaemonError, DaemonUnreachable

MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


def encode(message: dict[str, Any]) -> bytes:
    return json.dumps(message, separators=(",", ":"), default=str).encode("utf-8") + b"\n"


def decode(line: bytes) -> dict[str, Any]:
    message = json.loads(line)
    if not isinstance(message, dict):
        raise ValueError("message must be a JSON object")
    return message


def request(
    socket_path: Path | str,
    cmd: str,
    args: dict[str, Any] | None = None,
    *,
    timeout: float = 60.0,
) -> Any:
    """Send one request and return its result. Blocking, for the CLI.

    Raises DaemonUnreachable if there is no daemon, DaemonError if the daemon reports an error.
    """
    message = {"id": 1, "cmd": cmd, "args": args or {}}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(socket_path))
            sock.sendall(encode(message))
            buffer = bytearray()
            while b"\n" not in buffer:
                chunk = sock.recv(65536)
                if not chunk:
                    raise DaemonUnreachable("daemon closed the connection without answering")
                buffer += chunk
                if len(buffer) > MAX_RESPONSE_BYTES:
                    raise DaemonUnreachable("response too large")
    except FileNotFoundError:
        raise DaemonUnreachable(f"no daemon socket at {socket_path} (is smartsolard running?)") from None
    except ConnectionRefusedError:
        raise DaemonUnreachable(f"daemon not accepting connections at {socket_path}") from None
    except PermissionError:
        raise DaemonUnreachable(f"no permission to use {socket_path}") from None
    except TimeoutError:
        raise DaemonUnreachable(f"daemon did not answer within {timeout:g} s") from None
    except OSError as exc:  # e.g. a socket path that is too long
        raise DaemonUnreachable(f"cannot connect to {socket_path}: {exc}") from None
    try:
        response = decode(bytes(buffer).split(b"\n", 1)[0])
    except ValueError as exc:
        raise DaemonUnreachable(f"invalid response from daemon: {exc}") from None
    if response.get("ok"):
        return response.get("result")
    error = response.get("error") or {}
    raise DaemonError(str(error.get("type", "internal")), str(error.get("message", "unknown error")))
