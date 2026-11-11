"""The daily summary: what is next, what is waiting, what is due soon.

Off unless `today.type` is set in the vault config. It selects the notes of that type from
`index.json` (path, type, archived, template), then reads each file's frontmatter, because the
index does not keep priority, due date or waiting-on values. Field names come from the config,
so a vault with Spanish field names works the same.

    collect(cfg) -> dict          the data
    render(cfg, all_items=False, today=None) -> str     at most `max_lines` lines and `max_bytes`
"""
import datetime as dt
import json
from pathlib import Path

import core as C
import legacy as LG
import telemetry as TL

DEFAULTS = {
    "type": "",
    "status_field": "status",
    "groups": {"next": ["next", "doing"], "waiting": ["waiting"]},
    "priority_field": "priority",
    "priority_order": ["critical", "high", "normal"],
    "due_field": "due",
    "due_days": 3,
    "waiting_field": "waiting_on",
    "id_field": "id",
    "title_field": "title",
    "max_lines": 8,
    "max_bytes": 600,
    "per_group": 3,
    "title_width": 48,
    "entrypoints": ["cli"],
}
FOOTER = "full list: vault-memory today --all"


def options(cfg):
    own = cfg.get("today")
    out = {**DEFAULTS, **(own if isinstance(own, dict) else {})}
    if not isinstance(out["groups"], dict):
        out["groups"] = DEFAULTS["groups"]
    return out


def enabled(cfg):
    return bool(options(cfg)["type"])


def _field(fm, name):
    """A frontmatter value by the configured name; legacy translation may have renamed it."""
    if name in fm:
        return fm[name]
    return fm.get(LG.FM_KEYS.get(name, name), "")


def _date(value):
    try:
        return dt.date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def _read(cfg, rel):
    try:
        text = (cfg["_vault"] / rel).read_text(encoding="utf-8")
    except OSError:
        return None
    return C.frontmatter(text)[0]


def _latest_distill(cfg, today):
    try:
        events = [e for e in TL.read(cfg["_vault"], days=2) if e.get("ev") == "distill"]
    except Exception:
        return None
    return events[-1] if events else None


def collect(cfg, today=None):
    """{"enabled", "today", "counts", "groups", "due", "due_count", "distill"}.

    `groups` maps each group name to its ordered items; `due` lists, in date order, the items
    of any group due within `due_days` or overdue. An item is a dict with id, title, status,
    priority, due, waiting, p."""
    opts = options(cfg)
    today = today or C.today()
    out = {"enabled": bool(opts["type"]), "today": today.isoformat(), "counts": {},
           "groups": {name: [] for name in opts["groups"]}, "due": [], "due_count": 0,
           "distill": None}
    if not out["enabled"]:
        return out
    wanted = LG.type_value(opts["type"])
    by_status = {C.norm(s): name for name, statuses in opts["groups"].items() for s in statuses}
    order = {C.norm(x): i for i, x in enumerate(opts["priority_order"])}
    limit = today + dt.timedelta(days=int(opts["due_days"]))
    idx = json.loads((cfg["_state"] / "index.json").read_text(encoding="utf-8"))

    for d in idx["docs"]:
        if d.get("type") != wanted or d.get("archived") or d.get("template"):
            continue
        fm = _read(cfg, d["p"])
        if fm is None:
            continue
        status = _field(fm, opts["status_field"]).strip()
        group = by_status.get(C.norm(status))
        if group is None:
            continue
        stem = d["p"].rsplit("/", 1)[-1][:-3]
        item = {"id": _field(fm, opts["id_field"]).strip() or stem,
                "title": _field(fm, opts["title_field"]).strip() or d.get("t") or stem,
                "status": status, "priority": _field(fm, opts["priority_field"]).strip(),
                "due": _field(fm, opts["due_field"]).strip(),
                "waiting": _field(fm, opts["waiting_field"]).strip(), "p": d["p"]}
        out["groups"][group].append(item)

    def key(item):
        due = _date(item["due"])
        return (order.get(C.norm(item["priority"]), len(order)), due is None,
                due or dt.date.max, item["id"])

    for items in out["groups"].values():
        items.sort(key=key)
    out["counts"] = {name: len(items) for name, items in out["groups"].items()}
    due = [i for items in out["groups"].values() for i in items
           if _date(i["due"]) and _date(i["due"]) <= limit]
    out["due"] = sorted(due, key=lambda i: (_date(i["due"]), key(i)))
    out["due_count"] = len(out["due"])
    out["distill"] = _latest_distill(cfg, today)
    return out


