#!/usr/bin/env python3
"""Synthesis: turns the episodic (N loose record notes) into the semantic.

It answers what no single note can: what repeats, what repeats WITHOUT a documented
solution, what got stuck, which class of event is not in the taxonomy.

The taxonomy is optional: if config.taxonomy points to a note with a table
`| \`id\` | ... | solution | ... |`, it is crossed against it.
"""
import datetime as dt
import json
import re
from collections import defaultdict
from pathlib import Path

import core as C
import freshness as FR


def stale_threshold(cfg):
    return int(cfg["patterns"].get("stale_days", 30))


UNDOCUMENTED = ("undocumented", "not documented", "pending", "tbd", "?",
                "no documentada", "sin documentar", "pendiente")
RE_ROW = re.compile(r"^\|\s*`([^`|]+)`\s*\|(.+?)\|(.+?)\|(.+?)\|(.+?)\|\s*$", re.M)


def days_since(date):
    try:
        return (C.today() - dt.date.fromisoformat(date[:10])).days
    except Exception:
        return None


def taxonomy(cfg):
    rel = (cfg.get("taxonomy") or "").strip()
    if not rel:
        return {}
    path = cfg["_vault"] / rel
    if not path.exists():
        return {}
    out = {}
    for class_id, name, symptom, solution, extra in RE_ROW.findall(path.read_text(encoding="utf-8")):
        out[class_id.strip()] = {
            "name": name.strip(),
            "symptom": re.sub(r"\s+", " ", symptom.strip())[:200],
            "extra": extra.strip(),
            "documented": not any(s in solution.lower() for s in UNDOCUMENTED),
            "runbooks": re.findall(r"\[\[([^\]|]*[Rr]unbook[^\]|]*)", solution),
            "solution": re.sub(r"\s+", " ", solution)[:400],
        }
    return out


def _health(docs, link_graph, coverage, records):
    return {
        "notes": sum(1 for d in docs if not d.get("template")),
        "contract_coverage": coverage.get("coverage"),
        "conforming": coverage.get("conforming", 0),
        "edges": link_graph.get("edges", 0),
        "orphans": len(link_graph.get("orphans", [])),
        "broken_links": len(link_graph.get("broken", [])),
        "duplicates": len(link_graph.get("duplicates", {})),
        "records": len(records),
        "open_records": sum(1 for d in records if d["status"] and C.norm(d["status"]) not in C.CLOSED),
        **{k: v["n"] for k, v in link_graph.get("gaps", {}).items()},
    }


