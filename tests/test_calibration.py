"""calibration: the review log, confidence held up at the next review, learned-note precision,
when distill may write alone, and today's review budget. Invented data only."""
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPO / "tests"))

import calibration as CB  # noqa: E402
import core as C          # noqa: E402
import distill as DS      # noqa: E402
import note as N          # noqa: E402
import review as RV       # noqa: E402
import today as TD        # noqa: E402
from test_distill import DAY, FakeProvider, Pipeline, candidate  # noqa: E402

TODAY = "2026-11-10"
ACCESS = "Memory/Request production access.md"


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.vault = self.tmp / "vault"
        shutil.copytree(REPO / "tests" / "example_vault", self.vault)
        old = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(old)))
        os.environ["VAULT_MEMORY_TODAY"] = TODAY
        for name in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID"):
            os.environ.pop(name, None)

    def cfg(self, **distill):
        cfg = C.load_config(self.vault)
        if distill:
            cfg["distill"] = {**(cfg.get("distill") or {}), **distill}
        return cfg

    def log(self):
        path = self.vault / ".vault-memory" / "reviews.jsonl"
        return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def learned(self, title, body="Billing parks a charge after 3 retries."):
        return N.write(self.cfg(), title=title, note_type="doc", folder="Memory/learned", body=body,
                       source="distill 2026-11-09 aaaa1111", expires="2027-02-07", force=True)["p"]

    def event(self, p, verdict, prev=None, prev_confidence=None, learned=False,
              prev_checked="2026-10-01", day=TODAY):
        CB.append(self.cfg(), {"day": day, "p": p, "verdict": verdict, "confidence": None,
                               "volatility": None, "learned": learned, "prev_verdict": prev,
                               "prev_confidence": prev_confidence, "prev_checked": prev_checked})

    def audits(self, confirmed, outdated):
        for i in range(confirmed + outdated):
            self.event(f"Memory/learned/n{i:02}.md", "confirmed" if i < confirmed else "outdated", learned=True)

    def cli(self, *args):
        result = subprocess.run([sys.executable, "-B", str(SCRIPTS / "cli.py"), "--vault", str(self.vault), *args],
                                capture_output=True, text=True, env={**os.environ})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout


class TestLog(Env):
    def test_each_review_appends_one_line_with_the_previous_check(self):
        RV.record(self.cfg(), ACCESS, "unsure", "no access to the tracker", 6,
                  today=dt.date(2026, 10, 1))
        RV.record(self.cfg(), ACCESS, "confirmed", "ticket PLAT-12 shows the same flow", 9, "stable")
        first, second = self.log()
        self.assertEqual((first["verdict"], first["confidence"], first["prev_verdict"], first["learned"]),
                         ("unsure", 6, None, False))
        self.assertEqual((second["day"], second["verdict"], second["confidence"], second["volatility"]),
                         (TODAY, "confirmed", 9, "stable"))
        # the log keeps the confidence of an unsure, which the frontmatter drops
        self.assertEqual((second["prev_verdict"], second["prev_confidence"], second["prev_checked"]),
                         ("unsure", 6, "2026-10-01"))
        self.assertNotIn("evidence", second)          # telemetry style: no note text

    def test_previous_check_comes_from_frontmatter_when_the_log_has_none(self):
        RV.record(self.cfg(), ACCESS, "confirmed", "runbook in the wiki", 10, today=dt.date(2026, 9, 1))
        (self.vault / ".vault-memory" / "reviews.jsonl").unlink()
        RV.record(self.cfg(), ACCESS, "outdated", "the team now uses a form")
        [line] = self.log()
        self.assertEqual((line["prev_verdict"], line["prev_confidence"], line["prev_checked"]),
                         ("confirmed", 10, "2026-09-01"))

    def test_learned_notes_are_flagged(self):
        rel = self.learned("Parked charges")
        RV.record(self.cfg(), rel, "confirmed", "billing-api config retries=3", 9)
        self.assertTrue(self.log()[0]["learned"])

    def test_a_rejected_review_writes_no_line(self):
        with self.assertRaises(N.NoteError):
            RV.record(self.cfg(), ACCESS, "confirmed", "it looks right", 7)
        self.assertEqual(self.log(), [])


