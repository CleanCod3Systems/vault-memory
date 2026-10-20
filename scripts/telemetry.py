"""Local telemetry: what the hook suggests, what gets read afterwards and what is searched
without an answer.

One file per vault (`<state>/telemetry.jsonl`, outside git like the whole state folder), so
each profile measures only its own. Nothing leaves the machine. It answers with data whether
memory helps: how many pointers are used, which notes are read without the hook suggesting
them (missed recall → new eval.tsv case) and what is searched without finding anything
(gap → note that still has to be written).

Events: hook (suggested), hook_silent (did not suggest), read (Read of a vault note),
recall (CLI or MCP), note (memory was written), stop (end-of-session prompt).
"""
import datetime as dt
import json
import os
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

import core as C

FILE = "telemetry.jsonl"
LEGACY_FILE = "telemetria.jsonl"
MAX_BYTES = 5_000_000          # simple rotation to .1; a longer history is not needed
DEFAULTS = {"enabled": True, "store_prompt": True, "log_silent": True}
LEGACY_EVENTS = {"hook_mudo": "hook_silent", "leida": "read", "nota": "note", "cierre": "stop"}


def options(cfg_dict):
    return {**DEFAULTS, **(cfg_dict.get("telemetry") or {})}


def _file(vault):
    state = C.state_dir(vault)
    legacy = state / LEGACY_FILE
    return legacy if legacy.exists() and not (state / FILE).exists() else state / FILE


def record(vault, event, cfg_dict=None, session_id=None):
    """Never fails: telemetry must not break a prompt."""
    try:
        opts = options(cfg_dict or {})
        if not opts["enabled"]:
            return
        if not opts["store_prompt"] and "q" in event:
            event = {**event, "q": ""}
        path = C.state_dir(vault) / FILE
        if path.exists() and path.stat().st_size > MAX_BYTES:
            path.replace(path.with_suffix(".jsonl.1"))
        line = {"t": dt.datetime.now().isoformat(timespec="seconds"),
                "sid": session_id or os.environ.get("CLAUDE_SESSION_ID", "cli"), **event}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _normalize(event):
    """Events written by older versions use Spanish names."""
    event["ev"] = LEGACY_EVENTS.get(event.get("ev"), event.get("ev"))
    if "emitidos" in event and "emitted" not in event:
        event["emitted"] = event.pop("emitidos")
    return event


def _lines(vault):
    state = C.state_dir(vault)
    for name in (LEGACY_FILE, FILE):
        path = state / name
        if path.exists():
            yield from path.read_text(encoding="utf-8").splitlines()


def read(vault, days=14):
    since = (dt.datetime.now() - dt.timedelta(days=days)).isoformat(timespec="seconds")
    out = []
    for line in _lines(vault):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("t", "") >= since:
            out.append(_normalize(event))
    return sorted(out, key=lambda e: e["t"])


def session_events(vault, session_id):
    """Events of one session, in order. Reads the whole file (≤ 5 MB thanks to rotation)."""
    if not session_id:
        return []
    out = []
    try:
        for line in _lines(vault):
            if f'"{session_id}"' not in line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("sid") == session_id:
                out.append(_normalize(event))
    except OSError:
        return []
    return out


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(c.get("text", "") for c in content if isinstance(c, dict))
    return ""


