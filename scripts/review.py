"""Review against the source: which notes to check first, and how a check is recorded.

Freshness (freshness.py) says how old a note is. This module says what to do about it: a
short, deterministic queue of the notes worth checking now (contradicted ones, notes learned
from sessions or answers saved from a conversation and never audited, the ones people use, the
ones whose review is overdue) and
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

Each recorded check also adds one line to `<state>/reviews.jsonl`, the log calibration.py
measures confidence and learned-note precision against. A saved answer (answer.py) is logged
with `learned: false, answer: true`: it never counts toward learned-note precision.

A flag (`flag`) is lighter than a review: someone said in a conversation that a note may be
wrong. It never touches the note; it adds a line to `<state>/flags.jsonl` and puts the note at
the top of the queue until a review is recorded after it.
"""
import datetime as dt
import json
import re
from collections import defaultdict

import answer as AN
import calibration as CB
import contract as CT
import core as C
import distill as DS
import freshness as FR
import indexer as IX
import note as N
import recall as RC
import telemetry as TL

DEFAULTS = {"n": 20, "gap_days": 7, "usage_days": 30, "min_confirm": 8,
            # a scheduled run checks `base` notes plus one per high-priority note waiting
            # (contradicted, or learned and never audited), at most `max`
            "daily": {"base": 4, "max": 8}, "calibration_min": CB.MIN_SAMPLES}
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


FLAGS = "flags.jsonl"
REASON_MAX = 300


def flag(cfg, rel, reason, origin="cli"):
    """Mark a note as disputed in a conversation. The note is not edited: the flag waits in
    `<state>/flags.jsonl` until a review settles it at the source."""
    reason = DS.redact(" ".join((reason or "").split()))[:REASON_MAX]
    if not reason:
        raise N.NoteError(2, "a flag needs a reason: what was said against the note")
    rel = rel.strip().lstrip("/")
    N.refuse_capture(cfg, rel)
    path = (cfg["_vault"] / rel).resolve()
    if cfg["_vault"].resolve() not in path.parents or path.suffix != ".md" or not path.is_file():
        raise N.NoteError(2, f"{rel} is not a note of this vault")
    event = {"t": dt.datetime.now().isoformat(timespec="seconds"), "day": C.today().isoformat(),
             "p": rel, "reason": reason, "origin": origin}
    target = C.state_dir(cfg["_vault"]) / FLAGS
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    TL.record(cfg["_vault"], {"ev": "flag", "p": rel, "origin": origin}, cfg)
    return event


def open_flags(cfg):
    """{path: latest flag} for flags no review has settled yet (a review logged at or after
    the flag settles it, whatever the verdict)."""
    flags = {}
    try:
        lines = (C.state_dir(cfg["_vault"]) / FLAGS).read_text(encoding="utf-8").splitlines()
    except OSError:
        return flags
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("p") and event.get("t"):
            flags[event["p"]] = event
    for review_event in CB.read(cfg):
        current = flags.get(review_event["p"])
        if current and str(review_event.get("t", "")) >= current["t"]:
            del flags[review_event["p"]]
    return flags


def budget(cfg, high, due, cap=None):
    """How many notes today's run checks: `daily.base`, plus one per high-priority note
    waiting, up to `daily.max` (or `cap`), never more than are due."""
    daily = {**DEFAULTS["daily"], **(options(cfg).get("daily") or {})}
    base, top = int(daily["base"]), int(cap if cap is not None else daily["max"])
    return {"n": min(due, top, base + high), "base": base, "max": top, "high": high}


def queue(cfg, n=None, today=None, cap=None, use_budget=False):
    """{"queue": [rows by priority], "coverage": counts, "budget": how many to check today}.
    With `use_budget` the queue is cut to that count. Same input, same output."""
    opts = options(cfg)
    today = today or C.today()
    settings = cfg["freshness"]
    docs = [d for d in RC.load(cfg)["docs"] if not d["archived"] and not d.get("template")]
    learned = DS.options(cfg)["folder"].strip("/")
    answers = AN.folder(cfg)
    used = _usage(cfg, int(opts["usage_days"]))
    disputed = _disputed(cfg, docs, learned)
    flags = open_flags(cfg)
    for path, event in flags.items():
        disputed.setdefault(path, f"disputed in a conversation: {event['reason']}")
    coverage = {"notes": 0, "verified_current": 0, "outdated": 0, "unsure": 0, "due": 0}
    rows, high = [], 0
    for d in docs:
        is_answer = d["p"].startswith(answers + "/")
        is_learned = d["p"].startswith(learned + "/") and not is_answer
        if not (FR.reviewable(d, settings) or is_learned or is_answer or d["p"] in flags):
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
        if checked and (today - checked).days < int(opts["gap_days"]) and d["p"] not in flags:
            continue
        score, reasons = 0.0, []
        if d["p"] in disputed:
            score += 15 + 10 * (d["p"] in flags)     # a person disputing it outranks a detected contradiction
            reasons.append(disputed[d["p"]])
        if is_learned and not d.get("confidence"):
            score += 10
            reasons.append("learned from a session, never audited")
        elif is_answer and not d.get("confidence"):
            score += 10
            reasons.append("saved from a conversation, never audited")
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
        high += d["p"] in disputed or ((is_learned or is_answer) and not d.get("confidence"))
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
                     "learned": is_learned, "answer": is_answer})
    rows.sort(key=lambda r: (-r["score"], r["p"]))
    coverage["due"] = len(rows)
    plan = budget(cfg, high, len(rows), cap)
    length = plan["n"] if use_budget else int(n or opts["n"])
    return {"queue": rows[:length], "coverage": coverage, "budget": plan}


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
    N.refuse_capture(cfg, rel)
    path = cfg["_vault"] / rel
    if not path.is_file():
        raise N.NoteError(2, f"{rel} does not exist")
    text = path.read_text(encoding="utf-8")
    end = text.find("\n---", 3)
    if not text.startswith("---") or end < 0:
        raise N.NoteError(2, f"{rel} has no frontmatter: normalize it with  vault-memory fix --apply")
    fm, _ = C.frontmatter(text)
    is_answer = AN.is_answer(cfg, rel)
    day = (today or C.today()).isoformat()
    prev_verdict, prev_confidence, prev_checked = CB.previous(cfg, rel, fm)
    keys = {"checked": day, "evidence": evidence[:EVIDENCE_MAX]}
    drop = []
    if verdict == "confirmed":
        keys.update(verified=day, confidence=confidence)
        drop.append("review")
        if fm.get("source", "").startswith("distill") or is_answer:
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
    folder = DS.options(cfg)["folder"].strip("/")
    CB.append(cfg, {"day": day, "p": rel, "verdict": verdict, "confidence": confidence,
                    "volatility": volatility or fm.get("volatility") or None,
                    # a saved answer was not written by distill: it never counts toward
                    # learned-note precision, so earned autonomy is not inflated by it
                    "learned": not is_answer and (rel.startswith(folder + "/")
                                                  or fm.get("source", "").startswith("distill")),
                    **({"answer": True} if is_answer else {}),
                    "prev_verdict": prev_verdict, "prev_confidence": prev_confidence,
                    "prev_checked": prev_checked})
    TL.record(cfg["_vault"], {"ev": "review", "p": rel, "verdict": verdict,
                              **({"confidence": confidence} if verdict == "confirmed" else {})}, cfg)
    return {"p": rel, "verdict": verdict, **({"confidence": confidence} if confidence else {})}
