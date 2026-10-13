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
from smartsolar import profiles
from smartsolar.errors import AdvertError, ConfigError, DaemonError, DaemonUnreachable, InvalidWriteError, ProfileError, SmartsolarError
from smartsolar.tracker import advert_values
from smartsolar.vreg import build_write, vreg_table, writable_table

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
    if args.writable:
        rows = writable_table()
        if args.json:
            print(json.dumps(rows, indent=2, ensure_ascii=False))
        else:
            print(table([[r["name"], r["vreg"], r["unit"], str(r["allowed"]) if isinstance(r["allowed"], str) else ", ".join(r["allowed"]), r["description"]] for r in rows], ["NAME", "VREG", "UNIT", "ALLOWED", "DESCRIPTION"]))
        return EXIT_OK
    rows = vreg_table()
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
    else:
        print(table([[r["id"], r["name"], r["unit"], "yes" if r["verified"] else "no"] for r in rows], ["VREG", "NAME", "UNIT", "VERIFIED"]))
    return EXIT_OK


def parse_assignments(items: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for item in items:
        name, sep, value = item.partition("=")
        if not sep or not name.strip():
            raise InvalidWriteError(f"expected NAME=VALUE, got {item!r}")
        values[name.strip()] = value.strip()
    return values


def confirm_write(lines: list[str], yes: bool) -> int | None:
    """Ask before writing; return an exit code to stop with, or None to go ahead."""
    if yes:
        return None
    if not sys.stdin.isatty():
        print("error: refusing to write without confirmation, use --yes", file=sys.stderr)
        return EXIT_USAGE
    print("About to write to the device:")
    for line in lines:
        print(f"  {line}")
    if input("Write? [y/N] ").strip().lower() not in ("y", "yes"):
        print("aborted")
        return EXIT_ERROR
    return None


def cmd_write(args: argparse.Namespace, socket: Path) -> int:
    values = parse_assignments(args.assignments)
    items = build_write(values)  # fail early, before asking
    refused = confirm_write([f"{i.name} = {fmt_value(i.value)}" for i in items], args.yes)
    if refused is not None:
        return refused
    result = ipc.request(socket, "write", {"values": values}, timeout=180)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(table([[w["name"], fmt_value(w["old"]), "->", fmt_value(w["value"]), "ok" if w["ok"] else "MISMATCH"] for w in result["written"]]))
    return EXIT_OK if all(w["ok"] for w in result["written"]) else EXIT_ERROR


def format_changes(changes: list[profiles.Change]) -> str:
    return table([[c.name, "none" if c.old is None else fmt_value(c.old), "->", "none" if c.new is None else fmt_value(c.new), c.unit] for c in changes])


def live_settings(socket: Path) -> dict[str, Any]:
    return ipc.request(socket, "settings", {}, timeout=180)


def describe_profile(profile: profiles.Profile) -> str:
    head = [f"{profile.name}" + (f": {profile.description}" if profile.description else "")]
    meta = []
    if profile.product_id:
        meta.append(f"product 0x{profile.product_id:X}")
    if profile.min_firmware:
        meta.append(f"firmware from 0x{profile.min_firmware:X}")
    if profile.system_voltage:
        meta.append(f"{profile.system_voltage} V system")
    if meta:
        head.append(", ".join(meta))
    rows = [[n, "none" if v is None else fmt_value(v), writable_unit(n)] for n, v in profile.values.items()]
    out = "\n".join(head) + "\n" + table(rows, ["SETTING", "VALUE", "UNIT"])
    if profile.ignored:
        out += f"\n({len(profile.ignored)} other /Settings/ keys of the file are not supported and not imported)"
    return out


def writable_unit(name: str) -> str:
    from smartsolar.vreg import WRITABLE

    return WRITABLE[name].unit


def cmd_profile(args: argparse.Namespace) -> int:
    action = args.profile_cmd
    if action == "show":
        profile = profiles.load_profile(args.file)
        if args.json:
            print(json.dumps({"name": profile.name, "product_id": profile.product_id, "min_firmware": profile.min_firmware, "system_voltage": profile.system_voltage,
                              "values": profile.values, "ignored": list(profile.ignored)}, indent=2, ensure_ascii=False))
        else:
            print(describe_profile(profile))
        return EXIT_OK
    socket = resolve_socket(args)
    if action == "save":
        return cmd_profile_save(args, socket)
    return cmd_profile_apply(args, socket)


def cmd_profile_save(args: argparse.Namespace, socket: Path) -> int:
    path = None if args.file == "-" else Path(args.file)
    if path is not None and path.exists() and not args.force:
        print(f"error: {path} exists, use --force to overwrite", file=sys.stderr)
        return EXIT_USAGE
    live = live_settings(socket)
    values = {v["name"]: v["value"] for v in live["values"]}
    blob = profiles.dump_profile(values, product_id=live.get("product_id"), firmware=live.get("firmware"), system_voltage=live.get("system_voltage"), description=args.description or "")
    if path is None:
        sys.stdout.buffer.write(blob)
        return EXIT_OK
    try:
        with open(path, "wb" if args.force else "xb") as f:
            f.write(blob)
    except OSError as exc:
        print(f"error: cannot write {path}: {exc.strerror or exc}", file=sys.stderr)
        return EXIT_ERROR
    print(f"saved {len(values)} settings to {path} (not yet checked with VictronConnect; smartsolar can read it)")
    return EXIT_OK


def cmd_profile_apply(args: argparse.Namespace, socket: Path) -> int:
    """profile diff / profile load: compare with the device, then write what differs and verify."""
    profile = profiles.load_profile(args.file)
    live = live_settings(socket)
    blockers, warnings = profiles.problems(
        profile,
        product_id=live.get("product_id"),
        firmware=live.get("firmware"),
        system_voltage=live.get("system_voltage"),
        ignore_device=args.ignore_device,
    )
    for w in warnings:
        print(f"warning: {w} (ignored)", file=sys.stderr)
    if blockers:
        for b in blockers:
            print(f"error: {b}", file=sys.stderr)
        if any("product" in b or "firmware" in b for b in blockers):
            print("hint: --ignore-device loads it anyway; the device still refuses values outside its own ranges", file=sys.stderr)
        return EXIT_USAGE
    changes, notes = profiles.plan(profile, live["values"])
    loading = args.profile_cmd == "load" and not args.dry_run
    outcome: dict[str, Any] = {"profile": profile.name, "changes": [{"name": c.name, "old": c.old, "new": c.new, "unit": c.unit} for c in changes], "notes": notes}
    if not args.json:
        for note in notes:
            print(f"note: {note}")
    if not changes:
        print(f"{profile.name}: the device already has these values, nothing to change" if not args.json else json.dumps(outcome, indent=2, ensure_ascii=False))
        return EXIT_OK
    if not args.json:
        print(f"{profile.name}: {len(changes)} of {len(profile.values)} value(s) differ" if not loading else f"Loading {profile.name}:")
        print(format_changes(changes))
    if not loading:
        if args.json:
            print(json.dumps(outcome, indent=2, ensure_ascii=False))
        return EXIT_OK
    refused = confirm_write([f"{c.name} = {fmt_value(c.new)} {c.unit}".rstrip() for c in changes], args.yes)
    if refused is not None:
        return refused
    result = ipc.request(socket, "write", {"values": {c.name: c.new for c in changes}}, timeout=300)
    bad = [w for w in result["written"] if not w["ok"]]
    outcome.update(written=True, verified=not bad, mismatched=[w["name"] for w in bad])
    if args.json:
        print(json.dumps(outcome, indent=2, ensure_ascii=False))
    elif not bad:
        print(f"written and verified: {len(changes)} value(s) read back as expected")
    if bad:
        print("error: the device does not report the written value for: " + ", ".join(f"{w['name']} (wanted {fmt_value(w['requested'])}, got {fmt_value(w['value'])})" for w in bad), file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


def run(args: argparse.Namespace) -> int:
    if args.cmd == "profile" and args.profile_cmd == "show":
        return cmd_profile(args)
    if args.cmd == "decode":
        return cmd_decode(args)
    if args.cmd == "vregs":
        return cmd_vregs(args)
    socket = resolve_socket(args)
    if args.cmd == "write":
        return cmd_write(args, socket)
    if args.cmd == "profile":
        return cmd_profile(args)

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
    profile = sub.add_parser("profile", help="apply a VictronConnect settings file (.vcsf) to the charger, or save the settings to one")
    psub = profile.add_subparsers(dest="profile_cmd", required=True, metavar="action")
    show = psub.add_parser("show", parents=[common], help="print what a settings file contains (works without the daemon)")
    show.add_argument("file", metavar="FILE.vcsf")
    save = psub.add_parser("save", parents=[common], help="save the device's current settings to a .vcsf file (experimental, connects if needed)")
    save.add_argument("file", metavar="FILE.vcsf", help="where to write ('-' for stdout)")
    save.add_argument("--description", help="description stored in the file")
    save.add_argument("--force", action="store_true", help="overwrite an existing file")
    for action, text in (("diff", "show what loading a settings file would change"), ("load", "write a settings file to the device: check, diff, confirm, write, verify")):
        p = psub.add_parser(action, parents=[common], help=text)
        p.add_argument("file", metavar="FILE.vcsf")
        p.add_argument("--ignore-device", action="store_true", help="do not refuse a profile made for another product id or newer firmware (provisioning)")
        if action == "load":
            p.add_argument("--dry-run", action="store_true", help="only show the differences")
            p.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")
    sub.add_parser("reconnect", parents=[common], help="drop the GATT connection and connect again")
    sub.add_parser("rescan", parents=[common], help="choose the device again now")
    sub.add_parser("scan", parents=[common], help="Victron devices seen in the advertisements")
    fetch = sub.add_parser("fetch-key", parents=[common], help="read the Instant Readout key over GATT now")
    fetch.add_argument("--show", action="store_true", help="print the key (it is a secret)")
    decode = sub.add_parser("decode", parents=[common], help="decode one advertisement offline (no daemon)")
    decode.add_argument("--key", help="the 32 hex digit key")
    decode.add_argument("--key-file", type=Path, help="file containing the key")
    decode.add_argument("data", help="manufacturer data after the company id, hex, e.g. 100253a001...")
    vregs = sub.add_parser("vregs", parents=[common], help="the registers decoded over GATT (no daemon)")
    vregs.add_argument("--writable", action="store_true", help="list the registers `write` can change, with their allowed values")
    write = sub.add_parser("write", parents=[common], help="write registers over GATT, e.g. write device_mode=off (see vregs --writable)")
    write.add_argument("assignments", nargs="+", metavar="NAME=VALUE")
    write.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")
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
    except (ConfigError, InvalidWriteError, ProfileError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except SmartsolarError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