class TestCalibration(Env):
    def test_wilson(self):
        self.assertEqual(CB.wilson(0, 0), (None, None))
        self.assertEqual(CB.wilson(8, 10), (0.49, 0.943))
        self.assertEqual(CB.wilson(10, 10), (0.722, 1.0))

    def test_hold_rate_per_bucket(self):
        for i in range(9):
            self.event(f"Memory/a{i}.md", "confirmed", "confirmed", 9)
        self.event("Memory/b.md", "outdated", "confirmed", 9)
        self.event("Memory/c.md", "unsure", "confirmed", 9)              # settles nothing
        self.event("Memory/d.md", "outdated", "confirmed", 10)
        self.event("Memory/e.md", "confirmed", "unsure", 5)              # a low-confidence check
        self.event("Memory/f.md", "outdated", "confirmed", 9, prev_checked=TODAY)   # same-day fix
        self.event("Memory/g.md", "confirmed", "outdated", None)        # said "does not hold"
        data = CB.report(self.cfg())
        rows = {r["bucket"]: r for r in data["buckets"]}
        nine = rows["9"]
        self.assertEqual((nine["n"], nine["held"], nine["failed"], nine["unsure_next"]), (10, 9, 1, 1))
        self.assertEqual((nine["rate"], nine["low"], nine["high"]), (0.9, 0.596, 0.982))
        self.assertTrue(nine["enough"])
        self.assertEqual(nine["calibrated"], 0.9)
        self.assertEqual(nine["stated"], 0.9)
        ten = rows["10"]
        self.assertEqual((ten["n"], ten["failed"], ten["enough"], ten["calibrated"]), (1, 1, False, None))
        self.assertEqual((rows["<=7"]["n"], rows["<=7"]["held"], rows["<=7"]["stated"]), (1, 1, None))
        self.assertEqual((rows["8"]["n"], rows["8"]["rate"], rows["8"]["low"]), (0, None, None))
        self.assertEqual(data["same_day"], 1)
        self.assertEqual(data["reviews"], 15)

    def test_empty_log_reports_nothing_invented(self):
        data = CB.report(self.cfg())
        self.assertEqual(data["reviews"], 0)
        self.assertTrue(all(r["n"] == 0 and r["calibrated"] is None for r in data["buckets"]))
        self.assertIsNone(data["learned"]["precision"])

    def test_deterministic(self):
        self.audits(3, 1)
        self.assertEqual(CB.report(self.cfg()), CB.report(self.cfg()))


class TestPrecision(Env):
    def test_first_settled_verdict_per_learned_note(self):
        self.event("Memory/learned/a.md", "unsure", learned=True)
        self.event("Memory/learned/a.md", "confirmed", "unsure", 6, learned=True)
        self.event("Memory/learned/a.md", "outdated", "confirmed", 9, learned=True)   # later: the world changed
        self.event("Memory/learned/b.md", "outdated", learned=True)
        self.event("Memory/learned/c.md", "unsure", learned=True)
        self.event("Memory/learned/d.md", "outdated", "confirmed", 9, learned=True)   # audited before the log
        self.event("Memory/hand.md", "outdated", learned=False)
        learned = CB.precision(CB.read(self.cfg()))
        self.assertEqual((learned["audited"], learned["confirmed"], learned["outdated"], learned["unsettled"]),
                         (3, 2, 1, 1))
        self.assertEqual(learned["precision"], 0.667)

    def test_an_inbox_rejection_counts_like_an_outdated_learned_note(self):
        import inbox as IB
        cfg = self.cfg()
        DS.write_inbox(cfg, candidate(), {"sid": "aaaa1111-0000"}, DAY, 1, False)
        IB.reject(cfg, IB.pending(cfg)[0]["id"], "the ticket says otherwise")
        self.event("Memory/learned/b.md", "outdated", learned=True)
        rejected, reviewed = CB.read(cfg)
        self.assertEqual(CB.precision([rejected]), CB.precision([reviewed]))
        self.assertEqual(CB.buckets([rejected])[0], CB.buckets([reviewed])[0])   # no confidence to calibrate
        learned = CB.precision([rejected, reviewed])
        self.assertEqual((learned["audited"], learned["outdated"]), (2, 2))


class TestAutonomy(Env):
    def test_off_and_on_keep_their_meaning(self):
        self.assertFalse(CB.autonomy(self.cfg(auto=False))["auto"])
        self.assertTrue(CB.autonomy(self.cfg(auto=True))["auto"])

    def test_earned_needs_enough_audits(self):
        self.audits(5, 0)
        decision = CB.autonomy(self.cfg(auto="earned"))
        self.assertFalse(decision["auto"])
        self.assertIn("5 of 20 audits", decision["reason"])

    def test_earned_needs_the_lower_bound_over_the_threshold(self):
        self.audits(20, 5)                          # 80% observed, lower bound 0.61
        decision = CB.autonomy(self.cfg(auto="earned"))
        self.assertFalse(decision["auto"])
        self.assertIn("0.61 < 0.80", decision["reason"])
        self.assertTrue(CB.autonomy(self.cfg(auto="earned", earned_min_precision=0.6))["auto"])

    def test_earned_with_strong_evidence_writes_notes(self):
        self.audits(25, 0)                          # lower bound 0.867
        decision = CB.autonomy(self.cfg(auto="earned"))
        self.assertTrue(decision["auto"])
        self.assertIn("0.87 >= 0.80", decision["reason"])


