"""answer: an answer from a conversation kept as a note, unverified, queued for review and
expiring like a learned note, without counting toward learned-note precision."""
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

import answer as AN       # noqa: E402
import calibration as CB  # noqa: E402
import core as C          # noqa: E402
import note as N          # noqa: E402
import review as RV       # noqa: E402
import telemetry as TL    # noqa: E402

TODAY = "2026-11-10"
REFUND = "Atlas/Incidents/Inc - 2026-06-18 - Duplicate refund to customer.md"
QUEUES = "Atlas/Decisions/Decision - 2026-02-10 - Use queues for refunds.md"
QUESTION = "How do we avoid paying a refund twice?"
BODY = "Refunds go through a queue and every refund carries an idempotency key."


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.vault = self.tmp / "vault"
        shutil.copytree(REPO / "tests" / "example_vault", self.vault)
        old = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(old)))
        os.environ["VAULT_MEMORY_TODAY"] = TODAY
        for name in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "VAULT_MEMORY_VAULT"):
            os.environ.pop(name, None)

    def cfg(self):
        return C.load_config(self.vault)

    def save(self, question=QUESTION, body=BODY, cites=(REFUND, QUEUES), **kw):
        return AN.save(self.cfg(), question, body, list(cites), **kw)

    def text(self, rel):
        return (self.vault / rel).read_text(encoding="utf-8")

    def cli(self, *args, code=0):
        result = subprocess.run([sys.executable, "-B", str(SCRIPTS / "cli.py"), "--vault", str(self.vault), *args],
                                capture_output=True, text=True, env={**os.environ})
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result


class TestSave(Env):
    def test_two_cites_write_an_unverified_expiring_note(self):
        result = self.save()
        self.assertEqual(result["p"], "Memory/answers/How do we avoid paying a refund twice.md")
        text = self.text(result["p"])
        fm, body = C.frontmatter(text)
        self.assertEqual(fm["type"], "note")
        self.assertEqual(fm["source"], "ask 2026-11-10")
        self.assertEqual(fm["expires"], "2027-02-08")                       # today + 90 days
        self.assertIn('derived_from: ["[[Inc - 2026-06-18 - Duplicate refund to customer]]", '
                      '"[[Decision - 2026-02-10 - Use queues for refunds]]"]', text)
        self.assertNotIn("verified", fm)
        self.assertNotIn("confidence", fm)
        self.assertIn(f"## Question\n\n{QUESTION}\n\n## Answer\n\n{BODY}\n\n## Sources\n\n"
                      "- [[Inc - 2026-06-18 - Duplicate refund to customer]]\n"
                      "- [[Decision - 2026-02-10 - Use queues for refunds]]", body)

    def test_telemetry_keeps_counts_only(self):
        self.save()
        event = [e for e in TL.read(self.vault, days=1) if e.get("ev") == "answer"][-1]
        self.assertEqual((event["cites"], event["answers_cited"]), (2, 0))
        self.assertNotIn("p", event)
        self.assertNotIn("refund", json.dumps(event).lower())

    def test_one_cite_is_not_enough(self):
        with self.assertRaises(N.NoteError) as raised:
            self.save(cites=[REFUND])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("at least 2", raised.exception.message)
        self.cli("answer", "--question", QUESTION, "--body", BODY, "--cites", REFUND, code=2)
        self.assertFalse((self.vault / "Memory" / "answers").exists())

    def test_missing_archived_or_replaced_cites_are_rejected(self):
        for bad, why in (("Memory/Does not exist.md", "not a note of this vault"),
                         ("_archive/Old manual refunds process.md", "archived"),
                         ("Memory/Retry policy v1.md", "replaced")):
            with self.assertRaises(N.NoteError) as raised:
                self.save(cites=[REFUND, QUEUES, bad])
            self.assertEqual(raised.exception.code, 2)
            self.assertIn(why, raised.exception.message)

    def test_cites_without_extension_and_repeated_count_once(self):
        result = self.save(cites=[REFUND[:-3], REFUND, QUEUES])
        self.assertEqual(result["cites"], [REFUND, QUEUES])

    def test_an_answer_may_cite_an_answer_but_not_only_answers(self):
        first = self.save()["p"]
        second = self.save(question="Who owns the payment retries after an outage?",
                           body="The billing team owns retries; after an outage they replay the "
                                "pending jobs by hand and check each customer account.",
                           cites=[first, QUEUES])
        self.assertIn(first, second["cites"])
        with self.assertRaises(N.NoteError) as raised:
            self.save(question="What do the saved answers say?", body="Only answers here.",
                      cites=[first, second["p"]])
        self.assertEqual(raised.exception.code, 2)

    def test_secrets_are_redacted(self):
        token = "sk-" + "0" * 24
        rel = self.save(body=BODY + f" The test key was {token}.")["p"]
        text = self.text(rel)
        self.assertNotIn(token, text)
        self.assertIn("[REDACTED:api-key]", text)

    def test_duplicate_exits_3(self):
        self.save()
        result = self.cli("answer", "--question", QUESTION, "--body", BODY,
                          "--cites", f"{REFUND},{QUEUES}", "--json", code=3)
        self.assertTrue(json.loads(result.stdout)["duplicates"])
        self.cli("answer", "--question", QUESTION, "--title", "Refunds paid once", "--body", BODY,
                 "--cites", f"{REFUND},{QUEUES}", code=3)                     # same content

    def test_short_answers_from_the_same_notes_are_not_duplicates(self):
        # the Sources list repeats the cited names: it must not make two answers look alike
        self.save(question="Who owns refunds?", body="The payments team.")
        self.save(question="When did refunds move to a queue?", body="In February 2026.")

    def test_dry_run_writes_nothing(self):
        result = self.cli("answer", "--question", QUESTION, "--body", BODY,
                          "--cites", f"{REFUND},{QUEUES}", "--dry-run")
        self.assertIn("## Sources", result.stdout)
        self.assertFalse((self.vault / "Memory" / "answers").exists())


