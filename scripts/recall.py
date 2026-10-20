#!/usr/bin/env python3
"""Recall: search the vault before searching anywhere else.

BM25 + domain signals over the state folder's index.json. No network, no model, no tokens.
The index refreshes itself only when it went stale: a new, changed or deleted note, or a
config or tokenization change. It rescans only what changed.
"""
import json
import math
import re
import sys
from collections import defaultdict

import core as C
import freshness as FR
import indexer as IX

K1, B = 1.4, 0.72
RE_TICKET = re.compile(r"^[A-Za-z]{2,6}-\d{1,6}$")


# ─────────────────────────────────────────────────── loading with auto-refresh
def up_to_date(cfg, stored):
    """The index describes exactly the vault as it is now: same tokenization and config, and
    every note with the same fingerprint (mtime, size). A deleted note counts too."""
    if stored.get("tok") != C.TOK_VER or stored.get("cfg") != C.cfg_hash(cfg):
        return False
    saved = {d["p"]: (d.get("mte"), d.get("size")) for d in stored.get("docs", [])}
    seen = 0
    for path, rel in C.walk(cfg):
        if IX.SENSITIVE.search(rel):
            continue
        expected = saved.get(rel)
        try:
            if expected is None or IX.fingerprint(path) != expected:
                return False
        except OSError:
            return False
        seen += 1
    return seen == len(saved)


def _read(cfg):
    try:
        return json.loads((cfg["_state"] / "index.json").read_text(encoding="utf-8"))
    except Exception:
        return None


def load(cfg, auto=True):
    """A recall that answers in the past is worse than one that does not answer."""
    stored = _read(cfg)
    if auto and (stored is None or not up_to_date(cfg, stored)):
        IX.build(cfg, quiet=True, incremental=True)
        print("· index updated", file=sys.stderr)
        stored = _read(cfg)
    if stored is None:
        raise SystemExit("There is no index and it could not be built. Run:  vault-memory index")
    return stored


# ────────────────────────────────────────────────────────────────── scoring
def length(d):
    # The real length of the note (`words`), not the one of the tf cut at `max_terms`: with the
    # cut one a 50k-word daily looked like a short note and won every search.
    return d.get("words") or sum(d["tf"].values()) or 1


def statistics(docs):
    doc_freq = defaultdict(int)
    for d in docs:
        for term in d["tf"]:
            doc_freq[term] += 1
    average = sum(length(d) for d in docs) / max(len(docs), 1)
    return doc_freq, len(docs), average


def stats_of(idx):
    """df over the whole index (minus templates), once per process: the idf does not change
    with the query filter."""
    if "_stats" not in idx:
        idx["_stats"] = statistics([d for d in idx["docs"] if not d.get("template")])
    return idx["_stats"]


def bm25(d, query_terms, doc_freq, total, average, prefix=True):
    doc_length, score = length(d), 0.0
    for term in query_terms:
        freq = d["tf"].get(term, 0)
        if not freq and prefix:                      # prefix: "gatew" matches "gateways"
            freq = sum(v for k, v in d["tf"].items() if k.startswith(term)) * 0.5
        if not freq:
            continue
        idf = math.log(1 + (total - doc_freq.get(term, 1) + 0.5) / (doc_freq.get(term, 1) + 0.5))
        score += idf * (freq * (K1 + 1)) / (freq + K1 * (1 - B + B * doc_length / average))
    return score