class TestDistillEarned(Pipeline):
    def earn(self, confirmed):
        cfg = C.load_config(self.vault)
        for i in range(confirmed):
            CB.append(cfg, {"day": DAY, "p": f"Memory/learned/n{i:02}.md", "verdict": "confirmed",
                            "learned": True, "prev_verdict": None})

    def test_without_evidence_learned_notes_go_to_the_inbox(self):
        result = DS.run(self.cfg(auto="earned"), day=DAY, provider=FakeProvider([candidate()]))
        self.assertEqual(result["kept"], 1)
        self.assertEqual(len(self.inbox()), 1)
        self.assertEqual(result["write"]["to"], "inbox")
        self.assertIn("not enough yet", result["write"]["reason"])
        self.assertFalse(any(p.startswith("Memory/learned/") for p in self.vault_tree()))
        self.assertIn('"write": "inbox"', self.telemetry())

    def test_with_evidence_they_are_written_as_notes(self):
        self.earn(25)
        result = DS.run(self.cfg(auto="earned"), day=DAY, provider=FakeProvider([candidate()]))
        self.assertEqual(result["write"]["to"], "notes")
        self.assertEqual(self.inbox(), [])
        self.assertTrue(any(p.startswith("Memory/learned/") for p in self.vault_tree()))

    def test_plain_auto_is_reported_too(self):
        result = DS.run(self.cfg(auto=True), day=DAY, provider=FakeProvider([]))
        self.assertEqual(result["write"], {"to": "notes", "reason": "distill.auto is on"})


class TestBudget(Env):
    def setUp(self):
        super().setUp()
        for i in range(8):                          # live states past their 21-day interval
            N.write(self.cfg(), title=f"Gateway {i} status", note_type="doc", folder="Memory",
                    body=f"Gateway {i} is enabled for card payments in region {i}.",
                    verified="2026-09-01", force=True, fields={"volatility": "live"})

    def test_base_plus_high_priority_up_to_max(self):
        plan = RV.queue(self.cfg())["budget"]
        self.assertEqual(plan["high"], 2)                       # the two notes sharing a key
        self.assertEqual((plan["base"], plan["max"]), (4, 8))
        self.assertEqual(plan["n"], 6)
        for i in range(5):
            self.learned(f"Learned fact {i}", f"Service {i} retries {i + 2} times before parking.")
        plan = RV.queue(self.cfg())["budget"]
        self.assertEqual((plan["high"], plan["n"]), (7, 8))

    def test_budget_cuts_the_queue_and_never_exceeds_what_is_due(self):
        result = RV.queue(self.cfg(), use_budget=True)
        self.assertEqual(len(result["queue"]), result["budget"]["n"])
        self.assertLessEqual(result["budget"]["n"], result["coverage"]["due"])
        self.assertEqual(RV.queue(self.cfg(), use_budget=True, cap=5)["budget"]["n"], 5)

    def test_config(self):
        cfg = self.cfg()
        cfg["review"] = {"daily": {"base": 1, "max": 2}}
        self.assertEqual(RV.queue(cfg)["budget"], {"n": 2, "base": 1, "max": 2, "high": 2})

    def test_cli_json(self):
        out = json.loads(self.cli("review", "--json", "--budget"))
        self.assertEqual(len(out["queue"]), out["budget"]["n"])
        self.assertEqual(json.loads(self.cli("review", "--json", "--budget", "3"))["budget"]["n"], 3)
        self.assertIn("budget", json.loads(self.cli("review", "--json")))
        self.assertIn("budget today", self.cli("review"))


class TestCli(Env):
    def test_calibration_json_and_text(self):
        self.audits(3, 1)
        out = json.loads(self.cli("review", "--calibration", "--json"))
        self.assertEqual(set(out), {"reviews", "min_samples", "buckets", "same_day", "learned",
                                    "autonomy", "earned"})
        self.assertEqual([b["bucket"] for b in out["buckets"]], ["10", "9", "8", "<=7"])
        self.assertEqual(out["learned"]["audited"], 4)
        text = self.cli("review", "--calibration")
        self.assertIn("too few", text)
        self.assertIn("learned notes  audited 4", text)


class TestToday(Env):
    def test_learned_line_only_on_mondays_with_enough_audits(self):
        monday, tuesday = dt.date(2026, 11, 9), dt.date(2026, 11, 10)
        self.audits(5, 0)
        self.assertEqual(TD._learned_line(self.cfg(), monday), "")
        self.audits(25, 0)
        self.assertEqual(TD._learned_line(self.cfg(), tuesday), "")
        line = TD._learned_line(self.cfg(auto="earned"), monday)
        self.assertTrue(line.startswith("learned notes: 100% confirmed on 25 audits"), line)
        self.assertTrue(line.endswith("distill → notes"), line)


if __name__ == "__main__":
    unittest.main()