def tokens(transcripts, vault, days=14, bytes_per_token=3.6):
    """How much vault-memory costs the context, measured in the Claude Code transcripts
    (`$CLAUDE_CONFIG_DIR/projects/*/*.jsonl`): hook pointers, `vault-memory` output through
    Bash/Skill and Reads of vault notes. Bytes is the primary metric; tokens is an estimate
    with a fixed constant (same before and after, so the comparison holds)."""
    # Both forms of the path: the Read may come without resolving symlinks (/var vs /private/var).
    bases = (str(Path(vault)), str(Path(vault).resolve()))
    since = time.time() - days * 86400
    totals = {"sessions": 0, "sessions_with_memory": 0, "pointers": 0, "hook_bytes": 0, "hook_ms": [],
              "recalls": 0, "recall_bytes": 0, "reads": 0, "read_bytes": 0,
              "input_tokens": 0, "cache_creation": 0, "cache_read": 0}
    is_cli = re.compile(r"(^|[\s;&|(])(vault-memory|cerebro)(\s|$)|scripts/cli\.py")
    for path in sorted(Path(transcripts).glob("*/*.jsonl")):
        try:
            if path.stat().st_mtime < since:
                continue
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        totals["sessions"] += 1
        pending, touched = {}, False
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            attachment = event.get("attachment")
            if isinstance(attachment, dict) and attachment.get("hookName") == "UserPromptSubmit":
                content = attachment.get("content") or attachment.get("stdout") or ""
                if "[vault]" in content:
                    totals["pointers"] += 1
                    totals["hook_bytes"] += len(content.encode("utf-8"))
                    touched = True
                    try:
                        totals["hook_ms"].append(int(attachment.get("durationMs", 0)))
                    except (TypeError, ValueError):
                        pass
            message = event.get("message") or {}
            usage = message.get("usage") or {}
            totals["input_tokens"] += int(usage.get("input_tokens", 0) or 0)
            totals["cache_creation"] += int(usage.get("cache_creation_input_tokens", 0) or 0)
            totals["cache_read"] += int(usage.get("cache_read_input_tokens", 0) or 0)
            for block in message.get("content") if isinstance(message.get("content"), list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    tool_input = block.get("input") or {}
                    kind = None
                    if block.get("name") == "Read" and str(tool_input.get("file_path", "")).startswith(bases):
                        kind = "read"
                    elif block.get("name") == "Bash" and is_cli.search(str(tool_input.get("command", ""))):
                        kind = "recall"
                    elif block.get("name") == "Skill" and "vault-memory" in str(tool_input.get("skill", "")):
                        kind = "recall"
                    if kind:
                        pending[block.get("id")] = kind
                elif block.get("type") == "tool_result" and block.get("tool_use_id") in pending:
                    kind = pending.pop(block["tool_use_id"])
                    size = len(_text(block.get("content")).encode("utf-8"))
                    totals[{"read": "reads", "recall": "recalls"}[kind]] += 1
                    totals[{"read": "read_bytes", "recall": "recall_bytes"}[kind]] += size
                    touched = True
        totals["sessions_with_memory"] += touched
    durations = sorted(totals.pop("hook_ms"))
    totals["hook_ms_median"] = durations[len(durations) // 2] if durations else 0
    total = totals["hook_bytes"] + totals["recall_bytes"] + totals["read_bytes"]
    totals["total_bytes"] = total
    totals["estimated_tokens"] = int(total / bytes_per_token)
    return totals


def _repeated(by_session):
    """Pointers with the same set as the previous one of the same session (dedupe avoids them)."""
    count = 0
    for events in by_session.values():
        last = None
        for event in events:
            if event.get("ev") in ("hook", "hook_brief"):
                if event.get("emitted") == last:
                    count += 1
                last = event.get("emitted")
    return count


def metrics(events):
    """Deterministic over the list of events (the clock only decides the window in read())."""
    by_session = defaultdict(list)
    for event in events:
        by_session[event.get("sid", "")].append(event)
    hooks = [e for e in events if e.get("ev") in ("hook", "hook_brief")]
    suggested, read_after_suggestion = Counter(), Counter()
    hooks_with_read, missed, seen_missed = 0, [], set()
    for session_id, session in by_session.items():
        emitted_so_far = set()
        last_prompt = ""
        for event in session:
            kind = event.get("ev")
            if kind in ("hook", "hook_brief", "hook_silent") and event.get("q"):
                last_prompt = event["q"]
            if kind in ("hook", "hook_brief"):
                emitted_so_far |= set(event.get("emitted", []))
                for path in event.get("emitted", []):
                    suggested[path] += 1
                later = {x.get("p") for x in session if x.get("ev") == "read" and x["t"] >= event["t"]}
                used = later & set(event.get("emitted", []))
                if used:
                    hooks_with_read += 1
                for path in used:
                    read_after_suggestion[path] += 1
            elif kind == "read" and event.get("p") and event["p"] not in emitted_so_far:
                key = (session_id, event["p"])
                if key not in seen_missed:
                    seen_missed.add(key)
                    missed.append({"p": event["p"], "q": last_prompt})
    empty = Counter(e.get("q", "") for e in events if e.get("ev") == "recall" and not e.get("n"))
    silent = Counter(e.get("reason") or e.get("motivo") for e in events if e.get("ev") == "hook_silent")
    return {
        "sessions": len(by_session),
        "sessions_with_pointer": len({e.get("sid") for e in hooks}),
        "pointers": len(hooks),
        "pointers_read": hooks_with_read,
        "pct_read": round(100 * hooks_with_read / len(hooks), 1) if hooks else 0.0,
        "never_read": [p for p, _ in sorted(suggested.items(), key=lambda x: (-x[1], x[0]))
                       if not read_after_suggestion[p]][:10],
        "missed_recall": missed[:20],
        "gaps": [q for q, _ in sorted(empty.items(), key=lambda x: (-x[1], x[0])) if q][:10],
        "notes_written": sum(1 for e in events if e.get("ev") == "note"),
        "brief_emitted": sum(1 for e in events if e.get("ev") == "hook_brief"),
        "repeated_pointers": _repeated(by_session),
        "silent_reasons": dict(sorted((k, v) for k, v in silent.items() if k)),
    }
