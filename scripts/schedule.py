#!/usr/bin/env python3
"""Scheduled refresh: run `vault-memory refresh` at a fixed time, with the system scheduler.

What gets scheduled is **deterministic**: rescan the vault, recompute the index and
regenerate the panel. No network, no model, no tokens, so it can run every day at no cost.

What it does NOT do: bring information from outside. That needs an agent task, which is a
different thing configured elsewhere.

macOS   → launchd, `~/Library/LaunchAgents/com.vault-memory.<vault>.plist`
Linux   → systemd user timer (`~/.config/systemd/user/vault-memory-<vault>.{service,timer}`,
          `Persistent=true`) when `systemctl --user` works; otherwise a user crontab line
          tagged with `# vault-memory:<vault>`
Windows → Task Scheduler (`schtasks`), task `vault-memory <vault>`

Every command and file is built by a pure function (`_plist`, `_cron_line`, `systemd_*`,
`schtasks_*_args`); the only place that touches the machine is `_sh`, which tests mock.
"""
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

import core as C

DAYS = {"daily": None, "weekdays": [1, 2, 3, 4, 5], "weekend": [6, 0]}
CLI = Path(__file__).resolve().parent / "cli.py"
# Week days for schtasks /D, indexed like DAYS (0 = Sunday).
WIN_DAYS = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"]
# systemd.time(7): English weekday names, ranges with `..`.
SD_DAYS = {"weekdays": "Mon..Fri", "weekend": "Sat,Sun"}
SCHTASKS_MAX_TR = 261   # schtasks /TR: "The path name must not exceed 262 characters"


def shim_path(system_name, home=None, env=None):
    """Where `install-cli` writes the shim on each OS (pure)."""
    home = Path(home) if home else Path.home()
    env = os.environ if env is None else env
    if system_name == "windows":
        base = Path(env.get("LOCALAPPDATA") or (home / "AppData" / "Local"))
        return base / "vault-memory" / "bin" / "vault-memory.cmd"
    return home / ".local" / "bin" / "vault-memory"


def _entry():
    # The installed plugin lives in a folder per version that changes on every update: the
    # schedule calls the shim (stable path), so it always runs the current copy.
    shim = shim_path(system())
    return [str(shim)] if shim.exists() else [sys.executable, str(CLI)]


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


def _command(vault, entry=None, system_name=None):
    """The command line to schedule by hand when there is no scheduler to drive."""
    args = " ".join(f'"{a}"' for a in (entry or _entry()))
    if (system_name or system()) == "windows":
        return f'{args} --vault "{vault}" refresh'
    return f'CLAUDE_CONFIG_DIR="{_profile()}" {args} --vault "{vault}" refresh'


class NoScheduler(Exception):
    """There is no `crontab` / `launchctl` / `schtasks` on this machine. A real case
    (containers, some minimal distros) that deserves a message, not a traceback."""


