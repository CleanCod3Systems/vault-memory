"""Review against the source: which notes to check first, and how a check is recorded.

Freshness (freshness.py) says how old a note is. This module says what to do about it: a
short, deterministic queue of the notes worth checking now (contradicted ones, notes learned
from sessions and never audited, the ones people use, the ones whose review is overdue) and
one call to record the verdict with its evidence. Checking needs the real sources (code,
tickets, chat, databases), so an agent does it following the `verify` skill; nothing here
talks to a model or to the network.

Frontmatter a review writes (only these keys, never the body; a backup is kept):
    checked:    YYYY-MM-DD       last review, whatever the verdict
    evidence:   text             what was checked and where
    verified:   YYYY-MM-DD       confirmed only
    confidence: 8-10             confirmed only: how much primary evidence backs it
    review:     outdated|unsure  the source says otherwise / it could not be settled
    volatility: live|stable      how fast the fact changes (optional)
"""
import datetime as dt
import re
from collections import defaultdict

import contract as CT
import core as C
import distill as DS
import freshness as FR
import indexer as IX
import note as N
import recall as RC
import telemetry as TL

DEFAULTS = {"n": 20, "gap_days": 7, "usage_days": 30, "min_confirm": 8}
VERDICTS = ("confirmed", "outdated", "unsure")
CONTRADICTS = re.compile(r"Contradicts \[\[([^\]|#]+)")
EVIDENCE_MAX = 300


def options(cfg):
    return {**DEFAULTS, **(cfg.get("review") or {})}


def _usage(cfg, days):
    """How much each note is used: a read weighs more than being suggested by the hook."""
    used = defaultdict(float)
    for event in TL.read(cfg["_vault"], days=days):
        if event.get("ev") == "read" and event.get("p"):
            used[event["p"]] += 2
        elif event.get("ev") in ("hook", "hook_brief"):
            for path in event.get("emitted") or []:
                used[path] += 0.5
    return used


def _disputed(cfg, docs, learned):
    """Notes something says may be wrong: two current notes with the same `key`, or a note a
    learned note names as `Contradicts [[X]]` (distill writes that line instead of editing
    a note the person wrote)."""
    out = {}
    by_key = defaultdict(list)
    for d in docs:
        if d.get("key") and FR.state(d) in FR.CURRENT:
            by_key[d["key"]].append(d["p"])
    for paths in by_key.values():
        if len(paths) > 1:
            for path in paths:
                out[path] = "same key as another current note"
    by_name = {d["n"]: d["p"] for d in docs}
    for d in docs:
        if not d["p"].startswith(learned + "/") or not d["refs"]["wiki"]:
            continue
        try:
            text = (cfg["_vault"] / d["p"]).read_text(encoding="utf-8")
        except OSError:
            continue
        for name in CONTRADICTS.findall(text):
            target = by_name.get(name.strip())
            if target and target != d["p"]:
                out.setdefault(target, f"contradicted by {d['p']}")
                out.setdefault(d["p"], f"contradicts {target}")
    return out


