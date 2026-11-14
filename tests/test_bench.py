"""bench: cases mined from use, honest metrics, tuning that does not fool itself, and the
signals it feeds on (reads through Bash, keyword lists in the gate, the Monday line)."""
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import bench as BN        # noqa: E402
import core as C          # noqa: E402
import recall as RC       # noqa: E402
import today as TD        # noqa: E402

KNOWN = {"Memory/A.md", "Memory/B.md", "Memory/C.md"}


def hook(name):
    """A hook module by path: hooks/recall.py would shadow scripts/recall.py on sys.path."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(f"hook_{name}", REPO / "hooks" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ev(t, kind, sid="s1", **kw):
    return {"t": f"2026-11-10T10:{t:02d}:00", "sid": sid, "ev": kind, **kw}


class TestMine(unittest.TestCase):
    def test_question_then_read_is_a_case(self):
        cases = BN.mine([ev(0, "recall", q="retry policy"), ev(2, "read", p="Memory/A.md"),
                         ev(3, "read", p="Memory/B.md")], KNOWN)
        self.assertEqual([(c["q"], c["relevant"]) for c in cases], [("retry policy", ["Memory/A.md", "Memory/B.md"])])

    def test_reading_what_you_just_wrote_is_not_a_retrieval(self):
        cases = BN.mine([ev(0, "note", p="Memory/A.md"), ev(1, "hook", q="retry policy"),
                         ev(2, "read", p="Memory/A.md")], KNOWN)
        self.assertEqual(cases, [])

    def test_late_reads_unknown_notes_and_other_sessions_do_not_count(self):
        cases = BN.mine([ev(0, "recall", q="retry policy"), ev(20, "read", p="Memory/A.md"),
                         ev(1, "read", p="Memory/zzz.md"), ev(2, "read", p="Memory/B.md", sid="s2")], KNOWN)
        self.assertEqual(cases, [])

    def test_automatic_prompt_cuts_the_link(self):
        cases = BN.mine([ev(0, "recall", q="retry policy"),
                         ev(1, "hook_silent", q="AUTO RUN: nobody is watching", reason="automatic"),
                         ev(2, "read", p="Memory/A.md")], KNOWN, skip=("AUTO RUN",))
        self.assertEqual(cases, [])

    def test_same_question_twice_counts_once(self):
        cases = BN.mine([ev(0, "recall", q="retry policy"), ev(1, "read", p="Memory/A.md"),
                         ev(0, "recall", q="retry policy", sid="s2"), ev(1, "read", p="Memory/B.md", sid="s2")], KNOWN)
        self.assertEqual(len(cases), 1)

    def test_entry_files_and_home_notes_are_not_answers(self):
        docs = [{"p": "README.md", "type": "home"}, {"p": "CLAUDE.md", "type": "note"},
                {"p": "Team/AGENTS.md", "type": "doc"}, {"p": "Daily/2026-11-10.md", "type": "daily"},
                {"p": "Memory/A.md", "type": "doc"}, {"p": "Flat note.md", "type": "note"}]
        cfg = {"recall": {"similar_skip_types": ["daily", "period", "home", "index"]}}
        self.assertEqual(BN.answer_paths(cfg, {"docs": docs}), {"Memory/A.md", "Flat note.md"})

    def test_each_case_keeps_its_kind_of_question(self):
        events = [ev(0, "recall", q="retry policy", sid="a"), ev(1, "read", p="Memory/A.md", sid="a"),
                  ev(0, "gate_miss", q="refund duplicate", sid="b"), ev(1, "read", p="Memory/A.md", sid="b"),
                  ev(0, "gate_hit", q="tls renewal", sid="c"), ev(1, "read", p="Memory/B.md", sid="c"),
                  ev(0, "hook", q="how do we retry payments", sid="d"), ev(1, "read", p="Memory/A.md", sid="d"),
                  ev(0, "hook_brief", q="where is the tls runbook", sid="e"), ev(1, "read", p="Memory/B.md", sid="e"),
                  ev(0, "hook_silent", q="why did the refund fail twice", reason="floor", sid="f"),
                  ev(1, "read", p="Memory/C.md", sid="f"),
                  ev(0, "hook_silent", q="ok do item 3", reason="not_first", sid="g"),
                  ev(1, "read", p="Memory/C.md", sid="g")]
        kinds = {c["q"]: c["kind"] for c in BN.mine(events, KNOWN)}
        self.assertEqual(kinds, {"retry policy": "agent", "refund duplicate": "agent", "tls renewal": "agent",
                                 "how do we retry payments": "prompt", "where is the tls runbook": "prompt",
                                 "why did the refund fail twice": "prompt", "ok do item 3": "follow_up"})

    def test_by_kind_adds_up_and_follows_the_kind_order(self):
        cases = [{"kind": k} for k in ("hand", "follow_up", "agent", "agent", "prompt", "follow_up")]
        ranks = [1, None, 2, None, 1, None]
        groups = BN.by_kind(cases, ranks)
        self.assertEqual(list(groups), ["agent", "prompt", "follow_up", "hand"])
        self.assertEqual(sum(g["cases"] for g in groups.values()), len(cases))
        self.assertEqual(groups["agent"], {"cases": 2, "hit1": 0.0, "hit3": 0.5, "mrr": 0.25,
                                           "mrr_ci": groups["agent"]["mrr_ci"]})
        self.assertEqual(groups["follow_up"]["mrr"], 0.0)
        self.assertNotIn("prompt", BN.by_kind(cases[:1], ranks[:1]))

    def test_prior_never_sees_the_answer(self):
        reads = [ev(1, "read", p="Memory/A.md", sid="s0"), ev(5, "read", p="Memory/A.md", sid="s1"),
                 ev(9, "read", p="Memory/B.md", sid="s0")]
        prior = BN._prior_before(reads, {"t": "2026-11-10T10:06:00", "sid": "s1"})
        self.assertEqual(dict(prior), {"Memory/A.md": 1})


class WithVault(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.vault = self.tmp / "vault"
        shutil.copytree(REPO / "tests" / "example_vault", self.vault)
        old = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(old)))
        os.environ["VAULT_MEMORY_TODAY"] = "2026-11-09"
        self.cfg = C.load_config(self.vault)
        (self.cfg["_state"] / "eval.tsv").write_text(
            "payment retry policy\tRetry policy v2\nrenew TLS certificate\tRenew TLS\n"
            "how to request production access\tproduction access\n", encoding="utf-8")


class TestRun(WithVault):
    def test_metrics_files_and_determinism(self):
        first = BN.run(self.cfg)
        self.assertEqual((first["cases"], first["hand"], first["mined"]), (3, 3, 0))
        self.assertEqual(first["hit3"], 1.0)
        self.assertEqual(first, BN.run(self.cfg))
        for name in ("bench.json", "bench.tsv", "usage.json"):
            self.assertTrue((self.cfg["_state"] / name).exists(), name)
        self.assertIn("verified", json.loads((self.cfg["_state"] / "bench.json").read_text()))

    def test_mined_and_hand_cases_by_kind(self):
        paths = sorted(d["p"] for d in RC.load(self.cfg)["docs"] if "Retry policy v2" in d["p"])
        now = dt.datetime.now().replace(microsecond=0)
        events = [{"t": (now + dt.timedelta(seconds=s)).isoformat(), "sid": sid, "ev": kind, **kw}
                  for s, sid, kind, kw in [
                      (0, "x", "recall", {"q": "payment retry policy v2"}), (5, "x", "read", {"p": paths[0]}),
                      (0, "y", "hook_silent", {"q": "ok go on with that", "reason": "not_first"}),
                      (5, "y", "read", {"p": paths[0]})]]
        with (self.cfg["_state"] / "telemetry.jsonl").open("a", encoding="utf-8") as handle:
            handle.writelines(json.dumps(e) + "\n" for e in events)
        result = BN.run(self.cfg)
        self.assertEqual(list(result["by_kind"]), ["agent", "follow_up", "hand"])
        self.assertEqual(sum(g["cases"] for g in result["by_kind"].values()), result["cases"])
        self.assertEqual(result["by_kind"]["hand"]["cases"], 3)
        self.assertEqual(result, BN.run(self.cfg))
        saved = json.loads((self.cfg["_state"] / "bench.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["by_kind"], result["by_kind"])
        rows = (self.cfg["_state"] / "bench.tsv").read_text(encoding="utf-8").splitlines()
        self.assertTrue(rows[0].endswith("<TAB>kind"))
        self.assertEqual(sorted(r.split("\t")[3] for r in rows[1:]), ["agent", "follow_up"])

    def test_few_cases_never_change_the_config(self):
        result = BN.run(self.cfg, do_tune=True)
        self.assertFalse(result["tune"]["clear"], "3 cases are never enough evidence")

    def test_apply_writes_only_recall_keys_with_a_backup(self):
        before = json.loads((self.cfg["_state"] / "config.json").read_text())
        BN.apply(self.cfg, {"k1": 0.9, "b": 0.72, "title_bonus": 12, "prior_weight": 0.1})
        after = json.loads((self.cfg["_state"] / "config.json").read_text())
        self.assertEqual(after["recall"]["k1"], 0.9)
        self.assertEqual({k: v for k, v in after.items() if k != "recall"},
                         {k: v for k, v in before.items() if k != "recall"})
        self.assertTrue(list((self.cfg["_state"] / "backups").glob("config.json.*-pre-bench")))


class TestPrior(WithVault):
    def test_past_reads_lift_a_note_only_when_enabled(self):
        idx = RC.load(self.cfg)
        query = "retry policy"
        base = {d["p"]: s for s, d in RC.search(idx, query, n=10)}
        target = "Memory/Retry policy v2.md"
        C.PRIOR_WEIGHT = 0.25
        try:
            lifted = {d["p"]: s for s, d in RC.search(idx, query, n=10, prior={target: 6})}
        finally:
            C.PRIOR_WEIGHT = 0.0
        self.assertGreater(lifted[target], base[target])
        self.assertEqual({d["p"]: s for s, d in RC.search(idx, query, n=10, prior={target: 6})}, base)


class TestReadHook(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.base = self.tmp.resolve()
        (self.base / "Memory").mkdir()
        (self.base / "Memory" / "My note.md").write_text("x", encoding="utf-8")

    def paths(self, command, cwd=None):
        return hook("read").note_paths(command, cwd or str(self.base), self.base)

    def test_shell_reads_count(self):
        self.assertEqual(self.paths('cat "Memory/My note.md"'), ["Memory/My note.md"])
        self.assertEqual(self.paths(f"sed -n 1,40p '{self.base}/Memory/My note.md' | head"), ["Memory/My note.md"])
        self.assertEqual(self.paths("head -5 'My note.md'", cwd=str(self.base / "Memory")), ["Memory/My note.md"])

    def test_writes_and_outside_paths_do_not(self):
        self.assertEqual(self.paths('echo hi > "Memory/My note.md"'), [])
        self.assertEqual(self.paths('rm "Memory/My note.md"'), [])
        self.assertEqual(self.paths("cat /etc/README.md"), [])
        self.assertEqual(self.paths('cat "Memory/My note.md" 2>/dev/null'), ["Memory/My note.md"])


class TestGateLists(unittest.TestCase):
    def test_keyword_lists_become_the_query(self):
        GT = hook("gate")
        tools = {"mcp__slack__search": "keywords"}
        self.assertEqual(GT.query_for(tools, "mcp__slack__search_all", {"keywords": ["refund", "duplicate"]}),
                         "refund duplicate")
        self.assertEqual(GT.query_for({"x": "*"}, "x", {"a": "one", "b": ["two"], "c": 3}), "one two")


class TestMondayLine(WithVault):
    def test_only_on_mondays_and_only_when_fresh(self):
        BN.run(self.cfg)
        monday = dt.date(2026, 11, 9)
        self.assertIn("memory last week:", TD._memory_line(self.cfg, monday))
        self.assertEqual(TD._memory_line(self.cfg, monday + dt.timedelta(days=1)), "")
        self.assertEqual(TD._memory_line(self.cfg, monday + dt.timedelta(days=7)), "")


if __name__ == "__main__":
    unittest.main()