def build(cfg, docs, link_graph, coverage, write=True):
    if not C.enabled(cfg, "patterns"):
        # Health is returned anyway: panel and doctor use it even when synthesis is off.
        empty = {"generated": dt.datetime.now().isoformat(timespec="seconds"),
                 "disabled": True,
                 "health": _health(docs, link_graph, coverage, []),
                 "recurrence": [], "documentation_debt": [], "outside_taxonomy": [],
                 "unused_taxonomy": [], "stale": [], "pending_runbooks": [],
                 "gaps": link_graph.get("gaps", {}), "has_taxonomy": False}
        if write:
            (cfg["_state"] / "patterns.json").write_text(
                json.dumps(empty, ensure_ascii=False, indent=1), encoding="utf-8")
        return empty
    tax = taxonomy(cfg)
    types = C.record_types(cfg)
    records = [d for d in docs if d["type"] in types and not d["archived"] and not d.get("template")]

    by_class = defaultdict(list)
    for d in records:
        by_class[d["class"] or "unclassified"].append(d)

    recurrence = []
    for class_id, members in by_class.items():
        dates = sorted(x["date"] for x in members if x["date"])
        meta = tax.get(class_id, {})
        recurrence.append({
            "class": class_id, "n": len(members), "name": meta.get("name", ""),
            "in_taxonomy": class_id in tax,
            "documented": meta.get("documented", False) if tax else None,
            "runbooks": meta.get("runbooks", []),
            "first": dates[0] if dates else "", "last": dates[-1] if dates else "",
            "open": sum(1 for x in members if C.norm(x["status"]) not in C.CLOSED),
            "systems": sorted({x["system"] for x in members if x["system"]})[:5],
            "notes": [x["p"] for x in members],
        })
    recurrence.sort(key=lambda x: (-x["n"], x["class"]))

    stale = sorted(
        ({"p": d["p"], "t": d["t"], "date": d["date"], "status": d["status"],
          "sev": d["sev"], "type": d["type"], "days": days_since(d["date"])}
         for d in records
         if d["status"] and C.norm(d["status"]) not in C.CLOSED
         and (days_since(d["date"]) or 0) > stale_threshold(cfg)),
        key=lambda x: -(x["days"] or 0))

    out = {
        "generated": dt.datetime.now().isoformat(timespec="seconds"),
        "disabled": False,
        "health": _health(docs, link_graph, coverage, records),
        "recurrence": recurrence,
        "documentation_debt": [r for r in recurrence if r["n"] >= 2 and tax and not r["documented"]],
        "outside_taxonomy": [r for r in recurrence if tax and not r["in_taxonomy"]],
        "unused_taxonomy": sorted(set(tax) - set(by_class)) if tax else [],
        "stale": stale,
        "pending_runbooks": [d["p"] for d in docs if d["type"] == "runbook" and not d.get("template")
                             and any(s in d["status"].lower() for s in UNDOCUMENTED)],
        "gaps": link_graph["gaps"],
        "has_taxonomy": bool(tax),
        "record_types": types,
        "freshness": FR.analyze({"docs": docs}, cfg),
    }
    if write:
        (cfg["_state"] / "patterns.json").write_text(
            json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def summary(result):
    print(f"  patterns.json  {len(result['recurrence'])} classes · "
          f"{len(result['documentation_debt'])} with documentation debt · "
          f"{len(result['stale'])} stale")


def write_note(cfg, result, target):
    """Derived note inside the vault. Regenerated; not edited by hand."""
    health, lines = result["health"], []
    lines += ["---", "type: reference", f"area: {cfg.get('default_area', 'general')}",
              "tags:\n  - reference\n  - patterns", f"date: {C.today().isoformat()}",
              f"generated: {result['generated']}", "---", "",
              "# Recurrence patterns", "",
              "> [!info] Derived note",
              "> Written by `vault-memory patterns --note`. Do not edit by hand: it is regenerated.", "",
              f"`{health['records']}` record notes · `{health['open_records']}` open · "
              f"`{health['notes']}` indexed notes · contract `{health['contract_coverage']}%`", ""]

    if result["documentation_debt"]:
        lines += ["## 🔴 Repeats and has no written solution", "",
                  "| Class | Times | First | Last | Open |", "|---|---|---|---|---|"]
        lines += [f"| `{r['class']}` {r['name']} | {r['n']} | {r['first']} | "
                  f"{r['last']} | {r['open']} |" for r in result["documentation_debt"]]
        lines.append("")
    if result["outside_taxonomy"]:
        lines += ["## 🟡 Classes outside the taxonomy", ""]
        lines += [f"- `{r['class']}` — {r['n']} note(s), last {r['last'] or '—'}"
                  for r in result["outside_taxonomy"]]
        lines.append("")

    lines += ["## 📊 Recurrence by class", "",
              "| Class | Times | Open | Last | Systems |", "|---|---|---|---|---|"]
    lines += [f"| `{r['class']}` | {r['n']} | {r['open']} | {r['last'] or '—'} | "
              f"{' · '.join(r['systems']) or '—'} |" for r in result["recurrence"]]
    lines.append("")

    if result["stale"]:
        lines += [f"## ⏳ Open for more than {stale_threshold(cfg)} days", ""]
        lines += [f"- `{e['days']} d` · {e['sev'] or '—'} · {e['status']} — [[{Path(e['p']).stem}]]"
                  for e in result["stale"]]
        lines.append("")

    labels = {"record_without_ticket": "record notes without a linked ticket",
              "record_without_source": "record notes without a link to the source conversation",
              "runbooks_without_backlink": "runbooks nobody references"}
    lines += ["## 🕳 Linking gaps", ""]
    lines += [f"- **{v['n']}** {labels.get(k, k)}" for k, v in result["gaps"].items()]
    if result["pending_runbooks"]:
        lines.append(f"- **{len(result['pending_runbooks'])}** unwritten runbooks: "
                     + ", ".join(f"[[{Path(x).stem}]]" for x in result["pending_runbooks"]))
    if result["unused_taxonomy"]:
        lines.append(f"- **{len(result['unused_taxonomy'])}** taxonomy classes without any note: "
                     + ", ".join(f"`{x}`" for x in result["unused_taxonomy"]))
    lines.append("")
    path = cfg["_vault"] / target
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return target
