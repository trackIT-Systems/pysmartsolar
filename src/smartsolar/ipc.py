"""Newline-delimited JSON over a Unix socket (or loopback TCP): the protocol between CLI and daemon.

The endpoint is a socket path, or ``tcp://127.0.0.1:port`` where Unix sockets are not usable
(Windows). TCP is limited to the loopback interface and has no file permissions to protect it:
every local user can talk to the daemon, so use a Unix socket where there is one.

Request:  {"id": 1, "cmd": "read", "args": {"block": "status"}}
Response: {"id": 1, "ok": true, "result": ...}
          {"id": 1, "ok": false, "error": {"type": "not_connected", "message": "..."}}
"""

from __future__ import annotations

import ipaddress
import json
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from smartsolar.errors import ConfigError, DaemonError, DaemonUnreachable

MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class Endpoint:
    path: Path | None = None  # a Unix socket
    host: str | None = None  # or loopback TCP
    port: int | None = None

    @property
    def is_tcp(self) -> bool:
        return self.host is not None

    def __str__(self) -> str:
        return f"tcp://{self.host}:{self.port}" if self.is_tcp else str(self.path)


def parse_endpoint(value: Path | str) -> Endpoint:
    text = str(value)
    if not text.startswith("tcp://"):
        return Endpoint(path=Path(text))
    host, _, port = text[len("tcp://"):].rpartition(":")
    try:
        address = ipaddress.ip_address(host)
        number = int(port)
    except ValueError:
        raise ConfigError(f"{text!r} is not tcp://127.0.0.1:port") from None
    if address.version != 4 or not address.is_loopback:
        raise ConfigError(f"{text!r}: only IPv4 loopback addresses (127.x.x.x) are allowed, the control socket has no authentication")
    if not 1 <= number <= 65535:
        raise ConfigError(f"{text!r}: port out of range")
    return Endpoint(host=str(address), port=number)


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
        endpoint = parse_endpoint(socket_path)
    except ConfigError as exc:
        raise DaemonUnreachable(str(exc)) from None
    try:
        family = socket.AF_INET if endpoint.is_tcp else getattr(socket, "AF_UNIX", None)
        if family is None:
            raise DaemonUnreachable("this system has no Unix sockets; use a tcp://127.0.0.1:port endpoint")
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect((endpoint.host, endpoint.port) if endpoint.is_tcp else str(endpoint.path))
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
        raise DaemonUnreachable(f"daemon not accepting connections at {socket_path} (is smartsolard running?)") from None
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
