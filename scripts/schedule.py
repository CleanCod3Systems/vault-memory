#!/usr/bin/env python3
"""Scheduled refresh: run `vault-memory refresh` at a fixed time, with the system scheduler.

What gets scheduled is **deterministic**: rescan the vault, recompute the index and
regenerate the panel. No network, no model, no tokens, so it can run every day at no cost.

What it does NOT do: bring information from outside. That needs an agent task, which is a
different thing configured elsewhere.

macOS  → launchd, `~/Library/LaunchAgents/com.vault-memory.<vault>.plist`
Linux  → user crontab, one line tagged with `# vault-memory:<vault>`
"""
import os
import platform
import re
import subprocess
import sys
from pathlib import Path

import core as C

DAYS = {"daily": None, "weekdays": [1, 2, 3, 4, 5], "weekend": [6, 0]}
CLI = Path(__file__).resolve().parent / "cli.py"
# The installed plugin lives in a folder per version that changes on every update: the
# schedule calls the shim (stable path), so it always runs the current copy.
SHIM = Path.home() / ".local" / "bin" / "vault-memory"


def _entry():
    return [str(SHIM)] if SHIM.exists() else [sys.executable, str(CLI)]


def _profile():
    return os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")


def _slug(name):
    return re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower() or "vault"


def _time(text):
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", (text or "").strip())
    if not match:
        raise SystemExit(f"Invalid time: {text!r}. Use HH:MM (24 h), for example 08:30.")
    return int(match.group(1)), int(match.group(2))


def plist_path(vault):
    return Path.home() / "Library" / "LaunchAgents" / f"com.vault-memory.{_slug(vault.name)}.plist"


def _marker(vault):
    return f"# vault-memory:{_slug(vault.name)}"


def _log(vault):
    return C.state_dir(vault) / "schedule.log"


def _command(vault):
    args = " ".join(f'"{a}"' for a in _entry())
    return f'CLAUDE_CONFIG_DIR="{_profile()}" {args} --vault "{vault}" refresh'


# ───────────────────────────────────────────────────────────────────── macOS
def _plist(vault, hour, minute, days):
    if days is None:
        calendar = (f"  <key>StartCalendarInterval</key>\n  <dict>\n"
                    f"    <key>Hour</key><integer>{hour}</integer>\n"
                    f"    <key>Minute</key><integer>{minute}</integer>\n  </dict>\n")
    else:
        rows = "".join(
            f"    <dict><key>Weekday</key><integer>{d}</integer>"
            f"<key>Hour</key><integer>{hour}</integer>"
            f"<key>Minute</key><integer>{minute}</integer></dict>\n" for d in days)
        calendar = f"  <key>StartCalendarInterval</key>\n  <array>\n{rows}  </array>\n"
    log = _log(vault)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n<dict>\n'
        f'  <key>Label</key><string>com.vault-memory.{_slug(vault.name)}</string>\n'
        '  <key>ProgramArguments</key>\n  <array>\n'
        + "".join(f'    <string>{a}</string>\n' for a in _entry())
        + '    <string>--vault</string>\n'
        f'    <string>{vault}</string>\n    <string>refresh</string>\n  </array>\n'
        '  <key>EnvironmentVariables</key>\n  <dict>\n'
        f'    <key>CLAUDE_CONFIG_DIR</key><string>{_profile()}</string>\n  </dict>\n'
        f'{calendar}'
        f'  <key>StandardOutPath</key><string>{log}</string>\n'
        f'  <key>StandardErrorPath</key><string>{log}</string>\n'
        '  <key>RunAtLoad</key><false/>\n'
        '</dict>\n</plist>\n')


def _mac_install(vault, hour, minute, days):
    path = plist_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_plist(vault, hour, minute, days), encoding="utf-8")
    subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
    result = subprocess.run(["launchctl", "load", str(path)], capture_output=True, text=True)
    if result.returncode:
        return False, (result.stderr or result.stdout).strip()
    return True, str(path)


def mac_remove(vault):
    path = plist_path(vault)
    if not path.exists():
        return False, "nothing was scheduled"
    subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
    path.unlink()
    return True, str(path)


def _mac_status(vault):
    path = plist_path(vault)
    if not path.exists():
        return None
    result = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
    loaded = f"com.vault-memory.{_slug(vault.name)}" in (result.stdout or "")
    return {"file": str(path), "loaded": loaded}


# ───────────────────────────────────────────────────────────────────── Linux
def _cron_line(vault, hour, minute, days):
    day_of_week = "*" if days is None else ",".join(str(d) for d in days)
    return f"{minute} {hour} * * {day_of_week} {_command(vault)} >> \"{_log(vault)}\" 2>&1  {_marker(vault)}"


