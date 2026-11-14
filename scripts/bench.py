"""vault-memory bench — does recall find what actually gets used? Measured on this vault's own
history, with confidence intervals, and tuned only when the evidence says so.

Cases come from two places:
  - mined: a question (prompt the hook saw, or a `recall`) followed, in the same session and
    within `WINDOW` seconds, by reading a vault note. The note read is the answer that was
    needed. Notes written in that same session are left out: reading what you just wrote is
    not a retrieval.
  - written by hand: `<state>/eval.tsv` (query<TAB>fragment of the expected path).

Metrics per case: rank of the first relevant note among the results the hook would accept;
hit@1, hit@3 and MRR@10 with a seeded bootstrap 95% interval (same input, same output).
They are also given by kind of question (`KINDS`), because one mixed number hides what the
system does: a query an agent wrote is keyword-like, a prompt is a person's sentence, and a
follow-up ("ok, do item 3") is answered by the conversation, not by its own words.

Tuning (`--tune`) tries one parameter at a time (BM25 k1 and b, the title bonus, the weight of
past use) and keeps a change only if the paired bootstrap of the difference stays above zero
and there are at least `MIN_CASES` cases. The use prior is computed without leaks: for each
mined case only reads before it, from other sessions, count.
"""
import datetime as dt
import json
import random
from collections import Counter, defaultdict

import core as C
import evaluate as EV
import hookfilter as HF
import recall as RC
import telemetry as TL

WINDOW = 900                   # seconds between the question and the read
DAYS = 120
MIN_CASES = 30
RESAMPLES = 1000
SILENT_OK = {"domain", "floor", "repeated", "not_first"}
# agent: a query an agent wrote (recall, or the gate before a tool) · prompt: a prompt the
# hook searched · follow_up: a later prompt `hook.mode: first` does not search · hand: eval.tsv
KINDS = ("agent", "prompt", "follow_up", "hand")
GRID = {"k1": [0.9, 1.2, 1.4, 1.8], "b": [0.5, 0.72, 0.9], "title_bonus": [4, 8, 12],
        "prior_weight": [0.0, 0.1, 0.25]}


def _ts(text):
    return dt.datetime.fromisoformat(text)


def _is_question(event, skip=()):
    q = (event.get("q") or "").strip()
    if not q or q.startswith(("<", "/", "!", *skip)):
        return False
    kind = event.get("ev")
    if kind in ("hook", "hook_brief", "recall", "gate_hit", "gate_miss"):
        return True
    return kind == "hook_silent" and event.get("reason") in SILENT_OK


def _kind(event):
    """Kind of question (`KINDS`) of an event `_is_question` accepted."""
    if event.get("ev") in ("recall", "gate_hit", "gate_miss"):
        return "agent"
    if event.get("ev") == "hook_silent" and event.get("reason") == "not_first":
        return "follow_up"
    return "prompt"


def mine(events, known_paths, skip=()):
    """[{"q", "relevant": [paths], "t", "sid", "kind"}] from telemetry, oldest first. `skip`:
    prompt prefixes of automatic runs (the hook's `skip_prefixes`): what they read is not an answer."""
    by_session = defaultdict(list)
    for event in events:
        by_session[event.get("sid", "")].append(event)
    cases = {}
    for sid, session in by_session.items():
        session.sort(key=lambda e: e["t"])
        written, question = set(), None
        for event in session:
            kind = event.get("ev")
            if kind == "note" and event.get("p"):
                written.add(event["p"])
            elif _is_question(event, skip):
                question = event
            elif event.get("q"):
                question = None            # an automatic or chat prompt: later reads are not answers
            elif kind == "read" and question and event.get("p") in known_paths \
                    and event["p"] not in written \
                    and (_ts(event["t"]) - _ts(question["t"])).total_seconds() <= WINDOW:
                key = (sid, question["t"], question["q"])
                case = cases.setdefault(key, {"q": question["q"], "relevant": [], "t": question["t"], "sid": sid,
                                              "kind": _kind(question)})
                if event["p"] not in case["relevant"]:
                    case["relevant"].append(event["p"])
    # The same question asked twice counts once (the latest), so a repeated prompt does not
    # weigh double.
    latest = {}
    for case in sorted(cases.values(), key=lambda c: c["t"]):
        latest[case["q"]] = case
    return sorted(latest.values(), key=lambda c: c["t"])


def hand_cases(cfg):
    path = cfg["_state"] / "eval.tsv"
    if not path.exists():
        return []
    cases, _ = EV.read_cases(path)
    return [{"q": q, "fragment": expected, "t": "", "sid": "", "kind": "hand"} for q, expected in cases]