def search(idx, query, note_type=None, area=None, status=None, system=None, since=None,
           family=None, include_archive=False, n=8, include_expired=False):
    docs = [d for d in idx["docs"] if include_archive or not d["archived"]]
    # A template is never the answer to anything: it contains {{title}} and sample words that
    # compete with real notes. It only shows up when asked for by type.
    if note_type == "template":
        docs = [d for d in docs if d.get("template")]
        note_type = None
    else:
        docs = [d for d in docs if not d.get("template")]
    if note_type:
        docs = [d for d in docs if d["type"] == note_type]
    if family:
        docs = [d for d in docs if d["family"] == family]
    if area:
        docs = [d for d in docs if d["area"] == area]
    if status:
        docs = [d for d in docs if C.norm(status) in C.norm(d["status"])]
    if system:
        docs = [d for d in docs if C.norm(system) in C.norm(d["system"])]
    if since:
        docs = [d for d in docs if (d["date"] or d["mt"]) >= since]
    if not docs:
        return []

    doc_freq, total, average = stats_of(idx)
    query = query.strip()
    query_terms = list(C.tokens(query, keep_stopwords=True))
    normalized = C.norm(query)
    # An ID or a ticket is searched exactly: any partial match is noise.
    exact = bool(re.fullmatch(r"\d{3,10}", query) or RE_TICKET.match(query))
    # IDs and tickets inside a text query ("order 48213 stuck"): the tokenizer drops numbers,
    # so without this the ID weighed nothing.
    query_ids = set(re.findall(r"\b\d{3,10}\b", query))
    query_tickets = {x.upper() for x in re.findall(r"\b[A-Za-z]{2,6}-\d{1,6}\b", query)}
    query_set = set(C.tokens(query))
    out = []
    for d in docs:
        if exact:
            score = 0.0
            if query in d["ids"]:
                score += 60
            if query.upper() in [x.upper() for x in d["refs"]["tickets"]]:
                score += 60
            if normalized in C.norm(d["n"] + " " + d["t"]):
                score += 12
        else:
            score = bm25(d, query_terms, doc_freq, total, average)
            if score > 0 and query_set:
                # How much of the query is in the name/title: BM25 alone did not reward a note
                # whose title is almost the query against long notes with many mentions.
                score += C.TITLE_BONUS * len(query_set & set(C.tokens(d["n"] + " " + d["t"]))) / len(query_set)
            if query_ids & set(d["ids"]):
                score += 25
            if query_tickets & {x.upper() for x in d["refs"]["tickets"]}:
                score += 25
            if normalized and normalized in C.norm(d["n"]):
                score += 12
            if normalized and normalized in C.norm(d["t"]):
                score += 8
            if any(normalized and normalized in C.norm(h) for h in d["h2"]):
                score += 3
            # The "is open" and "is severe" bonuses break ties between notes that ALREADY
            # match. Applied to all of them, every query without results became a list of
            # everything open in the vault.
            if score > 0:
                if d["status"] and C.norm(d["status"]) not in C.CLOSED:
                    score += 1.5
                if C.norm(d["sev"]) in C.HIGH_SEVERITY:
                    score += 1.0
        # Freshness: replaced or expired notes go down (they do not disappear: they may be
        # exactly what is being searched). An exact ID is not penalized.
        if score > 0 and not exact and not include_expired:
            score *= FR.factor(d)
        if score > 0:
            out.append((score, d))
    out.sort(key=lambda x: (-x[0], x[1]["p"]))
    if not out and exact:
        # It looked like a ticket or an ID but it is not one in this vault (or no prefixes are
        # configured). Before saying "nothing", search it as text.
        return search(idx, " ".join(re.split(r"[-_]", query)), note_type, area, status, system,
                      since, family, include_archive, n, include_expired)
    return out[:n]


# ─────────────────────────────────────────────────────────────────── passages
def passage(cfg, d, query_terms, width=300):
    """The actual paragraph the query hits. The file is read, not the index: the index keeps
    terms, not text, so it does not need to grow."""
    path = cfg["_vault"] / d["p"]
    if not path.exists():
        return d["s"]
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return d["s"]
    best, chosen = 0, None
    for paragraph in re.split(r"\n\s*\n", text):
        paragraph = paragraph.strip()
        if len(paragraph) < 40 or paragraph.startswith("---"):
            continue
        normalized = C.norm(paragraph)
        hits = sum(normalized.count(t) for t in query_terms)
        if hits > best:
            best, chosen = hits, paragraph
    paragraph = chosen or d["s"]
    paragraph = re.sub(r"\[\[([^\]|]+)\|?[^\]]*\]\]", r"\1", paragraph)
    paragraph = re.sub(r"\s+", " ", re.sub(r"[*`>]", "", paragraph)).strip()
    return paragraph[:width] + ("…" if len(paragraph) > width else "")


