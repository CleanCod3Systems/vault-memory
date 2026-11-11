"""Calibration: does a review's confidence mean what it says, and may distill write alone?

A model's self-reported confidence runs high (arXiv 2306.13063), so the 1-10 a review records
is checked against what happened later. Every `review.record` appends one line to
`<state>/reviews.jsonl` (telemetry style: no note text):

    {"t", "day", "p", "verdict", "confidence", "volatility", "learned",
     "prev_verdict", "prev_confidence", "prev_checked"}

`prev_*` is the note's previous review: the last line of the log for that path, else what
its frontmatter says (so reviews made before the log existed still count once the note comes
back). Each line is then one observation, read without replaying anything:

- **Hold rate per confidence bucket** (8, 9, 10, ≤7): a check that said the note holds
  (`confirmed`, or `unsure` with a confidence) is *held* when the next review, on a later day,
  confirms it, *failed* when it says `outdated`. `unsure` at the next review settles nothing:
  it is counted apart and left out of the rate. A same-day re-check is a correction, not a
  test of time, and is left out too.
- **Learned-note precision**: of the learned notes whose audit settled (`confirmed` or
  `outdated`; the first settled verdict per note), the share confirmed.
- **Autonomy**: `distill.auto: "earned"` writes notes directly only while the Wilson lower
  bound of that precision is ≥ `distill.earned_min_precision` (0.8) over at least
  `distill.earned_min_audits` (20) audits; otherwise learned notes go to the inbox.

Deterministic over the log; nothing here talks to a model.
"""
import datetime as dt
import json
import math

import core as C

FILE = "reviews.jsonl"
BUCKETS = ("10", "9", "8", "<=7")
SETTLED = ("confirmed", "outdated")
Z95 = 1.959964
MIN_SAMPLES = 10                       # per bucket, before a rate is reported as calibrated
EARNED = {"earned_min_precision": 0.8, "earned_min_audits": 20}


def _path(cfg):
    return C.state_dir(cfg["_vault"]) / FILE


def read(cfg):
    """Every line of the log, in the order written. Broken lines are skipped."""
    path = _path(cfg)
    out = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("p") and event.get("verdict"):
            out.append(event)
    return out


def previous(cfg, rel, fm):
    """(verdict, confidence, checked) of the note's last review: the log first (it keeps the
    confidence of an `unsure`, which the frontmatter drops), else its frontmatter."""
    for event in reversed(read(cfg)):
        if event["p"] == rel:
            return event["verdict"], event.get("confidence"), event.get("day")
    checked = str(fm.get("checked") or fm.get("verified") or "")[:10] or None
    if fm.get("review") in ("outdated", "unsure"):
        return fm["review"], None, checked
    try:
        confidence = int(str(fm.get("confidence", "")).strip())
    except ValueError:
        return None, None, None
    return "confirmed", confidence, checked


def append(cfg, event):
    """One line per review. Never fails the review it describes."""
    try:
        path = _path(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = {"t": dt.datetime.now().isoformat(timespec="seconds"), **event}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError:
        pass


def wilson(successes, n, z=Z95):
    """95% Wilson score interval (low, high) of a proportion; (None, None) with no data."""
    if not n:
        return None, None
    p = successes / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return round(max(0.0, centre - half), 3), round(min(1.0, centre + half), 3)


def _bucket(confidence):
    try:
        confidence = int(confidence)
    except (TypeError, ValueError):
        return None
    return str(confidence) if confidence >= 8 else "<=7"


def _days(a, b):
    try:
        return (dt.date.fromisoformat(str(b)[:10]) - dt.date.fromisoformat(str(a)[:10])).days
    except ValueError:
        return None


def buckets(events, min_samples=MIN_SAMPLES):
    """Hold rate of "it holds" checks, by the confidence they were made with."""
    rows = {b: {"bucket": b, "stated": None if b == "<=7" else int(b) / 10,
                "n": 0, "held": 0, "failed": 0, "unsure_next": 0} for b in BUCKETS}
    same_day = 0
    for event in events:
        prev, bucket = event.get("prev_verdict"), _bucket(event.get("prev_confidence"))
        if prev not in ("confirmed", "unsure") or bucket is None:
            continue
        gap = _days(event.get("prev_checked"), event.get("day"))
        if gap is not None and gap < 1:
            same_day += 1
            continue
        row = rows[bucket]
        if event["verdict"] == "confirmed":
            row["held"] += 1
        elif event["verdict"] == "outdated":
            row["failed"] += 1
        else:
            row["unsure_next"] += 1
    for row in rows.values():
        row["n"] = row["held"] + row["failed"]
        row["rate"] = round(row["held"] / row["n"], 3) if row["n"] else None
        row["low"], row["high"] = wilson(row["held"], row["n"])
        row["enough"] = row["n"] >= min_samples
        row["calibrated"] = row["rate"] if row["enough"] else None
    return [rows[b] for b in BUCKETS], same_day


def precision(events):
    """Learned notes judged at their first settled audit: confirmed / (confirmed + outdated)."""
    first, unsettled = {}, set()
    for event in events:
        if not event.get("learned"):
            continue
        rel = event["p"]
        if rel in first:
            continue
        if rel not in unsettled and event.get("prev_verdict") in SETTLED:
            first[rel] = event["prev_verdict"]          # audited before the log existed
            continue
        if event["verdict"] in SETTLED:
            first[rel] = event["verdict"]
            unsettled.discard(rel)
        else:
            unsettled.add(rel)
    confirmed = sum(1 for v in first.values() if v == "confirmed")
    audited = len(first)
    low, high = wilson(confirmed, audited)
    return {"audited": audited, "confirmed": confirmed, "outdated": audited - confirmed,
            "unsettled": len(unsettled),
            "precision": round(confirmed / audited, 3) if audited else None, "low": low, "high": high}


def earned_options(cfg):
    own = cfg.get("distill") or {}
    return {k: own.get(k, v) for k, v in EARNED.items()}


def autonomy(cfg, learned=None):
    """{"mode": off|on|earned, "auto": bool, "reason": text}: may distill write learned notes
    as notes today? `auto` False means the inbox."""
    mode = (cfg.get("distill") or {}).get("auto", False)
    if mode is not True and mode != "earned":
        return {"mode": "off", "auto": False, "reason": "distill.auto is off"}
    if mode is True:
        return {"mode": "on", "auto": True, "reason": "distill.auto is on"}
    opts = earned_options(cfg)
    learned = learned or precision(read(cfg))
    need, floor = int(opts["earned_min_audits"]), float(opts["earned_min_precision"])
    if learned["audited"] < max(need, 1):
        return {"mode": "earned", "auto": False,
                "reason": f"{learned['audited']} of {need} audits of learned notes: not enough yet"}
    measured = (f"precision {learned['precision']:.0%} on {learned['audited']} audits, "
                f"lower bound {learned['low']:.2f}")
    if learned["low"] < floor:
        return {"mode": "earned", "auto": False, "reason": f"{measured} < {floor:.2f}"}
    return {"mode": "earned", "auto": True, "reason": f"{measured} >= {floor:.2f}"}


def report(cfg, min_samples=None):
    """Everything `vault-memory review --calibration` shows."""
    events = read(cfg)
    min_samples = int(min_samples or (cfg.get("review") or {}).get("calibration_min", MIN_SAMPLES))
    rows, same_day = buckets(events, min_samples)
    learned = precision(events)
    return {"reviews": len(events), "min_samples": min_samples, "buckets": rows,
            "same_day": same_day, "learned": learned, "autonomy": autonomy(cfg, learned),
            "earned": earned_options(cfg)}
