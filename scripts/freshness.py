"""Memory freshness: which note is still true, which went stale and which contradict each
other. All deterministic, over optional frontmatter fields (docs/freshness.md):

    verified: YYYY-MM-DD   last time someone confirmed it is still true
    source:   text         where it comes from (ticket, meeting, doc, person)
    replaces: [[Note]]     this note makes that one obsolete (supersede)
    expires:  YYYY-MM-DD   from then on it no longer holds (temporary agreements)
    key:      slug         the fact it talks about: one key, one current note

What depends on the date (expired, unverified) is computed at query time, not at index time:
yesterday's index must not say something "still holds" today.
"""
import datetime as dt
from collections import defaultdict

import core as C
import indexer as IX
import recall as RC

CURRENT = ("ok", "unverified")


def annotate(docs, name_to_paths):
    """At index time: `replaced_by` = path of the note that replaces this one (empty if none)."""
    for d in docs:
        d["replaced_by"] = ""
    by_path = {d["p"]: d for d in docs}
    for d in sorted(docs, key=lambda x: x["p"]):
        for name in d.get("replaces", []):
            targets = IX.resolve_link(name, name_to_paths, d["p"])
            target = targets[0] if targets else None
            if target and target != d["p"] and target in by_path and not by_path[target]["replaced_by"]:
                by_path[target]["replaced_by"] = d["p"]


def _date(text):
    try:
        return dt.date.fromisoformat((text or "")[:10])
    except ValueError:
        return None


def days_unverified(d):
    reference = _date(d.get("verified")) or _date(d.get("date")) or _date(d.get("mt"))
    return (C.today() - reference).days if reference else None


def state(d, settings=None):
    """ok | replaced | expired | unverified."""
    settings = settings or C.FRESHNESS
    if d.get("replaced_by"):
        return "replaced"
    expires = _date(d.get("expires"))
    if (expires and expires < C.today()) or C.norm(d.get("status", "")) in C.OBSOLETE:
        return "expired"
    if d.get("type") in settings.get("types", []):
        days = days_unverified(d)
        limit = (settings.get("review_days_by_type") or {}).get(d.get("type"), settings.get("review_days", 180))
        if days is not None and days > int(limit):
            return "unverified"
    return "ok"


def factor(d, settings=None):
    settings = settings or C.FRESHNESS
    return float(settings.get("penalty", {}).get(state(d, settings), 1.0))


def _title_terms(d):
    return set(C.tokens(d["t"]))


def analyze(idx, cfg):
    """Report for doctor: sorted lists, same entries on every run."""
    settings = cfg["freshness"]
    docs = [d for d in idx["docs"] if not d["archived"] and not d.get("template")]
    linked_pairs = {(d["p"], d["replaced_by"]) for d in docs if d.get("replaced_by")}

    def linked(a, b):
        return (a, b) in linked_pairs or (b, a) in linked_pairs

    current = [d for d in docs if state(d, settings) in CURRENT]
    out = {
        "expired": sorted(d["p"] for d in docs if state(d, settings) == "expired"),
        "replaced": sorted(({"p": d["p"], "by": d["replaced_by"]} for d in docs if d.get("replaced_by")),
                           key=lambda x: x["p"]),
        "unverified": sorted(({"p": d["p"], "days": days_unverified(d)} for d in docs
                              if state(d, settings) == "unverified"), key=lambda x: (-x["days"], x["p"])),
        "contradictions": [], "possible_contradictions": [], "near_duplicates": [],
    }

    # Contradiction: two current notes that say they talk about the same fact (same `key`)
    # without one replacing the other.
    by_key = defaultdict(list)
    for d in current:
        if d.get("key"):
            by_key[d["key"]].append(d["p"])
    out["contradictions"] = [{"key": k, "notes": sorted(v)} for k, v in sorted(by_key.items()) if len(v) > 1]

    # Candidates (heuristic, not certainty): two notes of the same type that look like they
    # talk about the same thing. "Share a ticket" alone gives hundreds of pairs (summary notes
    # list dozens of tickets), so each signal also requires both notes to be specific. Built
    # with an inverted index to avoid O(n²).
    reviewable = [d for d in current if d["type"] in settings["types"]]
    inverted = defaultdict(set)
    titles = {d["p"]: _title_terms(d) for d in reviewable}
    for d in reviewable:
        for term in titles[d["p"]]:
            inverted[term].add(d["p"])
        for note_id in d["ids"]:
            inverted["#" + note_id].add(d["p"])
        for ticket in d["refs"]["tickets"]:
            inverted["@" + ticket].add(d["p"])
    by_path = {d["p"]: d for d in reviewable}
    seen = set()
    for group in inverted.values():
        if len(group) > 40:          # too common a term: says nothing about the pair
            continue
        for a in sorted(group):
            for b in sorted(group):
                if a >= b or (a, b) in seen or linked(a, b):
                    continue
                seen.add((a, b))
                doc_a, doc_b = by_path[a], by_path[b]
                if doc_a["type"] != doc_b["type"]:
                    continue
                title_a, title_b = titles[a], titles[b]
                jaccard = len(title_a & title_b) / max(len(title_a | title_b), 1)
                ids_a, ids_b = set(doc_a["ids"]), set(doc_b["ids"])
                tickets_a, tickets_b = set(doc_a["refs"]["tickets"]), set(doc_b["refs"]["tickets"])
                reason = None
                if jaccard >= 0.6 and (not doc_a["system"] or doc_a["system"] == doc_b["system"]):
                    reason = f"near-identical titles ({jaccard:.2f})"
                elif len(ids_a & ids_b) >= 2 and max(len(ids_a), len(ids_b)) <= 5:
                    reason = "share two or more IDs"
                elif tickets_a and tickets_a == tickets_b and len(tickets_a) <= 2:
                    reason = "same ticket"
                if reason and not (doc_a.get("key") and doc_a.get("key") == doc_b.get("key")):
                    out["possible_contradictions"].append({"a": a, "b": b, "reason": reason})

    # Near duplicates: similarity normalized against the note itself. Only against reviewable
    # notes sharing one of the 8 strongest terms, and without prefix search: all-against-all
    # took doctor from 3 s to 34 s on a real vault.
    threshold = float(settings.get("near_duplicate", 0.85))
    pairs = set()
    by_term = defaultdict(set)
    for d in reviewable:
        for term in RC.top_terms(d)[:8]:
            by_term[term].add(d["p"])
    for d in reviewable:
        own = RC.similarity_score(idx, d, d, prefix=False)
        if own <= 0:
            continue
        nearby = set().union(*(by_term[t] for t in RC.top_terms(d)[:8])) - {d["p"]}
        candidates = []
        for path in nearby:
            other = by_path[path]
            score = RC.similarity_score(idx, d, other, prefix=False)
            if score > 0:
                candidates.append((score, other))
        for score, other in sorted(candidates, key=lambda z: (-z[0], z[1]["p"]))[:3]:
            if other["type"] not in settings["types"] or linked(d["p"], other["p"]) \
                    or state(other, settings) not in CURRENT:
                continue
            normalized = round(score / own, 2)
            pair = tuple(sorted((d["p"], other["p"])))
            if normalized >= threshold and pair not in pairs:
                pairs.add(pair)
                out["near_duplicates"].append({"a": pair[0], "b": pair[1], "score": normalized})
    out["near_duplicates"].sort(key=lambda x: (-x["score"], x["a"], x["b"]))
    # A pair already reported as near duplicate is not repeated as a candidate.
    out["possible_contradictions"] = [c for c in out["possible_contradictions"]
                                      if (c["a"], c["b"]) not in pairs]
    return out
