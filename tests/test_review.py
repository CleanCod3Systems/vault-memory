"""review: the queue of notes to check against their source, and how a check is recorded."""
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

import core as C          # noqa: E402
import freshness as FR    # noqa: E402
import hookfilter as HF   # noqa: E402
import note as N          # noqa: E402
import recall as RC       # noqa: E402
import review as RV       # noqa: E402
import telemetry as TL    # noqa: E402

TODAY = "2026-11-10"
TEAM_A = "Memory/On-call schedule - team A.md"
TEAM_B = "Memory/On-call schedule - team B.md"
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

    def cfg(self):
        return C.load_config(self.vault)

    def text(self, rel):
        return (self.vault / rel).read_text(encoding="utf-8")

    def queue(self, **kw):
        return RV.queue(self.cfg(), **kw)

    def paths(self, **kw):
        return [r["p"] for r in self.queue(**kw)["queue"]]

    def learned(self, title, body, **kw):
        return N.write(self.cfg(), title=title, note_type="doc", folder="Memory/learned", body=body,
                       source="distill 2026-11-09 aaaa1111", expires="2027-02-07", **kw)["p"]

    def cli(self, *args, code=0):
        result = subprocess.run([sys.executable, "-B", str(SCRIPTS / "cli.py"), "--vault", str(self.vault), *args],
                                capture_output=True, text=True, env={**os.environ})
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result.stdout


class TestQueue(Env):
    def test_contradicted_notes_come_first(self):
        rows = self.queue()["queue"]
        self.assertEqual({rows[0]["p"], rows[1]["p"]}, {TEAM_A, TEAM_B})
        self.assertIn("same key as another current note", rows[0]["reasons"])

    def test_learned_note_never_audited_is_queued(self):
        rel = self.learned("Parked charges", "Billing parks a charge after 3 retries.")
        row = next(r for r in self.queue()["queue"] if r["p"] == rel)
        self.assertTrue(row["learned"])
        self.assertIn("learned from a session, never audited", row["reasons"])

    def test_learned_contradiction_flags_the_note_the_person_wrote(self):
        rel = self.learned("Access by chat", "Production access is now granted in chat.\n\n"
                                             "Contradicts [[Request production access]]: check which one holds.")
        rows = {r["p"]: r for r in self.queue()["queue"]}
        self.assertIn(f"contradicted by {rel}", rows[ACCESS]["reasons"])
        self.assertIn(f"contradicts {ACCESS}", rows[rel]["reasons"])

    def test_replaced_and_expired_are_not_queued(self):
        paths = self.paths(n=100)
        self.assertNotIn("Memory/Retry policy v1.md", paths)
        self.assertNotIn("Memory/Temporary shipping provider.md", paths)

    def test_recently_verified_note_is_not_due(self):
        self.assertNotIn("Memory/Retry policy v2.md", self.paths(n=100))

    def test_use_raises_the_priority(self):
        before = self.paths(n=100)
        for _ in range(4):
            TL.record(self.vault, {"ev": "read", "p": ACCESS}, {})
        after = self.paths(n=100)
        self.assertIn(ACCESS, after)
        self.assertTrue(ACCESS not in before or after.index(ACCESS) < before.index(ACCESS))

    def test_deterministic(self):
        self.assertEqual(self.queue(), self.queue())

    def test_cli_json(self):
        out = json.loads(self.cli("review", "--json", "--n", "3"))
        self.assertLessEqual(len(out["queue"]), 3)
        self.assertEqual(set(out["coverage"]), {"notes", "verified_current", "outdated", "unsure", "due"})


