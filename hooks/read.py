#!/usr/bin/env python3
"""PostToolUse(Read|Bash): records which vault note was read. With that `vault-memory usage`
and `vault-memory bench` measure whether recall finds what gets used.

Agents often read a note with a shell command (`cat`, `sed -n`, `head`, `grep`) instead of the
Read tool; counting only Read hid most of the real use. A Bash command is checked cheaply
first (only json, no project imports) so the many commands that touch no note cost nothing.
"""
import json
import re
import shlex
import sys
from pathlib import Path

MAX_PER_COMMAND = 3
WRITES = re.compile(r"(^|[^>&0-9])>>?\s*\S|\btee\b|\bsed\s+-i|\bmv\b|\brm\b|\bcp\b")


def note_paths(command, cwd, base):
    """Vault notes a shell command reads: tokens ending in .md, resolved against the session
    folder and the vault. Commands that write (redirect, tee, sed -i, mv, rm, cp) are skipped."""
    if WRITES.search(command):
        return []
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = command.split()
    out = []
    for token in tokens:
        if not token.endswith(".md"):
            continue
        for candidate in (Path(token).expanduser(), Path(cwd or ".") / token, base / token):
            try:
                path = candidate.resolve()
            except OSError:
                continue
            if base in path.parents and path.is_file():
                rel = path.relative_to(base).as_posix()
                if rel not in out:
                    out.append(rel)
                break
    return out[:MAX_PER_COMMAND]


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    tool = payload.get("tool_name", "Read")
    tool_input = payload.get("tool_input") or {}
    if tool == "Bash" and ".md" not in str(tool_input.get("command", "")):
        return
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import core as C          # noqa: E402
    import telemetry as TL    # noqa: E402
    vault = C.hook_vault()
    if not vault:
        return
    try:
        base = Path(vault).resolve()
        if tool == "Bash":
            paths, via = note_paths(tool_input.get("command", ""), payload.get("cwd"), base), "bash"
        else:
            path = Path(tool_input.get("file_path", "")).resolve()
            paths = [path.relative_to(base).as_posix()] if path.suffix == ".md" and base in path.parents else []
            via = "read"
        if not paths:
            return
        cfg = C.read_config_file(base)
    except BaseException:
        return
    if not cfg:
        return
    for rel in paths:
        TL.record(vault, {"ev": "read", "p": rel, **({"via": via} if via != "read" else {})},
                  cfg, payload.get("session_id"))


if __name__ == "__main__":
    main()