def _clip(text, width):
    text = " ".join(str(text).split())
    return text if len(text) <= width else text[:max(width - 3, 1)].rstrip() + "..."


def _item(item, width, extra=""):
    text = _clip(f"{item['id']} {item['title']}", width)
    if item["priority"]:
        text += f" [{item['priority']}]"
    return text + extra


def _distill_line(event):
    parts = [f"{event.get(k, 0)} {k}" for k in ("kept", "updated", "replaced", "renewed")
             if event.get(k)]
    return f"distill {event.get('day', '')}: " + (", ".join(parts) if parts else "nothing new")


def _memory_line(cfg, today):
    """Mondays: last week's "did memory help?" numbers, from `bench.json` (written by refresh)."""
    if today.weekday() != 0:
        return ""
    try:
        data = json.loads((Path(cfg["_state"]) / "bench.json").read_text(encoding="utf-8"))
        generated = dt.date.fromisoformat(data["generated"])
    except (OSError, ValueError, KeyError, TypeError):
        return ""
    if (today - generated).days > 3:
        return ""
    week, cov = data.get("week") or {}, data.get("verified") or {}
    return (f"memory last week: {week.get('pointers_read', 0)}/{week.get('pointers', 0)} suggestions used · "
            f"recall MRR {data.get('mrr', 0):.2f} on {data.get('cases', 0)} cases · "
            f"{cov.get('verified_current', 0)}/{cov.get('notes', 0)} notes verified")


def render(cfg, all_items=False, today=None):
    """The summary as text. Deterministic for a given `today`."""
    opts = options(cfg)
    if not opts["type"]:
        return ""
    today = today or C.today()
    data = collect(cfg, today)
    counts = data["counts"]
    if not any(counts.values()) and not data["distill"]:
        return "today: nothing open"
    width = int(opts["title_width"])
    per_group = None if all_items else int(opts["per_group"])

    head = [f"{n} {name}" for name, n in counts.items()]
    if data["due_count"]:
        head.append(f"{data['due_count']} due soon")
    lines = ["today: " + " | ".join(head)]
    shown = set()
    for name, items in data["groups"].items():
        if not items:
            continue
        picked = items if per_group is None else items[:per_group]
        shown.update(i["p"] for i in picked)
        text = []
        for i in picked:
            extra = f" ({i['waiting']})" if i["waiting"] else ""
            if i["due"] and _date(i["due"]):
                extra += f" (due {i['due']})"
            text.append(_item(i, width, extra))
        more = f" (+{len(items) - len(picked)})" if len(picked) < len(items) else ""
        lines.append(f"{name}: " + "; ".join(text) + more)
    rest = [i for i in data["due"] if i["p"] not in shown]
    if rest:
        picked = rest if per_group is None else rest[:per_group]
        text = [_item(i, width, f" ({'overdue ' if _date(i['due']) < today else ''}{i['due']})")
                for i in picked]
        more = f" (+{len(rest) - len(picked)})" if len(picked) < len(rest) else ""
        lines.append("due soon: " + "; ".join(text) + more)
    if data["distill"]:
        lines.append(_distill_line(data["distill"]))
    memory = _memory_line(cfg, today)
    if memory:
        lines.append(memory)
    if all_items:
        return "\n".join(lines)

    lines.append(FOOTER)
    max_lines, max_bytes = max(int(opts["max_lines"]), 2), int(opts["max_bytes"])
    while len(lines) > 2 and (len(lines) > max_lines or len("\n".join(lines).encode()) > max_bytes):
        del lines[-2]
    return "\n".join(lines)
