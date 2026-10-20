#!/usr/bin/env python3
"""Self-contained HTML panel: a page that searches the vault with no backend and no network.

The full index weighs hundreds of KB and keeps frequencies the browser does not need. Here
it is compressed to the minimum: metadata + a bag of significant terms per note.

The page embeds note titles, paths and short excerpts: it is meant to be opened locally,
not published.
"""
import datetime as dt
import json
from pathlib import Path

import core as C

TERMS = 110
DF_MAX = 0.5          # a term present in half the notes does not discriminate


def build(cfg, docs, link_graph, coverage, result, target=None):
    total = max(len(docs), 1)
    doc_freq = {}
    for d in docs:
        for term in d["tf"]:
            doc_freq[term] = doc_freq.get(term, 0) + 1
    limit = total * DF_MAX

    notes = []
    for d in docs:
        if d["archived"] or d.get("template"):
            continue
        terms = [t for t, _ in sorted(d["tf"].items(), key=lambda x: -x[1])
                 if doc_freq.get(t, 0) <= limit][:TERMS]
        notes.append({
            "p": d["p"], "n": d["n"], "t": d["t"], "tp": d["type"], "a": d["area"],
            "f": d["date"], "m": d["mt"], "e": d["status"], "sv": d["sev"],
            "sys": d["system"], "ids": d["ids"][:8], "k": d["refs"]["tickets"][:4],
            "bl": len(link_graph["backlinks"].get(d["p"], [])), "s": d["s"][:200],
            "w": " ".join(terms),
        })

    related_file = cfg["_state"] / "related.json"
    related = {}
    if related_file.exists():
        try:
            related = {k: [{"p": x["p"], "t": x["t"], "type": x["type"]} for x in v[:3]]
                       for k, v in json.loads(related_file.read_text(encoding="utf-8"))["items"].items()}
        except Exception:
            related = {}

    now = dt.datetime.now()
    data = {
        "vault": cfg["_vault"].name,
        "generated": now.strftime("%Y-%m-%d %H:%M"),
        "generated_iso": now.isoformat(timespec="seconds"),
        "closed": sorted(C.CLOSED),
        "mods": C.modules(cfg),
        "health": result["health"],
        "recurrence": result["recurrence"],
        "debt": result["documentation_debt"],
        "outside": result["outside_taxonomy"],
        "stale": result["stale"][:15],
        "pending_runbooks": result["pending_runbooks"],
        "has_taxonomy": result["has_taxonomy"],
        "orphans": link_graph["orphans"][:40],
        "broken": link_graph["broken"][:40],
        "duplicates": link_graph["duplicates"],
        "rel": related,
        "notes": notes,
    }

    here = Path(__file__).resolve().parent
    css = (here / "panel.css").read_text(encoding="utf-8")
    js = (here / "panel.js").read_text(encoding="utf-8")
    page = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>🧠 {cfg["_vault"].name} — vault-memory</title>'
            f'<style>{css}</style></head><body><div class="wrap" id="app"></div>'
            '<script id="data">const DATA=' + json.dumps(data, ensure_ascii=False)
            + ';</script>'
            f'<script>{js}</script></body></html>')

    out = Path(target).expanduser() if target else (cfg["_state"] / "panel.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    return out, len(notes)
