"""smartsolar: talk to the running smartsolard."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from smartsolar import __version__, instant, ipc
from smartsolar.config import DEFAULT_CONFIG_PATH, DEFAULT_SOCKET_PATH, load_config, parse_key
from smartsolar.errors import AdvertError, ConfigError, DaemonError, DaemonUnreachable, SmartsolarError
from smartsolar.tracker import advert_values
from smartsolar.vreg import vreg_table

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_UNREACHABLE = 0, 1, 2, 3


# -- formatting -----------------------------------------------------------------


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    return f"{s // 86400}d{(s % 86400) // 3600:02d}h"


def parse_time(text: str | None) -> datetime | None:
    if not text:
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def fmt_value(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def table(rows: list[list[str]], header: list[str] | None = None) -> str:
    rows = ([header] if header else []) + rows
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip() for row in rows)


def format_status(status: dict[str, Any], now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    lines: list[tuple[str, str]] = []
    device = status.get("device")
    if device:
        parts = [device["address"]]
        if device.get("name"):
            parts.append(device["name"])
        if device.get("rssi") is not None:
            parts.append(f"rssi {device['rssi']} dBm")
        if device.get("model"):
            parts.append(f"model {device['model']}")
        if device.get("pinned"):
            parts.append("pinned")
        lines.append(("device", "  ".join(parts)))
        key = device["key"]
        lines.append(("key", f"{key['source']}, {key['state']}"))
        advert = status["blocks"]["advert"]
        text = "no data yet" if advert["age_s"] is None else f"{'stale' if advert['stale'] else 'ok'} {fmt_duration(advert['age_s'])} ago"
        a = status["advert"]
        text += f"   {device['adverts_per_min']:g}/min, {a['decoded']} decoded"
        for label in ("no_key", "rejected", "undecodable"):
            if a[label]:
                text += f", {a[label]} {label.replace('_', ' ')}"
        lines.append(("advert", text))
    else:
        lines.append(("device", f"none ({status['devices_seen']} Victron device(s) seen)"))
    g = status["gatt"]
    text = g["mode"]
    if g["mode"] != "off" or g["state"] != "idle":
        text += f", {g['state']}"
    if g["connected"] and g["connected_since"]:
        since = parse_time(g["connected_since"])
        text += f" since {fmt_duration((now - since).total_seconds())}" if since else ""
    if g["mode"] == "poll":
        text += f", {g['polls']} poll(s)"
        if g["next_poll_in_s"] is not None:
            text += f", next in {fmt_duration(g['next_poll_in_s'])}"
    if g["connect_attempts"]:
        text += f"   {g['connect_attempts']} attempts, {g['connects']} connects, {g['disconnects']} disconnects"
    if g["last_error"]:
        text += f", last error: {g['last_error']}"
    if g["backoff_s"] is not None and g["state"] == "backoff":
        text += f", retrying in {g['backoff_s']:g}s"
    lines.append(("gatt", text))
    block = status["blocks"]["gatt"]
    if block["enabled"]:
        lines.append(("gatt data", "no data yet" if block["age_s"] is None else f"{'stale' if block['stale'] else 'ok'} {fmt_duration(block['age_s'])} ago"))
    lines.append(("daemon", f"smartsolard {status['version']}, up {fmt_duration(status['uptime_s'])}"))
    width = max(len(k) for k, _ in lines)
    return "\n".join(f"{k.ljust(width)}  {v}" for k, v in lines)


def format_values(values: list[dict[str, Any]]) -> str:
    return table([[v["name"], fmt_value(v["value"]), v.get("unit", "")] for v in values], ["NAME", "VALUE", "UNIT"])


def format_cached(result: dict[str, Any]) -> str:
    out = []
    for name, block in result["blocks"].items():
        if not block["enabled"]:
            out.append(f"{name}: not in use (gatt.mode is off)")
        elif block["time"] is None:
            out.append(f"{name}: no data yet")
        else:
            out.append(f"{name} ({fmt_duration(block['age_s'])} old{', stale' if block['stale'] else ''})")
            out.append(format_values(block["values"]))
        out.append("")
    return "\n".join(out).rstrip()


def format_read(result: dict[str, Any]) -> str:
    out = []
    for name, block in result["blocks"].items():
        if "error" in block:
            out.append(f"{name}: {block['error']}")
        else:
            out.append(f"{name} (live)")
            out.append(format_values(block["values"]))
        out.append("")
    return "\n".join(out).rstrip()


def format_raw(raw: dict[str, str]) -> str:
    return "raw registers (hex)\n" + table([[k, v] for k, v in raw.items()], ["VREG", "VALUE"])


def format_scan(scan: dict[str, Any]) -> str:
    if not scan["devices"]:
        return "no Victron devices seen yet"
    rows = [
        [
            d["address"],
            d.get("name") or "",
            f"{d['rssi']} dBm",
            d["model"] or "-",
            "solar charger" if d["solar_charger"] else f"type {d['record_type']}",
            d["key"],
            f"{d['age_s']:g}s",
            "*" if d["current"] else "",
        ]
        for d in scan["devices"]
    ]
    return table(rows, ["ADDRESS", "NAME", "RSSI", "MODEL", "RECORD", "KEY", "LAST SEEN", "SELECTED"])


# -- commands -------------------------------------------------------------------


def resolve_socket(args: argparse.Namespace) -> Path:
    if args.socket is not None:
        return args.socket
    if args.config is not None:
        return load_config(args.config).daemon.socket  # an explicit config must be usable
    if DEFAULT_CONFIG_PATH.is_file():
        try:
            return load_config().daemon.socket
        except ConfigError:
            pass
    return DEFAULT_SOCKET_PATH


def cmd_decode(args: argparse.Namespace) -> int:
    if args.key_file is not None:
        text = args.key_file.read_text(encoding="utf-8").strip()
    elif args.key is not None:
        text = args.key
    else:
        print("error: give --key or --key-file", file=sys.stderr)
        return EXIT_USAGE
    key = parse_key(text, "the key")
    try:
        data = bytes.fromhex(args.data.replace(":", "").replace(" ", ""))
    except ValueError:
        print("error: DATA must be hex (the manufacturer data after the company id 0x02e1)", file=sys.stderr)
        return EXIT_USAGE
    try:
        decoded = instant.decode(key, data)
    except AdvertError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    values = [v.as_dict() for v in advert_values(decoded.values)]
    if args.json:
        print(json.dumps({"model": f"0x{decoded.header.model:04x}", "nonce": decoded.header.nonce, "values": values}, indent=2, ensure_ascii=False))
    else:
        print(f"model 0x{decoded.header.model:04x}, record type {decoded.header.record_type}, nonce {decoded.header.nonce}")
        print(format_values(values))
    return EXIT_OK


def cmd_vregs(args: argparse.Namespace) -> int:
    rows = vreg_table()
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
    else:
        print(table([[r["id"], r["name"], r["unit"], "yes" if r["verified"] else "no"] for r in rows], ["VREG", "NAME", "UNIT", "VERIFIED"]))
    return EXIT_OK


def run(args: argparse.Namespace) -> int:
    if args.cmd == "decode":
        return cmd_decode(args)
    if args.cmd == "vregs":
        return cmd_vregs(args)
    if args.cmd == "write":
        print("error: writing to the device is not implemented yet (the GATT write protocol is not reverse engineered)", file=sys.stderr)
        return EXIT_ERROR
    socket = resolve_socket(args)

    def show(result: Any, text: str) -> int:
        print(json.dumps(result, indent=2, ensure_ascii=False) if args.json else text)
        return EXIT_OK

    if args.cmd == "status":
        result = ipc.request(socket, "status")
        return show(result, format_status(result))
    if args.cmd == "cached":
        result = ipc.request(socket, "cached", {"block": args.block})
        return show(result, format_cached(result))
    if args.cmd == "read":
        result = ipc.request(socket, "read", {"block": args.block, "raw": args.raw})
        text = format_read(result)
        if "raw" in result:
            text += "\n\n" + format_raw(result["raw"])
        return show(result, text)
    if args.cmd == "reconnect":
        result = ipc.request(socket, "reconnect")
        return show(result, "reconnect requested")
    if args.cmd == "rescan":
        result = ipc.request(socket, "rescan")
        return show(result, format_scan(result))
    if args.cmd == "scan":
        result = ipc.request(socket, "scan")
        return show(result, format_scan(result))
    if args.cmd == "fetch-key":
        result = ipc.request(socket, "fetch_key", {"show": args.show})
        text = f"key of {result['address']} fetched and stored"
        if args.show:
            text += f"\nkey: {result['key']}"
        return show(result, text)
    raise AssertionError(args.cmd)


def build_parser() -> argparse.ArgumentParser:
    def add_global_options(parser: argparse.ArgumentParser, *, suppress: bool) -> None:
        """The global options work before and after the command (SUPPRESS keeps the first value)."""
        keep = argparse.SUPPRESS
        parser.add_argument("--config", type=Path, default=keep if suppress else None, help="config file, used to find the daemon socket")
        parser.add_argument("--socket", type=Path, default=keep if suppress else None, help=f"daemon socket (default: from the config, else {DEFAULT_SOCKET_PATH})")
        parser.add_argument("--json", action="store_true", default=keep if suppress else False, help="print the raw JSON result")

    common = argparse.ArgumentParser(add_help=False)
    add_global_options(common, suppress=True)
    parser = argparse.ArgumentParser(prog="smartsolar", description="Talk to the running smartsolard: cached and live values, key, reconnect, scan.")
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    add_global_options(parser, suppress=False)
    sub = parser.add_subparsers(dest="cmd", required=True, metavar="command")

    sub.add_parser("status", parents=[common], help="selected device, key, advertisement and GATT state")
    cached = sub.add_parser("cached", parents=[common], help="last values the daemon has (no radio traffic)")
    cached.add_argument("block", nargs="?", default="all", choices=["all", "advert", "gatt"])
    read = sub.add_parser("read", parents=[common], help="newest advertisement values, and GATT values (hold: now, poll: connects now)")
    read.add_argument("block", nargs="?", default="all", choices=["all", "advert", "gatt"])
    read.add_argument("--raw", action="store_true", help="also list every register received over GATT as hex, decoded or not")
    sub.add_parser("reconnect", parents=[common], help="drop the GATT connection and connect again")
    sub.add_parser("rescan", parents=[common], help="choose the device again now")
    sub.add_parser("scan", parents=[common], help="Victron devices seen in the advertisements")
    fetch = sub.add_parser("fetch-key", parents=[common], help="read the Instant Readout key over GATT now")
    fetch.add_argument("--show", action="store_true", help="print the key (it is a secret)")
    decode = sub.add_parser("decode", parents=[common], help="decode one advertisement offline (no daemon)")
    decode.add_argument("--key", help="the 32 hex digit key")
    decode.add_argument("--key-file", type=Path, help="file containing the key")
    decode.add_argument("data", help="manufacturer data after the company id, hex, e.g. 100253a001...")
    sub.add_parser("vregs", parents=[common], help="the registers decoded over GATT (no daemon)")
    write = sub.add_parser("write", parents=[common], help="not implemented: writing to the device")
    write.add_argument("assignments", nargs="*")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except DaemonUnreachable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_UNREACHABLE
    except DaemonError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return EXIT_ERROR
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except SmartsolarError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
