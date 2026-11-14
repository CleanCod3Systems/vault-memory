"""vault-memory inbox — list, accept or reject the learned candidates waiting for review.

Distill (and ingest) leave a candidate in `<state>/inbox/` when learning does not write notes on
its own (`distill.auto: false`, or `"earned"` before audits back it). Each candidate is one
Markdown file: frontmatter (`type`, `key`, `volatility`, `source: distill <day> <ref>`,
`session`, and `origin`, `url`, `capture` for sources other than Claude Code), then
`# title`, the body and `## Evidence` with the verbatim quote.

Deciding on a candidate is an audit of a learned claim, so it counts like a review in
`<state>/reviews.jsonl` (calibration.py), and toward the earned autonomy of distill:

- accept: the note is written as distill would have with `auto` on (same folder, fields,
  `expires`, provenance; a duplicate stops it with exit 3 and nothing is logged), then the
  same `review.record` that `vault-memory review` uses records it as `confirmed` with the
  evidence and confidence (8-10).
- reject: no note; one `outdated` line with `learned: true` (the learned claim did not hold),
  the redacted reason and no note text.

A decided candidate moves to `<state>/inbox/done/` with `outcome`, `decided` and the note or
the reason added to its frontmatter: kept for traceability, never listed again. The id of a
candidate is the first 8 hex digits of the SHA-1 of its file name.
"""
import datetime as dt
import hashlib

import calibration as CB
import core as C
import distill as DS
import freshness as FR
import note as N
import review as RV
import telemetry as TL

DONE = "done"
REASON_MAX = 300
QUOTE_WIDTH = 100


def folder(cfg):
    return C.state_dir(cfg["_vault"]) / "inbox"


def candidate_id(path):
    return hashlib.sha1(path.name.encode("utf-8")).hexdigest()[:8]


def _value(fm, key):
    return str(fm.get(key) or "").replace('\\"', '"').strip()


def parse(path):
    """One candidate as a dict: id, file, day, fields of its frontmatter, title, body, quote."""
    text = path.read_text(encoding="utf-8")
    fm, rest = C.raw_frontmatter(text)
    title, body, quote = "", [], []
    section = "head"
    for line in rest.splitlines():
        if section == "head" and line.startswith("# "):
            title, section = line[2:].strip(), "body"
        elif section == "body" and line.strip() == "## Evidence":
            section = "evidence"
        elif section == "body":
            body.append(line)
        elif section == "evidence" and line.startswith(">"):
            quote.append(line[1:].strip())
    source = _value(fm, "source")
    parts = source.split()
    day = parts[1] if len(parts) > 1 and parts[0] == "distill" else path.name[:10]
    return {"id": candidate_id(path), "file": path.name, "day": day, "title": title,
            "type": _value(fm, "type"), "key": _value(fm, "key"),
            "volatility": _value(fm, "volatility"), "source": source,
            "origin": _value(fm, "origin"), "url": _value(fm, "url"), "capture": _value(fm, "capture"),
            "body": "\n".join(body).strip(), "quote": " ".join(quote).strip(), "_path": path}


def pending(cfg):
    """Candidates waiting for a decision, oldest file name first."""
    base = folder(cfg)
    if not base.is_dir():
        return []
    out = []
    for path in sorted(base.glob("*.md")):
        try:
            out.append(parse(path))
        except OSError:
            continue
    return out


def listing(cfg):
    """{"count", "candidates": [{id, day, origin, source, title, type, quote}]} (quote on one line)."""
    rows = []
    for cand in pending(cfg):
        quote = cand["quote"]
        if len(quote) > QUOTE_WIDTH:
            quote = quote[:QUOTE_WIDTH - 1].rstrip() + "…"
        rows.append({"id": cand["id"], "day": cand["day"], "origin": cand["origin"],
                     "source": cand["source"], "title": cand["title"], "type": cand["type"], "quote": quote})
    return {"count": len(rows), "candidates": rows}


def find(cfg, ident):
    """The pending candidate with this id (or file name). Unknown → NoteError 2."""
    ident = (ident or "").strip()
    for cand in pending(cfg):
        if ident in (cand["id"], cand["file"], cand["file"][:-3]):
            return cand
    raise N.NoteError(2, f"no pending inbox candidate with id {ident!r}: list them with  vault-memory inbox")