def _sh(argv, input=None):
    """The one place that runs system commands. Returns (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(argv, input=input, capture_output=True, text=True)
    except FileNotFoundError:
        raise NoScheduler(f"the `{argv[0]}` command was not found on this machine")
    return result.returncode, result.stdout or "", result.stderr or ""


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
    _sh(["launchctl", "unload", str(path)])
    code, out, err = _sh(["launchctl", "load", str(path)])
    if code:
        return False, (err or out).strip()
    return True, str(path)


def mac_remove(vault):
    path = plist_path(vault)
    if not path.exists():
        return False, "nothing was scheduled"
    _sh(["launchctl", "unload", str(path)])
    path.unlink()
    return True, str(path)


def _mac_status(vault):
    path = plist_path(vault)
    if not path.exists():
        return None
    _code, out, _err = _sh(["launchctl", "list"])
    loaded = f"com.vault-memory.{_slug(vault.name)}" in out
    return {"file": str(path), "loaded": loaded}


# ───────────────────────────────────────────────────────── Linux · crontab
def _cron_line(vault, hour, minute, days):
    day_of_week = "*" if days is None else ",".join(str(d) for d in days)
    return f"{minute} {hour} * * {day_of_week} {_command(vault)} >> \"{_log(vault)}\" 2>&1  {_marker(vault)}"


def _current_crontab():
    code, out, _err = _sh(["crontab", "-l"])
    # "no crontab for <user>" goes to stderr with returncode 1: not an error
    return out if code == 0 else ""


def _write_crontab(text):
    code, _out, err = _sh(["crontab", "-"], input=text)
    return code == 0, err.strip()


def _cron_install(vault, hour, minute, days):
    others = [line for line in _current_crontab().splitlines() if _marker(vault) not in line]
    new_text = "\n".join(others + [_cron_line(vault, hour, minute, days)]).strip() + "\n"
    ok, error = _write_crontab(new_text)
    return ok, (error or "user crontab")


def _cron_remove(vault):
    lines = _current_crontab().splitlines()
    others = [line for line in lines if _marker(vault) not in line]
    if len(others) == len(lines):
        return False, "nothing was scheduled"
    ok, error = _write_crontab("\n".join(others).strip() + "\n")
    return ok, (error or "user crontab")


def _cron_status(vault):
    for line in _current_crontab().splitlines():
        if _marker(vault) in line:
            return {"file": "user crontab", "line": line.strip(), "loaded": True}
    return None


# ───────────────────────────────────────────────── Linux · systemd user timer
def _sd_quote(arg):
    """One word of an ExecStart= line (systemd.service(5)): double-quoted, with backslash and
    double quote escaped, and the specifier `%` and variable `$` markers doubled."""
    arg = str(arg).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{arg}"'


def systemd_unit_name(vault):
    return f"vault-memory-{_slug(vault.name)}"


def systemd_dir(home=None):
    return Path(home or Path.home()) / ".config" / "systemd" / "user"


def on_calendar(hour, minute, days):
    """systemd.time(7) calendar event. `days` follows DAYS (None = every day)."""
    prefix = ""
    if days is not None:
        prefix = SD_DAYS["weekdays" if days == DAYS["weekdays"] else "weekend"] + " "
    return f"{prefix}*-*-* {hour:02d}:{minute:02d}:00"


def systemd_service(vault, entry, profile, log):
    exec_start = " ".join(_sd_quote(a) for a in [*entry, "--vault", vault, "refresh"])
    return ("[Unit]\n"
            f"Description=vault-memory refresh ({vault.name})\n\n"
            "[Service]\nType=oneshot\n"
            f"Environment={_sd_quote('CLAUDE_CONFIG_DIR=' + str(profile))}\n"
            f"ExecStart={exec_start}\n"
            f"StandardOutput=append:{log}\nStandardError=append:{log}\n")


def systemd_timer(vault, hour, minute, days):
    return ("[Unit]\n"
            f"Description=vault-memory refresh timer ({vault.name})\n\n"
            "[Timer]\n"
            f"OnCalendar={on_calendar(hour, minute, days)}\n"
            "Persistent=true\n\n"
            "[Install]\nWantedBy=timers.target\n")


def systemctl_args(*args):
    return ["systemctl", "--user", *args]


def systemd_available():
    """`systemctl --user` answers (it does not in most containers, or WSL without systemd)."""
    if not shutil.which("systemctl"):
        return False
    try:
        code, _out, _err = _sh(systemctl_args("show-environment"))
    except NoScheduler:
        return False
    return code == 0


def _unit_files(vault):
    folder, name = systemd_dir(), systemd_unit_name(vault)
    return folder / f"{name}.service", folder / f"{name}.timer"


def _systemd_install(vault, hour, minute, days):
    service, timer = _unit_files(vault)
    service.parent.mkdir(parents=True, exist_ok=True)
    service.write_text(systemd_service(vault, _entry(), _profile(), _log(vault)), encoding="utf-8")
    timer.write_text(systemd_timer(vault, hour, minute, days), encoding="utf-8")
    _sh(systemctl_args("daemon-reload"))
    code, out, err = _sh(systemctl_args("enable", "--now", timer.name))
    if code:
        return False, (err or out).strip()
    try:  # a crontab line left from before would run the refresh twice
        _cron_remove(vault)
    except NoScheduler:
        pass
    return True, str(timer)


def _systemd_remove(vault):
    service, timer = _unit_files(vault)
    if not (service.exists() or timer.exists()):
        return False, "nothing was scheduled"
    try:
        _sh(systemctl_args("disable", "--now", timer.name))
    except NoScheduler:
        pass
    for path in (timer, service):
        if path.exists():
            path.unlink()
    try:
        _sh(systemctl_args("daemon-reload"))
    except NoScheduler:
        pass
    return True, str(timer)


def _systemd_status(vault):
    _service, timer = _unit_files(vault)
    if not timer.exists():
        return None
    code, _out, _err = _sh(systemctl_args("is-active", timer.name))
    return {"file": str(timer), "loaded": code == 0, "systemd": True}


def _linux_install(vault, hour, minute, days):
    if systemd_available():
        return _systemd_install(vault, hour, minute, days)
    return _cron_install(vault, hour, minute, days)


def linux_remove(vault):
    removed = []
    ok, detail = _systemd_remove(vault)
    if ok:
        removed.append(detail)
    try:
        ok, detail = _cron_remove(vault)
    except NoScheduler:
        ok, detail = False, ""
    if ok:
        removed.append(detail)
    return (True, " + ".join(removed)) if removed else (False, "nothing was scheduled")


def _linux_status(vault):
    return _systemd_status(vault) or _cron_status(vault)


# ──────────────────────────────────────────────────────── Windows · schtasks
def schtasks_name(vault):
    return f"vault-memory {_slug(vault.name)}"


def schtasks_run(vault, entry=None):
    """The /TR value: one string, every path quoted so spaces survive."""
    return " ".join(f'"{a}"' for a in (entry or _entry())) + f' --vault "{vault}" refresh'


def schtasks_create_args(vault, hour, minute, days, entry=None):
    """argv for `schtasks /Create`. Run it as a list: subprocess turns it into a command line
    that escapes the quotes inside /TR as \\", which is what schtasks expects."""
    schedule = ["/SC", "DAILY"] if days is None else ["/SC", "WEEKLY", "/D", ",".join(WIN_DAYS[d] for d in days)]
    return ["schtasks", "/Create", *schedule, "/ST", f"{hour:02d}:{minute:02d}",
            "/TN", schtasks_name(vault), "/TR", schtasks_run(vault, entry), "/F"]