class TestReview(Env):
    def test_a_fresh_answer_is_queued(self):
        rel = self.save()["p"]
        row = next(r for r in RV.queue(self.cfg(), n=100)["queue"] if r["p"] == rel)
        self.assertIn("saved from a conversation, never audited", row["reasons"])
        self.assertTrue(row["answer"])
        self.assertFalse(row["learned"])

    def test_reviewing_an_answer_does_not_touch_learned_precision(self):
        before = CB.precision(CB.read(self.cfg()))
        rel = self.save()["p"]
        RV.record(self.cfg(), rel, "outdated", "the decision note was replaced last week")
        [line] = CB.read(self.cfg())
        self.assertFalse(line["learned"])
        self.assertTrue(line["answer"])
        self.assertEqual(CB.precision(CB.read(self.cfg())), before)
        self.assertEqual(CB.report(self.cfg())["learned"]["audited"], 0)

    def test_a_confirmed_answer_stops_expiring(self):
        rel = self.save()["p"]
        RV.record(self.cfg(), rel, "confirmed", "incident and decision notes say the same", 9)
        fm, _ = C.frontmatter(self.text(rel))
        self.assertNotIn("expires", fm)
        self.assertEqual(fm["confidence"], "9")


class TestExpiry(Env):
    def test_an_expired_unread_answer_is_archived(self):
        rel = self.save()["p"]
        os.environ["VAULT_MEMORY_TODAY"] = "2027-03-01"
        self.assertEqual(AN.upkeep(self.cfg()), (0, 1))
        self.assertFalse((self.vault / rel).exists())
        self.assertTrue((self.vault / "_archive" / rel).exists())

    def test_reading_an_answer_renews_it(self):
        rel = self.save()["p"]
        os.environ["VAULT_MEMORY_TODAY"] = "2026-12-01"
        TL.record(self.vault, {"ev": "read", "p": rel}, {})
        self.assertEqual(AN.upkeep(self.cfg()), (1, 0))
        fm, _ = C.frontmatter(self.text(rel))
        self.assertEqual(fm["expires"], "2027-03-01")                       # 2026-12-01 + 90 days
        self.assertNotIn("verified", fm)

    def test_learned_notes_are_left_alone(self):
        learned = N.write(self.cfg(), title="Parked charges", note_type="doc", folder="Memory/learned",
                          body="Billing parks a charge after 3 retries.",
                          source="distill 2026-08-01 aaaa1111", expires="2026-11-01")["p"]
        self.assertEqual(AN.upkeep(self.cfg()), (0, 0))
        self.assertTrue((self.vault / learned).exists())


if __name__ == "__main__":
    unittest.main()
