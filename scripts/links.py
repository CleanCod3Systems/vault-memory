"""vault-memory links — similar notes that do not link to each other. Read only.

Obsidian's graph and backlinks only know the links someone wrote. Two notes about the same
thing without a [[wikilink]] between them stay apart, and the reader of one never finds the
other. Similarity is the one `doctor` uses for near duplicates (normalized against the note
itself, candidates from shared strong terms): from MIN to just below the duplicate threshold
the pair is "related", above it `doctor` already reports it as a duplicate.
"""
import re
from collections import defaultdict

import core as C
import recall as RC

MIN = 0.35
GROUP_MAX = 40          # a term shared by more notes than this says nothing about a pair


def _link_names(d):
    names = set()
    for link in d["refs"]["wiki"]:
        target = link.split("|")[0].split("#")[0].strip()
        target = target.rsplit("/", 1)[-1]
        names.add(target[:-3] if target.endswith(".md") else target)
    return names


DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _series(a, b):
    """Two notes of one series (dated siblings, or "Project - X" next to "Project - Y" in the
    same folder): they look alike because they share a template, not a topic."""
    if a["p"].rsplit("/", 1)[0] != b["p"].rsplit("/", 1)[0]:
        return False
    if DATE.search(a["n"]) and DATE.search(b["n"]):
        return True
    prefix_a, sep_a, _ = a["n"].partition(" - ")
    prefix_b, sep_b, _ = b["n"].partition(" - ")
    return bool(sep_a and sep_b and prefix_a.strip().lower() == prefix_b.strip().lower())


def suggest(cfg, n=20):
    idx = RC.load(cfg)
    duplicate = float(cfg["freshness"].get("near_duplicate", 0.85))
    docs = [d for d in idx["docs"] if not d["archived"] and not d.get("template") and "/" in d["p"]
            and d["type"] not in C.SIMILAR_SKIP]
    by_path = {d["p"]: d for d in docs}
    tops = {d["p"]: RC.top_terms(d)[:8] for d in docs}
    by_term = defaultdict(set)
    for path, terms in tops.items():
        for term in terms:
            by_term[term].add(path)
    links = {d["p"]: _link_names(d) for d in docs}
    pairs = {}
    for d in docs:
        own = RC.similarity_score(idx, d, d, prefix=False)
        if own <= 0:
            continue
        nearby = set()
        for term in tops[d["p"]]:
            if len(by_term[term]) <= GROUP_MAX:
                nearby |= by_term[term]
        nearby.discard(d["p"])
        for path in nearby:
            other = by_path[path]
            if other["n"] in links[d["p"]] or d["n"] in links[path] or _series(d, other):
                continue
            score = RC.similarity_score(idx, d, other, prefix=False) / own
            if MIN <= score < duplicate:
                key = tuple(sorted((d["p"], path)))
                pairs[key] = max(pairs.get(key, 0.0), score)
    ranked = sorted(pairs.items(), key=lambda x: (-x[1], x[0]))[:n]
    return [{"a": a, "b": b, "a_n": by_path[a]["n"], "b_n": by_path[b]["n"], "score": round(100 * s, 1)}
            for (a, b), s in ranked]
