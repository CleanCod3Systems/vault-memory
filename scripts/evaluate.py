"""vault-memory eval — measures recall against a set of real cases, without writing anything.

The set lives in the vault (`<state>/eval.tsv`) because the questions that matter are that
vault's. Format: one line per case, `query<TAB>fragment of the expected path`; with `-` as the
expected value it is a conversational query that should bring nothing (its max score is
reported, to watch the noise). `#` comments.

It scans in memory (leaves the state untouched) and applies the same filters as the hook:
drops root notes, the `home`/`index` types and the paths in `recall.skip_paths`.
"""
import sys
from pathlib import Path

import freshness as FR
import hookfilter as HF
import indexer as IX
import recall as RC


def read_cases(path):
    cases, conversational = [], []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        query, _, expected = line.partition("\t")
        expected = expected.strip()
        (conversational if expected in ("", "-") else cases).append((query.strip(), expected))
    return cases, [q for q, _ in conversational]


def usable(cfg):
    skip = tuple(cfg.get("recall", {}).get("skip_paths", []))

    def check(d):
        return ("/" in d["p"] and not (skip and d["p"].startswith(skip))
                and d.get("type") not in HF.SKIP_TYPES)
    return check


def pointers(cfg, idx, query):
    """What the hook would emit for this query (without session dedupe)."""
    should_search, memory_only, _ = HF.decide(query, cfg)
    if not should_search:
        return []
    rows = [{"score": s, **d, "state": FR.state(d)} for s, d in RC.search(idx, query, n=12)]
    hits, _ = HF.select(rows, query, cfg, memory_only)
    return [r["p"] for r in hits]


def run_hook(cfg, cases, conversational, idx, minimum=None):
    hit1 = hit3 = 0
    for query, expected in cases:
        emitted = pointers(cfg, idx, query)
        position = next((i + 1 for i, p in enumerate(emitted) if expected.lower() in p.lower()), None)
        hit1 += position == 1
        hit3 += position is not None
        print(f"  {('HIT' if position else 'MISS'):4} {position or '-':>2}  {query[:60]:60}  "
              f"{emitted[0][-45:] if emitted else '(none)'}")
    noise = [q for q in conversational if pointers(cfg, idx, q)]
    print(f"\n  hook: hit@1 {hit1}/{len(cases)} · hit@3 {hit3}/{len(cases)} · "
          f"emissions on conversational prompts {len(noise)}/{len(conversational)}")
    for query in noise:
        print(f"    noise: {query}")
    if minimum is not None and (hit3 < minimum or noise):
        sys.exit(1)
    return hit1, hit3, len(noise)


def run(cfg, path=None, minimum=None, hook=False):
    path = Path(path) if path else cfg["_state"] / "eval.tsv"
    if not path.exists():
        raise SystemExit(f"No evaluation set at {path}.  Format: query<TAB>expected path")
    cases, conversational = read_cases(path)
    docs, _ = IX.scan(cfg)
    idx, check = {"docs": docs}, usable(cfg)
    if hook:
        return run_hook(cfg, cases, conversational, idx, minimum)

    top1 = 0
    for query, expected in cases:
        results = [(s, d) for s, d in RC.search(idx, query, n=40) if check(d)]
        position = next((i + 1 for i, (_, d) in enumerate(results) if expected.lower() in d["p"].lower()), None)
        score = next((s for s, d in results if expected.lower() in d["p"].lower()), 0)
        top1 += position == 1
        best = results[0][1]["n"][:45] if results else "-"
        print(f"  {position or '-':>3}  {score:5.1f}  {query[:55]:55}  top: {best}")
    if conversational:
        print("\n  conversational (max score):")
        for query in conversational:
            results = [(s, d) for s, d in RC.search(idx, query, n=40) if check(d)]
            print(f"       {results[0][0] if results else 0:5.1f}  {query}")
    print(f"\n  top-1: {top1}/{len(cases)}" + (f"  (minimum {minimum})" if minimum is not None else ""))
    if minimum is not None and top1 < minimum:
        sys.exit(1)
    return top1, len(cases)