# ───────────────────────────────────────────────────────────────── similarity
def top_terms(base):
    return [t for t, _ in sorted(base["tf"].items(), key=lambda x: (-x[1], x[0]))[:60]]


def similarity_score(idx, base, d, terms=None, prefix=True):
    """How much `d` looks like `base`. With base == d it gives the maximum possible for that
    note: used to normalize (near duplicate = own score × threshold)."""
    doc_freq, total, average = stats_of(idx)
    score = bm25(d, terms or top_terms(base), doc_freq, total, average, prefix)
    if base["type"] == d["type"]:
        score *= 1.25
    if base["system"] and base["system"] == d["system"]:
        score *= 1.15
    if set(base["ids"]) & set(d["ids"]):
        score += 25
    return score


def similar_to_doc(idx, base, n=5):
    """Neighbors of any doc, whether or not it is in the index (a `vault-memory note` draft)."""
    terms, out = top_terms(base), []
    for d in idx["docs"]:
        if d["p"] == base.get("p") or d["archived"] or d.get("template"):
            continue
        # A monthly daily (thousands of words), a weekly summary or an index shares terms
        # with every note: as a neighbor it does not tell the story, it hides it.
        if d["type"] in C.SIMILAR_SKIP:
            continue
        score = similarity_score(idx, base, d, terms)
        if score > 0:
            out.append((round(score, 2), d))
    out.sort(key=lambda x: (-x[0], x[1]["p"]))
    return out[:n]


def similar(idx, ref, n=5):
    docs = {d["p"]: d for d in idx["docs"]}
    base = docs.get(ref) or next((d for d in idx["docs"]
                                  if d["n"] == ref or d["p"].endswith(ref)), None)
    return similar_to_doc(idx, base, n) if base else []


def write_related(cfg, idx):
    """Precomputed neighbors: each record note comes with its history."""
    related = {}
    for d in idx["docs"]:
        if d["family"] != "record" and d["type"] != "runbook":
            continue
        if d["archived"] or d.get("template") or d["type"] == "daily":
            continue
        related[d["p"]] = [{"p": x["p"], "n": x["n"], "t": x["t"], "type": x["type"],
                            "date": x["date"], "status": x["status"], "score": score}
                           for score, x in similar(idx, d["p"], 4)]
    (cfg["_state"] / "related.json").write_text(
        json.dumps({"generated": idx.get("generated", ""), "items": related}, ensure_ascii=False),
        encoding="utf-8")
    return len(related)


# ──────────────────────────────────────────────────────────────────── output
ICON = {"incident": "🔴", "runbook": "📘", "project": "📦", "meeting": "🗣",
        "decision": "⚖", "period": "📅", "daily": "📓", "reference": "🗄",
        "doc": "📄", "home": "🏠", "template": "🧩", "note": "📝"}


def print_results(cfg, results, query_terms):
    if not results:
        print("No results in the vault. Only now is it worth searching elsewhere.")
        return
    for i, (score, d) in enumerate(results, 1):
        meta = " · ".join(x for x in [d["type"], d["area"], d["date"] or d["mt"],
                                      d["status"], d["sev"], d["system"]] if x)
        print(f"\n{i}. {ICON.get(d['type'], '·')} {d['t']}   [{score:.1f}]")
        print(f"   {meta}")
        print(f"   {d['p']}")
        print(f"   {passage(cfg, d, query_terms)}")
        if d["refs"]["tickets"]:
            print("   tickets: " + ", ".join(d["refs"]["tickets"][:4]))
        if d["ids"]:
            print("   ids: " + ", ".join(d["ids"][:6]))
        if d["refs"]["chat"]:
            print("   source: " + d["refs"]["chat"][0])