class NoScheduler(Exception):
    """There is no `crontab` / `launchctl` on this machine. A real case (containers, some
    minimal distros) that deserves a message, not a traceback."""


def _current_crontab():
    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    except FileNotFoundError:
        raise NoScheduler("the `crontab` command was not found on this machine")
    # "no crontab for <user>" goes to stderr with returncode 1: not an error
    return result.stdout if result.returncode == 0 else ""


def _write_crontab(text):
    try:
        result = subprocess.run(["crontab", "-"], input=text, capture_output=True, text=True)
    except FileNotFoundError:
        raise NoScheduler("the `crontab` command was not found on this machine")
    return result.returncode == 0, (result.stderr or "").strip()


def _linux_install(vault, hour, minute, days):
    others = [line for line in _current_crontab().splitlines() if _marker(vault) not in line]
    new_text = "\n".join(others + [_cron_line(vault, hour, minute, days)]).strip() + "\n"
    ok, error = _write_crontab(new_text)
    return ok, (error or "user crontab")


def linux_remove(vault):
    lines = _current_crontab().splitlines()
    others = [line for line in lines if _marker(vault) not in line]
    if len(others) == len(lines):
        return False, "nothing was scheduled"
    ok, error = _write_crontab("\n".join(others).strip() + "\n")
    return ok, (error or "user crontab")


def _linux_status(vault):
    for line in _current_crontab().splitlines():
        if _marker(vault) in line:
            return {"file": "user crontab", "line": line.strip(), "loaded": True}
    return None


# ──────────────────────────────────────────────────────────────────── public
def system():
    name = platform.system()
    return "mac" if name == "Darwin" else "linux" if name == "Linux" else name.lower()


def remove(vault):
    return {"mac": mac_remove, "linux": linux_remove}.get(system(), lambda v: (False, "not supported"))(vault)


def status(vault):
    try:
        return {"mac": _mac_status, "linux": _linux_status}.get(system(), lambda v: None)(vault)
    except NoScheduler:
        return {"no_scheduler": True}


def run(args, cfg):
    try:
        _run(args, cfg)
    except NoScheduler as e:
        print(f"  {e}.")
        print("  Schedule it with the tool you use and point it at this command:")
        print(f"    {_command(cfg['_vault'])}")


def _run(args, cfg):
    vault = cfg["_vault"]
    current_system = system()

    if args.off:
        if current_system not in ("mac", "linux"):
            raise SystemExit(f"No automatic support on {current_system}. Remove the task by hand.")
        ok, detail = remove(vault)
        print(f"  {'removed' if ok else 'nothing to remove'} · {detail}")
        return

    current = status(vault)
    if current and current.get("no_scheduler"):
        raise NoScheduler("the system scheduler was not found")
    if args.status or (not args.install and not args.at):
        if current:
            print(f"  scheduled · {current['file']}")
            if current.get("line"):
                print(f"    {current['line']}")
            if current_system == "mac":
                print(f"    loaded in launchd: {'yes' if current['loaded'] else 'NO — run vault-memory schedule --install'}")
            log = _log(vault)
            if log.exists():
                tail = log.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-3:]
                print("    last run:")
                for line in tail:
                    print(f"      {line}")
        else:
            print("  no refresh scheduled for this vault")
            print("  to schedule it:  vault-memory schedule --at 08:30 --install")
        return

    hour, minute = _time(args.at)
    if args.days not in DAYS:
        raise SystemExit(f"--days must be one of: {', '.join(DAYS)}")
    days = DAYS[args.days]

    print(f"  vault    {vault}")
    print(f"  when     {args.at} · {args.days}")
    print("  what     vault-memory refresh   (rescans, recomputes and regenerates the panel)")
    print("  cost     0 tokens · no network · ~1 s")
    print(f"  log      {_log(vault)}")
    if current_system == "mac":
        print(f"  where    {plist_path(vault)}")
    elif current_system == "linux":
        print("  where    user crontab")
        print(f"  line     {_cron_line(vault, hour, minute, days)}")
    else:
        print(f"\n  No automatic support on {current_system}. Schedule this by hand:")
        print(f"    {_command(vault)}")
        return

    if not args.install:
        print(f"\n  Dry run. To install it:  vault-memory schedule --at {args.at} --days {args.days} --install")
        return

    install = {"mac": _mac_install, "linux": _linux_install}[current_system]
    ok, detail = install(vault, hour, minute, days)
    if ok:
        print(f"\n  installed · {detail}")
        print("  If the machine is off at that time the run is skipped; the next one catches up")
        print("  anyway, because the index is derived from the vault, not from a history.")
    else:
        print(f"\n  could not install: {detail}")