class TestRecord(Env):
    def test_confirmed_writes_only_frontmatter_with_a_backup(self):
        before = self.text(ACCESS)
        RV.record(self.cfg(), ACCESS, "confirmed", "ticket PLAT-12 shows the same flow", 9, "stable")
        after = self.text(ACCESS)
        for line in ("verified: 2026-11-10", "confidence: 9", "checked: 2026-11-10",
                     "evidence: ticket PLAT-12 shows the same flow", "volatility: stable"):
            self.assertIn(line, after)
        self.assertEqual(after.split("---", 2)[2], before.split("---", 2)[2])     # body untouched
        backups = list((self.vault / ".vault-memory" / "backups").rglob("Request production access.md"))
        self.assertEqual(len(backups), 1)

    def test_confirmed_needs_strong_evidence(self):
        with self.assertRaisesRegex(N.NoteError, "verdict is unsure"):
            RV.record(self.cfg(), ACCESS, "confirmed", "it looks right", 7)
        with self.assertRaisesRegex(N.NoteError, "evidence is required"):
            RV.record(self.cfg(), ACCESS, "confirmed", "  ", 10)
        with self.assertRaisesRegex(N.NoteError, "1 to 10"):
            RV.record(self.cfg(), ACCESS, "unsure", "x", 11)

    def test_confirmed_learned_note_stops_expiring(self):
        rel = self.learned("Parked charges", "Billing parks a charge after 3 retries.")
        RV.record(self.cfg(), rel, "confirmed", "billing-api config retries=3", 10)
        text = self.text(rel)
        self.assertNotIn("expires:", text)
        self.assertNotIn(rel, self.paths(n=100))

    def test_outdated_is_penalized_and_left_out_of_hook_and_queue(self):
        RV.record(self.cfg(), ACCESS, "outdated", "the platform team now uses a form, not tickets")
        cfg = self.cfg()
        doc = next(d for d in RC.load(cfg)["docs"] if d["p"] == ACCESS)
        self.assertEqual(FR.state(doc, cfg["freshness"]), "outdated")
        self.assertEqual(FR.factor(doc, cfg["freshness"]), 0.5)
        self.assertNotIn(ACCESS, self.paths(n=100))
        self.assertEqual(self.queue()["coverage"]["outdated"], 1)
        report = json.loads(self.cli("doctor", "--json"))["freshness"]
        self.assertEqual(report["outdated"], [ACCESS])
        rows = json.loads(self.cli("recall", "request production access", "--json"))
        self.assertEqual(next(r for r in rows if r["p"] == ACCESS)["state"], "outdated")

    def test_confirming_clears_a_previous_verdict(self):
        RV.record(self.cfg(), ACCESS, "outdated", "first look")
        RV.record(self.cfg(), ACCESS, "confirmed", "second look, at the source", 9)
        text = self.text(ACCESS)
        self.assertNotIn("review:", text)
        self.assertEqual(text.count("evidence:"), 1)

    def test_unsure_waits_the_gap_before_coming_back(self):
        os.environ["VAULT_MEMORY_TODAY"] = "2026-11-10"
        TL.record(self.vault, {"ev": "read", "p": ACCESS}, {})
        self.assertIn(ACCESS, self.paths(n=100))
        RV.record(self.cfg(), ACCESS, "unsure", "no access to the tracker", 5)
        self.assertNotIn("confidence:", self.text(ACCESS))
        self.assertNotIn(ACCESS, self.paths(n=100))
        later = RV.queue(self.cfg(), n=100, today=C.today().fromisoformat("2026-11-20"))["queue"]
        row = next(r for r in later if r["p"] == ACCESS)
        self.assertIn("last check was unsure", row["reasons"])

    def test_cli_record(self):
        out = self.cli("review", ACCESS, "--verdict", "confirmed", "--confidence", "10",
                       "--evidence", "read the runbook in the wiki", "--json")
        self.assertEqual(json.loads(out), {"p": ACCESS, "verdict": "confirmed", "confidence": 10})
        self.cli("review", ACCESS, "--verdict", "confirmed", "--confidence", "6", "--evidence", "x", code=2)


class TestFlag(Env):
    RETRY = "Memory/Retry policy v2.md"

    def test_a_flag_puts_a_current_note_first_without_editing_it(self):
        before = self.text(self.RETRY)
        self.assertNotIn(self.RETRY, self.paths(n=100))
        out = self.cli("review", self.RETRY, "--flag", "the team says retries are now 5, mail ana@example.com", "--json")
        self.assertIn("[REDACTED:email]", json.loads(out)["reason"])
        row = self.queue(n=100)["queue"][0]
        self.assertEqual(row["p"], self.RETRY)
        self.assertIn("disputed in a conversation", row["reasons"][0])
        self.assertEqual(self.text(self.RETRY), before, "a flag never edits the note")

    def test_a_review_after_the_flag_settles_it(self):
        RV.flag(self.cfg(), self.RETRY, "user says it changed")
        RV.record(self.cfg(), self.RETRY, "unsure", "could not reach the source")
        self.assertEqual(RV.open_flags(self.cfg()), {})

    def test_bad_flags_are_refused(self):
        self.cli("review", self.RETRY, "--flag", "  ", code=2)
        self.cli("review", "../outside.md", "--flag", "x", code=2)


class TestVolatility(Env):
    def write(self, volatility):
        return N.write(self.cfg(), title=f"Status {volatility}", note_type="doc", folder="Memory",
                       body=f"Gateway X is enabled for card payments ({volatility}).",
                       verified="2026-10-10", force=True, fields={"volatility": volatility})["p"]

    def test_live_notes_age_in_weeks(self):
        live, stable = self.write("live"), self.write("stable")
        cfg = self.cfg()
        docs = {d["p"]: d for d in RC.load(cfg)["docs"]}
        self.assertEqual(FR.review_limit(docs[live], cfg["freshness"]), 21)
        self.assertEqual(FR.state(docs[live], cfg["freshness"]), "unverified")
        self.assertEqual(FR.state(docs[stable], cfg["freshness"]), "ok")
        paths = self.paths(n=100)
        self.assertIn(live, paths)
        self.assertNotIn(stable, paths)


class TestHookPointer(unittest.TestCase):
    def test_pointer_shows_confidence(self):
        text = HF.pointer([{"p": "Memory/A.md", "type": "doc", "verified": "2026-11-10", "confidence": "9"}])
        self.assertIn("verified 2026-11 9/10", text)


if __name__ == "__main__":
    unittest.main()