def _prior_before(reads, case):
    """Reads before the case, from other sessions: the prior never sees the answer."""
    if not case.get("t"):
        return Counter(e["p"] for e in reads)
    return Counter(e["p"] for e in reads if e["t"] < case["t"] and e.get("sid") != case.get("sid"))


def rank(idx, case, check, prior):
    results = [d for _, d in RC.search(idx, case["q"], n=30, prior=prior) if check(d)][:10]
    for position, d in enumerate(results, 1):
        if case.get("fragment"):
            if case["fragment"].lower() in d["p"].lower():
                return position
        elif d["p"] in case["relevant"]:
            return position
    return None


def _metrics(ranks):
    n = len(ranks) or 1
    return {"hit1": sum(r == 1 for r in ranks if r) / n,
            "hit3": sum(1 for r in ranks if r and r <= 3) / n,
            "mrr": sum(1 / r for r in ranks if r) / n}


def _interval(values, seed=42):
    if not values:
        return [0.0, 0.0]
    rng = random.Random(seed)
    means = sorted(sum(rng.choice(values) for _ in values) / len(values) for _ in range(RESAMPLES))
    return [round(means[int(0.025 * RESAMPLES)], 3), round(means[int(0.975 * RESAMPLES) - 1], 3)]


def _summary(ranks):
    metrics = _metrics(ranks)
    metrics["mrr_ci"] = _interval([1 / r if r else 0.0 for r in ranks])
    return {k: (round(v, 3) if isinstance(v, float) else v) for k, v in metrics.items()}


def by_kind(cases, ranks):
    """{kind: {"cases", "hit1", "hit3", "mrr", "mrr_ci"}} for each kind with cases, in `KINDS` order."""
    out = {}
    for kind in KINDS:
        own = [r for c, r in zip(cases, ranks) if c.get("kind") == kind]
        if own:
            out[kind] = {"cases": len(own), **_summary(own)}
    return out


def evaluate(cfg, idx, cases, reads, params=None):
    """Ranks and metrics with `params` (k1, b, title_bonus, prior_weight) applied for the run."""
    saved = (C.BM25_K1, C.BM25_B, C.TITLE_BONUS, C.PRIOR_WEIGHT)
    params = params or {}
    C.BM25_K1 = float(params.get("k1", C.BM25_K1))
    C.BM25_B = float(params.get("b", C.BM25_B))
    C.TITLE_BONUS = float(params.get("title_bonus", C.TITLE_BONUS))
    C.PRIOR_WEIGHT = float(params.get("prior_weight", C.PRIOR_WEIGHT))
    try:
        check = EV.usable(cfg)
        ranks = [rank(idx, case, check, _prior_before(reads, case) if C.PRIOR_WEIGHT else {})
                 for case in cases]
    finally:
        C.BM25_K1, C.BM25_B, C.TITLE_BONUS, C.PRIOR_WEIGHT = saved
    return _summary(ranks), ranks


def current_params(cfg):
    recall = cfg["recall"]
    return {"k1": float(recall.get("k1", 1.4)), "b": float(recall.get("b", 0.72)),
            "title_bonus": float(recall.get("title_bonus", 8)),
            "prior_weight": float(recall.get("prior_weight", 0) or 0)}


def _search_best(cfg, idx, cases, reads, start):
    """Coordinate search: one parameter at a time, keep what raises MRR on these cases."""
    best = dict(start)
    best_metrics, _ = evaluate(cfg, idx, cases, reads, best)
    tried = 0
    for name, values in GRID.items():
        for value in values:
            if value == best[name]:
                continue
            candidate = {**best, name: value}
            metrics, _ = evaluate(cfg, idx, cases, reads, candidate)
            tried += 1
            if metrics["mrr"] > best_metrics["mrr"]:
                best, best_metrics = candidate, metrics
    return best, tried


def _fold(case):
    """Deterministic half: tuning and checking never share a case."""
    return sum(map(ord, case["q"])) % 2