def queue(cfg, n=None, today=None):
    """{"queue": [rows by priority], "coverage": counts}. Same input, same output."""
    opts = options(cfg)
    today = today or C.today()
    settings = cfg["freshness"]
    docs = [d for d in RC.load(cfg)["docs"] if not d["archived"] and not d.get("template")]
    learned = DS.options(cfg)["folder"].strip("/")
    used = _usage(cfg, int(opts["usage_days"]))
    disputed = _disputed(cfg, docs, learned)
    coverage = {"notes": 0, "verified_current": 0, "outdated": 0, "unsure": 0, "due": 0}
    rows = []
    for d in docs:
        is_learned = d["p"].startswith(learned + "/")
        if not (FR.reviewable(d, settings) or is_learned):
            continue
        current_state = FR.state(d, settings)
        if current_state in ("replaced", "expired"):
            continue
        coverage["notes"] += 1
        if current_state == "outdated":
            coverage["outdated"] += 1
            continue
        coverage["unsure"] += d.get("review") == "unsure"
        verified, checked = FR._date(d.get("verified")), FR._date(d.get("checked"))
        limit = FR.review_limit(d, settings)
        age = (today - verified).days if verified else None
        if age is not None and age <= limit:
            coverage["verified_current"] += 1
        if checked and (today - checked).days < int(opts["gap_days"]):
            continue
        score, reasons = 0.0, []
        if d["p"] in disputed:
            score += 15
            reasons.append(disputed[d["p"]])
        if is_learned and not d.get("confidence"):
            score += 10
            reasons.append("learned from a session, never audited")
        elif age is None:
            fallback = FR.days_unverified(d)
            if used.get(d["p"]) or (fallback is not None and fallback > limit):
                score += 2
                reasons.append("never verified")
        elif age > limit:
            score += 2 + 2 * min((age - limit) / 30, 5)
            reasons.append(f"verified {age} d ago, due every {limit} d")
        if not reasons:
            continue
        if d.get("volatility") == "live":
            score += 3
        if used.get(d["p"]):
            score += min(used[d["p"]], 30) / 3          # up to 10: ranks the used ones, never outweighs a contradiction
            reasons.append(f"used {used[d['p']]:g}")
        if d.get("review") == "unsure":
            reasons.append("last check was unsure")
        rows.append({"p": d["p"], "t": d["t"], "type": d["type"], "score": round(score, 1),
                     "reasons": reasons, "verified": d.get("verified", ""), "limit": limit,
                     "volatility": d.get("volatility", ""), "source": d.get("source", ""),
                     "learned": is_learned})
    rows.sort(key=lambda r: (-r["score"], r["p"]))
    coverage["due"] = len(rows)
    return {"queue": rows[:int(n or opts["n"])], "coverage": coverage}


def record(cfg, rel, verdict, evidence, confidence=None, volatility=None, today=None):
    """Write the verdict of one review into the note's frontmatter (backup first)."""
    opts = options(cfg)
    if verdict not in VERDICTS:
        raise N.NoteError(2, f"verdict must be one of {', '.join(VERDICTS)}")
    evidence = " ".join((evidence or "").split())
    if not evidence:
        raise N.NoteError(2, "--evidence is required: what was checked and where")
    if confidence is not None:
        try:
            confidence = int(confidence)
        except (TypeError, ValueError):
            raise N.NoteError(2, "--confidence must be a whole number from 1 to 10")
        if not 1 <= confidence <= 10:
            raise N.NoteError(2, "--confidence must be a whole number from 1 to 10")
    if verdict == "confirmed" and (confidence is None or confidence < int(opts["min_confirm"])):
        raise N.NoteError(2, f"confirmed needs --confidence {opts['min_confirm']}-10: with less "
                             "primary evidence the verdict is unsure")
    if volatility and volatility not in FR.VOLATILITY:
        raise N.NoteError(2, f"volatility must be one of {', '.join(FR.VOLATILITY)}")
    rel = rel.strip().lstrip("/")
    path = cfg["_vault"] / rel
    if not path.is_file():
        raise N.NoteError(2, f"{rel} does not exist")
    text = path.read_text(encoding="utf-8")
    end = text.find("\n---", 3)
    if not text.startswith("---") or end < 0:
        raise N.NoteError(2, f"{rel} has no frontmatter: normalize it with  vault-memory fix --apply")
    fm, _ = C.frontmatter(text)
    day = (today or C.today()).isoformat()
    keys = {"checked": day, "evidence": evidence[:EVIDENCE_MAX]}
    drop = []
    if verdict == "confirmed":
        keys.update(verified=day, confidence=confidence)
        drop.append("review")
        if fm.get("source", "").startswith("distill"):
            drop.append("expires")            # audited: it no longer needs to expire unused
    else:
        keys["review"] = verdict
        drop.append("confidence")
    if volatility:
        keys["volatility"] = volatility
    head = N._set_keys(N.drop_keys(text[4:end], drop), keys)
    CT.backup(cfg, rel)
    N.replace_if_unchanged(path, text, "---\n" + head + "\n---" + text[end + 4:])
    IX.build(cfg, quiet=True, incremental=True)
    TL.record(cfg["_vault"], {"ev": "review", "p": rel, "verdict": verdict,
                              **({"confidence": confidence} if verdict == "confirmed" else {})}, cfg)
    return {"p": rel, "verdict": verdict, **({"confidence": confidence} if confidence else {})}
