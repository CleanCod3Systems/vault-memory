#!/usr/bin/env python3
"""PreToolUse: before an external search, say if the vault already documents it.

Only for the tools listed in `gate.tools` of the vault config (tool name or prefix -> the
`tool_input` field used as the query; "*" joins every string value). Without that config it
does nothing. If there is one strong, current note it adds a single line of context:

    [vault] already documented: <path> — <passage, up to 200 characters>

It never blocks and never asks for permission (no `permissionDecision`): exit 0, and no
output when it has nothing to say. Any error is swallowed.

It reads the existing index in-process (no subprocess, no reindex: a stale index only costs
a missed hint) and filters with scripts/hookfilter.py, stricter thresholds than the prompt
hook. An ambiguous result (a second note almost as good) is silence: a wrong pointer costs
more than none. A note already shown in the session is not shown again.
"""
import json
import marshal
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import core as C          # noqa: E402
import freshness as FR    # noqa: E402
import hookfilter as HF   # noqa: E402
import recall as RC       # noqa: E402
import telemetry as TL    # noqa: E402

DEFAULTS = {"tools": {}, "floor": 14, "relative": 0.6, "width": 200, "candidates": 8}


def options(cfg):
    gate = cfg.get("gate")
    return {**DEFAULTS, **(gate if isinstance(gate, dict) else {})}


def query_for(tools, name, tool_input):
    """The text to search for this call, or "" if the tool is not listed or has no usable field."""
    if not isinstance(tools, dict) or not isinstance(tool_input, dict):
        return ""
    keys = [k for k in tools if isinstance(k, str) and name.startswith(k)]
    if not keys:
        return ""
    field = tools[max(keys, key=len)]
    if field == "*":
        parts = [v for v in tool_input.values() if isinstance(v, str)]
    else:
        value = tool_input.get(field)
        parts = [value] if isinstance(value, str) else []
    return " ".join(" ".join(parts).split())


def load_index(cfg):
    """The index as `recall` reads it, without rebuilding it. Parsing index.json is most of the
    time of the gate, so a marshal copy (keyed on the index's mtime and size and on the Python
    version, which marshal depends on) is kept next to it; a missing or stale copy is rebuilt."""
    state = cfg["_state"]
    stat = (state / "index.json").stat()
    stamp = [stat.st_mtime_ns, stat.st_size]
    cache = state / "gate-index-{}{}.bin".format(*sys.version_info[:2])
    try:
        saved, idx = marshal.loads(cache.read_bytes())   # loads(bytes): load(file) is much slower
        if saved == stamp:
            return idx
    except Exception:
        pass
    idx = RC.load(cfg, auto=False)
    RC.stats_of(idx)             # the df over every note is also worth keeping in the copy
    try:
        tmp = cache.with_suffix(".tmp")
        tmp.write_bytes(marshal.dumps([stamp, idx]))
        tmp.replace(cache)
    except Exception:
        pass
    return idx


def shortlist(idx, query, terms, size):
    """Cheap first pass for the latency budget. RC.search scans every term prefix of every note
    (most of its time); here only exact terms and IDs count, and RC.search then ranks just the
    best few with the same stats, so the final scores are the ones `recall` gives. A note that
    matches only by prefix is not shortlisted: the gate loses a hint, never gains a wrong one."""
    doc_freq, total, average = RC.stats_of(idx)
    ids = set(re.findall(r"\b\d{3,10}\b", query))
    tickets = {x.upper() for x in re.findall(r"\b[A-Za-z]{2,6}-\d{1,6}\b", query)}
    scored = []
    for d in idx["docs"]:
        if d["archived"] or d.get("template"):
            continue
        score = RC.bm25(d, terms, doc_freq, total, average, prefix=False)
        if ids & set(d["ids"]) or tickets & {x.upper() for x in d["refs"]["tickets"]}:
            score += 25
        if score > 0:
            scored.append((score, d["p"], d))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return {"docs": [d for _, _, d in scored[:size]], "_stats": (doc_freq, total, average)}


def best(cfg, gate, query):
    """(row, why_not): the only note worth pointing at, or none."""
    idx = load_index(cfg)
    terms = list(C.tokens(query, keep_stopwords=True))
    rows = [{"score": score, "p": d["p"], "n": d["n"], "t": d["t"], "type": d["type"],
             "ids": d["ids"], "refs": d["refs"], "state": FR.state(d), "_doc": d}
            for score, d in RC.search(shortlist(idx, query, terms, 4 * int(gate["candidates"])),
                              query, n=int(gate["candidates"]))]
    # Own thresholds on the prompt filter's rules: "relative" is the cut for what counts as
    # a rival, and a rival means the answer is not clear.
    hits, why = HF.select(rows, query, {"hook": {"floor": gate["floor"], "relative": gate["relative"]},
                                        "recall": cfg.get("recall", {})})
    if not hits:
        return None, why or "none"
    if len(hits) > 1:
        return None, "ambiguous"
    row = hits[0]
    row["passage"] = RC.passage(cfg, row["_doc"], terms, int(gate["width"]) - 1)
    return row, ""


def main():
    vault = C.hook_vault()
    try:
        payload = json.load(sys.stdin)
        if not vault or not isinstance(payload, dict):
            return
        cfg = C.read_config_file(Path(vault))
        gate = options(cfg)
        name = payload.get("tool_name")
        query = query_for(gate["tools"], name, payload.get("tool_input")) if isinstance(name, str) else ""
        if not query:
            return
        session_id = payload.get("session_id")
        cfg = C.load_config(Path(vault))
        row, why = best(cfg, gate, query)
        event = {"tool": name, "q": query[:200]}
        shown = {e.get("p") for e in TL.session_events(vault, session_id) if e.get("ev") == "gate_hit"}
        if row is None or row["p"] in shown:
            TL.record(vault, {"ev": "gate_miss", **event, "reason": why or "repeated"}, cfg, session_id)
            return
        text = " ".join(row["passage"].split())[:int(gate["width"])]
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": f"[vault] already documented: {row['p']} — {text}"}}, ensure_ascii=False))
        TL.record(vault, {"ev": "gate_hit", **event, "p": row["p"], "score": round(row["score"], 1)},
                  cfg, session_id)
    except BaseException:
        return


if __name__ == "__main__":
    main()