def _settle(cfg, cand, keys):
    """Move the candidate to done/ with the outcome added to its frontmatter."""
    path = cand["_path"]
    text = path.read_text(encoding="utf-8")
    end = text.find("\n---", 3)
    if text.startswith("---") and end >= 0:
        text = "---\n" + N._set_keys(text[4:end], keys) + "\n---" + text[end + 4:]
    N._write_atomic(path.parent / DONE / path.name, text)
    path.unlink()


def _confidence(cfg, confidence):
    floor = int(RV.options(cfg)["min_confirm"])
    if confidence is None:
        return floor
    try:
        confidence = int(confidence)
    except (TypeError, ValueError):
        raise N.NoteError(2, "--confidence must be a whole number from 1 to 10")
    if not floor <= confidence <= 10:
        raise N.NoteError(2, f"accepting confirms the note, so it needs --confidence {floor}-10; with less "
                             "primary evidence use --reject (it did not hold) or leave it in the inbox")
    return confidence


def accept(cfg, ident, evidence, confidence=None, volatility=None, today=None):
    """Write the candidate as a learned note, then record a `confirmed` review of it."""
    cand = find(cfg, ident)
    confidence = _confidence(cfg, confidence)
    if not " ".join((evidence or "").split()):
        raise N.NoteError(2, "--evidence is required: the source checked and what it shows")
    if volatility and volatility not in FR.VOLATILITY:
        raise N.NoteError(2, f"volatility must be one of {', '.join(FR.VOLATILITY)}")
    if not cand["title"] or not cand["body"]:
        raise N.NoteError(2, f"{cand['file']} has no title or body: it is not a candidate distill wrote")
    opts = DS.options(cfg)
    # the same note distill writes with `auto` on (write_auto), minus the model's "relate" step:
    # a duplicate is left for the person to settle (exit 3) instead of appended or replaced
    try:
        learned_on = dt.date.fromisoformat(cand["day"])
    except ValueError:
        learned_on = today or C.today()
    expires = (learned_on + dt.timedelta(days=int(opts["expires_days"]))).isoformat()
    fields = {**({"volatility": cand["volatility"]} if cand["volatility"] in DS.VOLATILITY else {}),
              **{k: cand[k] for k in ("origin", "url", "capture") if cand[k]}}
    written = N.write(cfg, title=cand["title"], note_type=cand["type"], folder=opts["folder"],
                      body=cand["body"], source=cand["source"], key=cand["key"] or None,
                      expires=expires, fields=fields or None)
    reviewed = RV.record(cfg, written["p"], "confirmed", evidence, confidence, volatility, today=today)
    day = (today or C.today()).isoformat()
    _settle(cfg, cand, {"outcome": "accepted", "decided": day, "note": written["p"]})
    TL.record(cfg["_vault"], {"ev": "inbox_accept", "n": 1, "pending": len(pending(cfg))}, cfg)
    return {"id": cand["id"], "outcome": "accepted", "p": written["p"],
            "verdict": reviewed["verdict"], "confidence": confidence}


def reject(cfg, ident, reason, today=None):
    """No note: log the learned claim as `outdated` (an audit that failed) and settle it."""
    cand = find(cfg, ident)
    reason = DS.redact(" ".join((reason or "").split()))[:REASON_MAX]
    if not reason:
        raise N.NoteError(2, "--reason is required: why the learned claim does not hold")
    day = (today or C.today()).isoformat()
    # `p` names the candidate, not a note: one rejected claim = one audited learned note
    CB.append(cfg, {"day": day, "p": f"inbox:{cand['id']}", "verdict": "outdated", "confidence": None,
                    "volatility": cand["volatility"] or None, "learned": True, "inbox": "rejected",
                    "reason": reason, "prev_verdict": None, "prev_confidence": None, "prev_checked": None})
    _settle(cfg, cand, {"outcome": "rejected", "decided": day, "reason": reason})
    TL.record(cfg["_vault"], {"ev": "inbox_reject", "n": 1, "pending": len(pending(cfg))}, cfg)
    return {"id": cand["id"], "outcome": "rejected", "verdict": "outdated"}
