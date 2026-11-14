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

A candidate with `proposal: update` is an update proposal (distill.propose_update): a change
to a note that already exists (`target`), as `append` (text added at the end) or `replace` (an
exact fragment swapped), with the SHA-256 of the note when it was proposed (`target_hash`).
Listing shows its diff. Accepting applies it only if the note is still byte for byte what the
proposal saw (else exit 4, nothing written), keeps a backup, and logs a `confirmed` line for
`inbox:<id>` with `learned: true`: the audited claim is the proposed change, not the whole note,
so the note's own `verified`/`confidence` are left alone. Rejecting writes nothing and logs it
as `outdated`, like any rejected candidate. Nothing else ever applies a proposal. A candidate
without `proposal` is a new note, as before.

A decided candidate moves to `<state>/inbox/done/` with `outcome`, `decided` and the note or
the reason added to its frontmatter: kept for traceability, never listed again. The id of a
candidate is the first 8 hex digits of the SHA-1 of its file name.
"""
import datetime as dt
import difflib
import hashlib
import re
from pathlib import Path

import calibration as CB
import contract as CT
import core as C
import distill as DS
import freshness as FR
import indexer as IX
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


def _block(rest, opening, closing):
    """(content between the two markers, rest without that block); ("", rest) when absent."""
    match = re.search(re.escape(opening) + r"\n(.*?)\n" + re.escape(closing) + r"\n?", rest, re.S)
    if not match:
        return "", rest
    return match.group(1), rest[:match.start()] + rest[match.end():]


def parse(path):
    """One candidate as a dict: id, file, day, fields of its frontmatter, title, body, quote.
    An update proposal also has kind "update", target, operation, target_hash, text and old."""
    text = path.read_text(encoding="utf-8")
    fm, rest = C.raw_frontmatter(text)
    update = {}
    if _value(fm, "proposal") == "update":
        new, rest = _block(rest, DS.MARKERS[0], DS.MARKERS[1])
        old, rest = _block(rest, DS.MARKERS[2], DS.MARKERS[3])
        update = {"kind": "update", "target": _value(fm, "target"), "operation": _value(fm, "operation"),
                  "relation": _value(fm, "relation"), "target_hash": _value(fm, "target_hash"),
                  "text": new, "old": old}
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
            "body": "\n".join(body).strip(), "quote": " ".join(quote).strip(), "_path": path, **update}


def _target(cfg, rel):
    """The vault-relative path of an update's target, checked again at decision time: the
    proposal is a file anyone can edit. Only an existing Markdown note inside the vault, never
    in a hidden folder (the state, .obsidian) nor a capture."""
    rel = (rel or "").strip().replace("\\", "/")
    parts = Path(rel).parts
    vault = Path(cfg["_vault"]).resolve()
    bad = (not rel or Path(rel).is_absolute() or ".." in parts or any(p.startswith(".") for p in parts))
    path = (vault / rel).resolve() if not bad else None
    if bad or vault not in path.parents or path.suffix != ".md" or not path.is_file():
        raise N.NoteError(2, f"{rel or '(empty)'} is not a note of this vault: the proposal's target "
                             "was changed by hand; reject it")
    N.refuse_capture(cfg, rel)
    return rel


def _proposed(cfg, cand):
    """(rel, current text, text after the update or None, stale): stale when the note changed
    since the proposal or the update no longer applies."""
    rel = _target(cfg, cand["target"])
    current = (Path(cfg["_vault"]) / rel).read_text(encoding="utf-8")
    after = DS.apply_update(current, cand["operation"], cand["text"], cand["old"])
    return rel, current, after, after is None or DS.note_hash(current) != cand["target_hash"]


def _diff(rel, before, after):
    if after is None:
        return ""
    return "\n".join(difflib.unified_diff(before.splitlines(), after.splitlines(), f"a/{rel}", f"b/{rel}",
                                          n=2, lineterm=""))


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
    """{"count", "candidates": [{id, day, origin, source, title, type, quote}]} (quote on one line).
    An update proposal adds kind "update", target, operation, diff (unified, against the note as
    it is now) and stale (the note changed since the proposal: accepting it would fail)."""
    rows = []
    for cand in pending(cfg):
        quote = cand["quote"]
        if len(quote) > QUOTE_WIDTH:
            quote = quote[:QUOTE_WIDTH - 1].rstrip() + "…"
        row = {"id": cand["id"], "day": cand["day"], "origin": cand["origin"],
               "source": cand["source"], "title": cand["title"], "type": cand["type"], "quote": quote}
        if cand.get("kind") == "update":
            try:
                rel, current, after, stale = _proposed(cfg, cand)
                diff = _diff(rel, current, after)
            except (N.NoteError, OSError):
                diff, stale = "", True
            row.update(kind="update", target=cand["target"], operation=cand["operation"], diff=diff, stale=stale)
        rows.append(row)
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
    if cand.get("kind") == "update":
        return _accept_update(cfg, cand, evidence, confidence, volatility, today)
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


def _accept_update(cfg, cand, evidence, confidence, volatility, today):
    """Apply an update proposal to its note (backup first), then log the change as confirmed."""
    if cand["operation"] not in DS.OPERATIONS or not cand["text"].strip():
        raise N.NoteError(2, f"{cand['file']} is not an update proposal distill wrote: reject it")
    rel, current, after, _ = _proposed(cfg, cand)
    if DS.note_hash(current) != cand["target_hash"]:
        raise N.NoteError(4, f"{rel} changed since this update was proposed: nothing written. Read the "
                             "note as it is now; if the change still fits, make it by hand, and reject "
                             "the proposal (--reject ID --reason …) when it no longer does", {"p": rel})
    if after is None:
        raise N.NoteError(4, f"the proposed update no longer applies to {rel}: nothing written", {"p": rel})
    backup = CT.backup(cfg, rel)
    N.replace_if_unchanged(Path(cfg["_vault"]) / rel, current, after)
    IX.build(cfg, quiet=True, incremental=True)
    day = (today or C.today()).isoformat()
    # `p` names the proposal: the audited claim is this change, not every line of the note
    CB.append(cfg, {"day": day, "p": f"inbox:{cand['id']}", "verdict": "confirmed", "confidence": confidence,
                    "volatility": volatility or cand["volatility"] or None, "learned": True, "inbox": "accepted",
                    "proposal": "update", "target": rel,
                    "prev_verdict": None, "prev_confidence": None, "prev_checked": None})
    evidence = DS.redact(" ".join(evidence.split()))[:RV.EVIDENCE_MAX]
    _settle(cfg, cand, {"outcome": "accepted", "decided": day, "note": rel, "evidence": evidence})
    TL.record(cfg["_vault"], {"ev": "inbox_accept", "n": 1, "kind": "update", "pending": len(pending(cfg))}, cfg)
    return {"id": cand["id"], "outcome": "accepted", "kind": "update", "p": rel, "verdict": "confirmed",
            "confidence": confidence, "backup": backup.relative_to(cfg["_vault"]).as_posix()}


def reject(cfg, ident, reason, today=None):
    """No note: log the learned claim as `outdated` (an audit that failed) and settle it."""
    cand = find(cfg, ident)
    reason = DS.redact(" ".join((reason or "").split()))[:REASON_MAX]
    if not reason:
        raise N.NoteError(2, "--reason is required: why the learned claim does not hold")
    day = (today or C.today()).isoformat()
    # `p` names the candidate, not a note: one rejected claim = one audited learned note
    update = {"proposal": "update", "target": cand["target"]} if cand.get("kind") == "update" else {}
    CB.append(cfg, {"day": day, "p": f"inbox:{cand['id']}", "verdict": "outdated", "confidence": None,
                    "volatility": cand["volatility"] or None, "learned": True, "inbox": "rejected",
                    "reason": reason, **update, "prev_verdict": None, "prev_confidence": None,
                    "prev_checked": None})
    _settle(cfg, cand, {"outcome": "rejected", "decided": day, "reason": reason})
    TL.record(cfg["_vault"], {"ev": "inbox_reject", "n": 1, **({"kind": "update"} if update else {}),
                              "pending": len(pending(cfg))}, cfg)
    return {"id": cand["id"], "outcome": "rejected", "verdict": "outdated", **({"kind": "update"} if update else {})}