def schtasks_delete_args(vault):
    return ["schtasks", "/Delete", "/TN", schtasks_name(vault), "/F"]


def schtasks_query_args(vault):
    return ["schtasks", "/Query", "/TN", schtasks_name(vault), "/FO", "LIST"]


def _win_install(vault, hour, minute, days):
    args = schtasks_create_args(vault, hour, minute, days)
    if len(args[args.index("/TR") + 1]) > SCHTASKS_MAX_TR:
        return False, (f"the command is longer than the {SCHTASKS_MAX_TR} characters schtasks accepts: "
                       "run `vault-memory install-cli` (a shorter path) and try again")
    code, out, err = _sh(args)
    if code:
        return False, (err or out).strip()
    return True, f"Task Scheduler · {schtasks_name(vault)}"


def _win_remove(vault):
    code, _out, _err = _sh(schtasks_query_args(vault))
    if code:
        return False, "nothing was scheduled"
    code, out, err = _sh(schtasks_delete_args(vault))
    return code == 0, ((err or out).strip() if code else f"Task Scheduler · {schtasks_name(vault)}")


def _win_status(vault):
    code, out, _err = _sh(schtasks_query_args(vault))
    if code:
        return None
    wanted = ("Next Run Time", "Status", "Schedule Type", "Days")
    rows = [line.strip() for line in out.splitlines() if line.split(":")[0].strip() in wanted]
    return {"file": f"Task Scheduler · {schtasks_name(vault)}", "line": " · ".join(rows), "loaded": True}


# ──────────────────────────────────────────────────────────────────── public
def system():
    name = platform.system()
    return "mac" if name == "Darwin" else name.lower()


REMOVERS = {"mac": mac_remove, "linux": linux_remove, "windows": _win_remove}
STATUSES = {"mac": _mac_status, "linux": _linux_status, "windows": _win_status}
INSTALLERS = {"mac": _mac_install, "linux": _linux_install, "windows": _win_install}


def remove(vault):
    return REMOVERS.get(system(), lambda v: (False, "not supported"))(vault)


def status(vault):
    try:
        return STATUSES.get(system(), lambda v: None)(vault)
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
        if current_system not in INSTALLERS:
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
            if current.get("systemd"):
                print(f"    timer active in systemd: {'yes' if current['loaded'] else 'NO — run vault-memory schedule --install'}")
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
    use_systemd = False
    if current_system == "windows":
        print("  where    Task Scheduler (no log file: schtasks cannot redirect output)")
        print(f"  task     {schtasks_name(vault)}")
        print(f"  run      {schtasks_run(vault)}")
    else:
        print(f"  log      {_log(vault)}")
    if current_system == "mac":
        print(f"  where    {plist_path(vault)}")
    elif current_system == "linux":
        use_systemd = systemd_available()
        if use_systemd:
            print(f"  where    systemd user timer {systemd_unit_name(vault)}.timer")
            print(f"  when     OnCalendar={on_calendar(hour, minute, days)} · Persistent=true")
        else:
            print("  where    user crontab (systemctl --user is not available)")
            print(f"  line     {_cron_line(vault, hour, minute, days)}")
    elif current_system != "windows":
        print(f"\n  No automatic support on {current_system}. Schedule this by hand:")
        print(f"    {_command(vault)}")
        return

    if not args.install:
        print(f"\n  Dry run. To install it:  vault-memory schedule --at {args.at} --days {args.days} --install")
        return

    ok, detail = INSTALLERS[current_system](vault, hour, minute, days)
    if ok:
        print(f"\n  installed · {detail}")
        if use_systemd:
            print("  If the machine is off at that time the run happens when it is back on")
            print("  (Persistent=true).")
        else:
            print("  If the machine is off at that time the run is skipped; the next one catches up")
            print("  anyway, because the index is derived from the vault, not from a history.")
    else:
        print(f"\n  could not install: {detail}")
