"""Operations log: a readable, chronological record of what the memory did (`log.md`).

The log of the LLM Wiki pattern: one entry per operation, newest first, each starting with
`## [YYYY-MM-DD] op | title` so `grep "^## \\["` lists them. It is a derived view and keeps no
data of its own: it is rebuilt by `refresh` from `<state>/reviews.jsonl` (reviews and rejected
inbox candidates, calibration.py) and `<state>/telemetry.jsonl` (writes, learning, inbox,
saved answers, captures; telemetry.py). Reads and recalls are not operations of the memory and
never appear. Every line goes through `distill.redact`.

Off by default (`log.path` null). The file is typed `index` and marked `generated`, so recall,
the hook and the bench leave it out, and it is rewritten only when its text changes.
"""
import datetime as dt
import json
from pathlib import Path

import calibration as CB
import catalog as CA
import core as C
import distill as DS
import telemetry as TL

DEFAULTS = {"path": None, "days": 30}
MAX_TITLE = 120


def options(cfg):
    return {**DEFAULTS, **(cfg.get("log") or {})}


def _stem(rel):
    return Path(str(rel)).stem


def _counts(event, keys):
    return " · ".join(f"{k} {event[k]}" for k in keys if event.get(k))


def _from_review(event):
    rel = event["p"]
    decision = event.get("inbox") or ("rejected" if rel.startswith("inbox:") else None)
    target = event.get("target") or ("" if rel.startswith("inbox:") else rel)
    title = _stem(target) if target else f"candidate {rel[6:]}"
    if decision == "rejected":                         # a rejected candidate or update proposal
        return "inbox-reject", title, f"rejected: {event.get('reason') or 'no reason given'}"
    detail = event["verdict"]
    if event.get("confidence"):
        detail += f" · confidence {event['confidence']}"
    if decision == "accepted":                         # the inbox decision is the operation
        return "inbox-accept", title, f"{detail}" + (f" · {target}" if target else "")
    return "review", _stem(rel), f"{detail} · {rel}"


def _from_telemetry(event):
    """(op, title, detail) of an operation, or None for what is not one (reads, recalls, hooks)."""
    kind = event.get("ev")
    if kind == "note":
        return "note", _stem(event.get("p", "")), f"{event.get('mode', 'write')} · {event.get('p', '')}"
    if kind == "flag":
        return "flag", _stem(event.get("p", "")), f"disputed via {event.get('origin', 'cli')} · {event.get('p', '')}"
    if kind == "distill" and (event.get("proposed") or event.get("update_proposals")):
        counts = _counts(event, ("sessions", "proposed", "kept", "updated", "replaced", "discarded",
                                 "update_proposals"))
        return "distill", f"learned from {event.get('day', '')}".strip(), f"{counts} · to {event.get('write', 'inbox')}"
    if kind == "distill_upkeep" or (kind == "distill" and (event.get("renewed") or event.get("archived"))):
        return "upkeep", "learned notes", _counts(event, ("renewed", "archived"))
    if kind == "ingest" and (event.get("proposed") or event.get("update_proposals")):
        counts = _counts(event, ("proposed", "kept", "updated", "replaced", "discarded", "update_proposals"))
        return "ingest", event.get("origin") or "external source", counts
    if kind == "answer":
        return "answer", "saved answer", f"cites {event.get('cites', 0)} note(s)"
    if kind == "capture" and event.get("created"):
        return "capture", "raw material kept", "new capture"
    return None                       # inbox decisions come from reviews.jsonl, which names the note


def entries(cfg, today=None):
    """Operations inside the window, newest first, as (day, op, title, detail). Deterministic
    over the two logs: `today` (not the clock) decides the window."""
    today = today or C.today()
    since = (today - dt.timedelta(days=int(options(cfg)["days"]))).isoformat()
    rows = []
    for event in CB.read(cfg):
        day = str(event.get("day") or event.get("t", ""))[:10]
        rows.append((day, str(event.get("t", "")), _from_review(event)))
    try:
        lines = list(TL._lines(cfg["_vault"]))
    except OSError:
        lines = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        op = _from_telemetry(TL._normalize(event)) if isinstance(event, dict) else None
        if op:
            rows.append((str(event.get("t", ""))[:10], str(event.get("t", "")), op))
    out = []
    for day, stamp, (op, title, detail) in rows:
        if not since <= day <= today.isoformat():
            continue
        clean = [DS.redact(" ".join(str(x).split())) for x in (title, detail)]
        out.append((day, stamp, op, clean[0][:MAX_TITLE] or op, clean[1]))
    out.sort(reverse=True)
    return [(day, op, title, detail) for day, _, op, title, detail in out]


MARKER = ("<!-- vault-memory: generated operations log. "
          "Do not edit by hand: `vault-memory refresh` rewrites it. -->")


def render(cfg, rows):
    lines = ["---", "type: index", f"area: {cfg.get('default_area', 'general')}",
             "tags:\n  - index\n  - log", "generated: true", "---", "",
             MARKER, "", "# Operations log", "",
             "> [!info] Generated file",
             "> Written by `vault-memory refresh` from the review log and the telemetry. "
             "Do not edit by hand: it is regenerated.", ""]
    if not rows:
        lines += ["No operations in the window.", ""]
    for day, op, title, detail in rows:
        lines += [f"## [{day}] {op} | {title}", detail, ""]
    return "\n".join(lines)


def target(cfg):
    """Absolute path of the log inside the vault, or None when it is off. A path outside the
    vault or inside the captures folder is refused."""
    configured = options(cfg)["path"]
    if not configured:
        return None
    vault = Path(cfg["_vault"]).resolve()
    path = (vault / str(configured)).resolve()
    if vault not in path.parents or path.suffix != ".md" or C.under_captures(path):
        raise ValueError(f"log.path {configured!r} must be a .md file inside the vault, outside the captures folder")
    return path


def write(cfg, today=None):
    """(path, entries, written): rewrites the log only when its text changed. None when off."""
    path = target(cfg)
    if path is None:
        return None
    rows = entries(cfg, today)
    text = render(cfg, rows)
    try:
        current = path.read_text(encoding="utf-8")
    except OSError:
        current = None
    if current is not None and not CA.is_generated(current):
        raise ValueError(f"{path.name} exists and was not generated by vault-memory: not overwritten")
    same = current == text
    if not same:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return path, len(rows), not same