def tune(cfg, idx, cases, reads):
    """Pick parameters on one half of the cases and measure the gain on the other half (both
    ways), so choosing the winner does not inflate the gain. The change is clear only if the
    paired bootstrap of those held-out gains stays above zero, with `MIN_CASES`+ cases."""
    base = current_params(cfg)
    held_out_diffs, tried = [], 0
    for fold in (0, 1):
        train = [c for c in cases if _fold(c) != fold]
        test = [c for c in cases if _fold(c) == fold]
        if not train or not test:
            continue
        picked, count = _search_best(cfg, idx, train, reads, base)
        tried += count
        _, base_ranks = evaluate(cfg, idx, test, reads, base)
        _, new_ranks = evaluate(cfg, idx, test, reads, picked)
        held_out_diffs += [(1 / b if b else 0.0) - (1 / a if a else 0.0) for a, b in zip(base_ranks, new_ranks)]
    best, count = _search_best(cfg, idx, cases, reads, base)
    tried += count
    base_metrics, _ = evaluate(cfg, idx, cases, reads, base)
    best_metrics, _ = evaluate(cfg, idx, cases, reads, best)
    gain_ci = _interval(held_out_diffs)
    held_out = round(sum(held_out_diffs) / len(held_out_diffs), 3) if held_out_diffs else 0.0
    clear = best != base and len(cases) >= MIN_CASES and gain_ci[0] > 0
    return {"current": {**base, **base_metrics}, "best": {**best, **best_metrics},
            "held_out_gain": held_out, "gain_ci": gain_ci, "clear": clear,
            "cases": len(cases), "tried": tried}


# Notes an agent opens to find its way, not to answer: entry files (read at the start of a
# session whatever was asked) and home, index, daily and period notes. A read of one after a
# question is not evidence that it answered it, and made most mined cases unanswerable.
ENTRY_FILES = {"readme.md", "claude.md", "agents.md", "gemini.md"}


def answer_paths(cfg, idx):
    """Notes whose read after a question counts as its answer."""
    skip = set(cfg["recall"].get("similar_skip_types", [])) | {"home", "template"}
    return {d["p"] for d in idx["docs"]
            if d["p"].rsplit("/", 1)[-1].lower() not in ENTRY_FILES and d["type"] not in skip}


def reads_of(events, known_paths):
    return sorted((e for e in events if e.get("ev") == "read" and e.get("p") in known_paths),
                  key=lambda e: e["t"])


def write_usage(cfg, reads):
    """`usage.json`: reads per note, the prior recall uses when `recall.prior_weight` > 0."""
    counts = Counter(e["p"] for e in reads)
    (cfg["_state"] / "usage.json").write_text(json.dumps(
        {"generated": C.today().isoformat(), "reads": dict(sorted(counts.items()))},
        ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def run(cfg, days=DAYS, do_tune=False, write=True):
    idx = RC.load(cfg)
    known = {d["p"] for d in idx["docs"]}
    events = TL.read(cfg["_vault"], days=days)
    reads = reads_of(events, known)
    mined = mine(events, answer_paths(cfg, idx), tuple(HF.options(cfg).get("skip_prefixes", [])))
    cases = mined + hand_cases(cfg)
    metrics, ranks = evaluate(cfg, idx, cases, reads)
    result = {"generated": C.today().isoformat(), "days": days, "cases": len(cases),
              "mined": len(mined), "hand": len(cases) - len(mined), **metrics,
              "by_kind": by_kind(cases, ranks),
              "misses": [c["q"][:80] for c, r in zip(cases, ranks) if not r][:10]}
    # The weekly "did it help?" numbers travel with it, so the daily summary only reads a file.
    import review as RV
    week = TL.metrics(TL.read(cfg["_vault"], days=7))
    result["week"] = {k: week[k] for k in ("pointers", "pointers_read", "pct_read")}
    result["verified"] = RV.queue(cfg, n=1)["coverage"]
    if do_tune:
        result["tune"] = tune(cfg, idx, cases, reads)
    if write:
        state = cfg["_state"]
        lines = ["# mined from telemetry: query<TAB>notes read after it<TAB>when<TAB>kind"]
        lines += [f"{c['q'].replace(chr(9), ' ').replace(chr(10), ' ')}\t{' | '.join(c['relevant'])}\t{c['t']}"
                  f"\t{c['kind']}" for c in mined]
        (state / "bench.tsv").write_text("\n".join(lines) + "\n", encoding="utf-8")
        (state / "bench.json").write_text(json.dumps({k: v for k, v in result.items() if k != "misses"},
                                                     ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        write_usage(cfg, reads)
    return result


def apply(cfg, params):
    """Write the tuned recall parameters into the vault's config.json (backup first)."""
    path = cfg["_state"] / "config.json"
    own = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if path.exists():
        backup = cfg["_state"] / "backups" / f"config.json.{dt.datetime.now():%Y%m%d-%H%M%S}-pre-bench"
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    own.setdefault("recall", {}).update({k: params[k] for k in ("k1", "b", "title_bonus", "prior_weight")})
    path.write_text(json.dumps(own, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return path
