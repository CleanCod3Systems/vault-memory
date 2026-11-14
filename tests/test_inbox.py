"""inbox: list, accept or reject the learned candidates distill left for review, and how each
decision counts as an audit of learned notes (earned autonomy). Invented data only."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPO / "tests"))

import calibration as CB  # noqa: E402
import core as C          # noqa: E402
import distill as DS      # noqa: E402
import inbox as IB        # noqa: E402
import note as N          # noqa: E402
from test_distill import DAY, FakeProvider, Pipeline, candidate  # noqa: E402

SESSION = {"sid": "aaaa1111-0000"}
EVIDENCE = "billing-api config: max_retries=3, then the charge is parked"
LETTERS = "abcdefghijklmnopqrstuvwxyz"


def word(i, salt):
    """A made-up word unique to (i, salt): candidates that share no vocabulary are not duplicates."""
    return "".join(LETTERS[(i * 7 + salt * 5 + k * 3) % 26] for k in range(4)) + LETTERS[salt] + LETTERS[i]


class Env(Pipeline):
    def put(self, cand=None, session=SESSION, index=1):
        """Leave a candidate in the inbox exactly as distill does; its id."""
        cand = cand or candidate(volatility="stable")
        before = set(self.inbox())
        self.assertEqual(DS.write_inbox(self.cfg(), cand, session, DAY, index, False), "kept")
        [new] = set(self.inbox()) - before
        return IB.candidate_id(new)

    def log(self):
        path = self.vault / ".vault-memory" / "reviews.jsonl"
        return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def done(self):
        folder = self.vault / ".vault-memory" / "inbox" / "done"
        return sorted(folder.glob("*.md")) if folder.exists() else []

    def cli(self, *args, code=0):
        env = {k: v for k, v in os.environ.items() if k != "VAULT_MEMORY_VAULT"}
        result = subprocess.run([sys.executable, "-B", str(SCRIPTS / "cli.py"), "--vault", str(self.vault), *args],
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result


class TestList(Env):
    def test_empty_inbox_says_so(self):
        self.assertIn("inbox is empty", self.cli("inbox").stdout)
        self.assertEqual(json.loads(self.cli("inbox", "--json").stdout), {"count": 0, "candidates": []})

    def test_lists_pending_candidates_with_a_stable_short_id(self):
        ident = self.put(session={"sid": "issues:42", "ref": "issues:42", "origin": "issues",
                                  "url": "https://tracker.example/42"})
        data = json.loads(self.cli("inbox", "--json").stdout)
        self.assertEqual(data["count"], 1)
        [row] = data["candidates"]
        self.assertEqual(set(row), {"id", "day", "origin", "source", "title", "type", "quote"})
        self.assertEqual((row["id"], row["day"], row["origin"], row["source"], row["type"]),
                         (ident, DAY, "issues", f"distill {DAY} issues:42", "decision"))
        self.assertEqual(row["title"], "Failed charges are parked after 3 retries")
        self.assertEqual(row["quote"], "we park charges after 3 retries")
        self.assertEqual(len(ident), 8)
        self.assertEqual(IB.listing(self.cfg())["candidates"][0]["id"], ident)      # same file, same id
        text = self.cli("inbox").stdout
        self.assertIn(ident, text)
        self.assertIn("> we park charges after 3 retries", text)

    def test_long_quote_is_one_short_line(self):
        self.put(candidate(evidence="x" * 150 + "\nmore"))
        quote = IB.listing(self.cfg())["candidates"][0]["quote"]
        self.assertLessEqual(len(quote), IB.QUOTE_WIDTH)
        self.assertNotIn("\n", quote)


class TestAccept(Env):
    def test_writes_the_note_as_distill_would_and_records_a_confirmed_review(self):
        ident = self.put(session={"sid": "issues:42", "ref": "issues:42", "origin": "issues",
                                  "url": "https://tracker.example/42"})
        data = json.loads(self.cli("inbox", "--accept", ident, "--evidence", EVIDENCE,
                                   "--confidence", "9", "--json").stdout)
        rel = "Memory/learned/Failed charges are parked after 3 retries.md"
        self.assertEqual(data, {"id": ident, "outcome": "accepted", "p": rel, "verdict": "confirmed",
                                "confidence": 9})
        fm, body = C.frontmatter((self.vault / rel).read_text(encoding="utf-8"))
        self.assertEqual((fm["type"], fm["source"], fm["key"], fm["volatility"], fm["origin"], fm["url"]),
                         ("decision", f"distill {DAY} issues:42", "billing-retry-policy", "stable", "issues",
                          "https://tracker.example/42"))
        self.assertEqual((fm["verified"], fm["confidence"], fm["checked"], fm["evidence"]),
                         ("2026-11-10", "9", "2026-11-10", EVIDENCE))
        self.assertNotIn("expires", fm)                # audited: review.record drops it, as for any learned note
        self.assertIn("Billing-api parks a charge after 3 failed retries.", body)
        [event] = self.log()
        self.assertEqual((event["p"], event["verdict"], event["confidence"], event["learned"]),
                         (rel, "confirmed", 9, True))
        self.assertEqual(self.inbox(), [])
        [settled] = self.done()
        fm, _ = C.frontmatter(settled.read_text(encoding="utf-8"))
        self.assertEqual((fm["outcome"], fm["decided"], fm["note"]), ("accepted", "2026-11-10", rel))
        self.assertIn('"ev": "inbox_accept"', self.telemetry())

    def test_default_confidence_is_the_review_minimum(self):
        ident = self.put()
        self.assertEqual(IB.accept(self.cfg(), ident, EVIDENCE)["confidence"], 8)

    def test_below_eight_is_refused_and_nothing_changes(self):
        ident = self.put()
        before = self.vault_tree()
        result = self.cli("inbox", "--accept", ident, "--evidence", EVIDENCE, "--confidence", "7", code=2)
        self.assertIn("--reject", result.stderr)
        self.assertEqual(self.vault_tree(), before)
        self.assertEqual(self.log(), [])
        self.assertEqual(len(self.inbox()), 1)

    def test_evidence_is_required(self):
        ident = self.put()
        self.cli("inbox", "--accept", ident, code=2)
        self.assertEqual((self.log(), len(self.inbox())), ([], 1))

    def test_duplicate_exits_3_with_the_duplicates_and_logs_nothing(self):
        N.write(self.cfg(), title="Charges parked after three retries", note_type="decision", folder="Memory",
                body="Billing-api parks a charge after 3 failed retries.", key="billing-retry-policy")
        ident = self.put()
        result = self.cli("inbox", "--accept", ident, "--evidence", EVIDENCE, "--json", code=3)
        data = json.loads(result.stdout)
        self.assertEqual(data["duplicates"][0]["p"], "Memory/Charges parked after three retries.md")
        self.assertEqual(self.log(), [])
        self.assertEqual(len(self.inbox()), 1)
        self.assertNotIn("inbox_accept", self.telemetry())
        self.assertFalse((self.vault / "Memory" / "learned").exists())


class TestReject(Env):
    def test_logs_an_outdated_learned_audit_and_writes_no_note(self):
        ident = self.put()
        before = self.vault_tree()
        data = json.loads(self.cli("inbox", "--reject", ident, "--reason",
                                   "the ticket says 5 retries; mail ops@example.com", "--json").stdout)
        self.assertEqual(data, {"id": ident, "outcome": "rejected", "verdict": "outdated"})
        self.assertEqual(self.vault_tree(), before)
        [event] = self.log()
        self.assertEqual((event["p"], event["verdict"], event["learned"], event["inbox"]),
                         (f"inbox:{ident}", "outdated", True, "rejected"))
        self.assertIn("[REDACTED:email]", event["reason"])
        self.assertNotIn("ops@example.com", json.dumps(event))
        self.assertNotIn("parked", json.dumps(event))              # no note text
        self.assertEqual(self.inbox(), [])
        [settled] = self.done()
        self.assertIn("outcome: rejected", settled.read_text(encoding="utf-8"))
        self.assertIn('"ev": "inbox_reject"', self.telemetry())
        learned = CB.precision(CB.read(self.cfg()))
        self.assertEqual((learned["audited"], learned["confirmed"], learned["outdated"]), (1, 0, 1))

    def test_reason_is_required(self):
        ident = self.put()
        self.cli("inbox", "--reject", ident, "--reason", "  ", code=2)
        self.assertEqual((self.log(), len(self.inbox())), ([], 1))

    def test_a_decided_candidate_does_not_come_back_on_a_forced_distill(self):
        self.put()
        IB.reject(self.cfg(), IB.pending(self.cfg())[0]["id"], "does not hold")
        DS.run(self.cfg(), day=DAY, provider=FakeProvider([candidate()]), force=True)
        self.assertEqual(self.inbox(), [])


class TestUnknown(Env):
    def test_unknown_id_exits_2(self):
        self.put()
        for args in (("--accept", "deadbeef", "--evidence", EVIDENCE), ("--reject", "deadbeef", "--reason", "x")):
            result = self.cli("inbox", *args, "--json", code=2)
            self.assertIn("deadbeef", json.loads(result.stdout)["error"])
        self.assertEqual((self.log(), len(self.inbox())), ([], 1))


class TestEarned(Env):
    def candidates(self, n):
        for i in range(n):
            a, b, c = word(i, 1), word(i, 2), word(i, 3)
            DS.write_inbox(self.cfg(), candidate(title=f"{a.capitalize()} {b} {c}", key=f"fact-{a}",
                                                 body=f"{a} {b} {c} {word(i, 4)} {word(i, 5)} {word(i, 6)}."),
                           {"sid": f"{i:04d}cafe"}, DAY, i, False)
        return [c["id"] for c in IB.pending(self.cfg())]

    def test_twenty_accepts_earn_autonomy(self):
        ids = self.candidates(20)
        self.assertFalse(CB.autonomy(self.cfg(auto="earned"))["auto"])
        for ident in ids:
            IB.accept(self.cfg(), ident, EVIDENCE)
        decision = CB.autonomy(self.cfg(auto="earned"))
        self.assertTrue(decision["auto"], decision["reason"])
        result = DS.run(self.cfg(auto="earned"), day=DAY, provider=FakeProvider([candidate()]), force=True)
        self.assertEqual(result["write"]["to"], "notes")

    def test_many_rejects_keep_the_inbox(self):
        ids = self.candidates(20)
        for n, ident in enumerate(ids):
            if n < 12:
                IB.accept(self.cfg(), ident, EVIDENCE)
            else:
                IB.reject(self.cfg(), ident, "the source says otherwise")
        learned = CB.precision(CB.read(self.cfg()))
        self.assertEqual((learned["audited"], learned["confirmed"], learned["outdated"]), (20, 12, 8))
        decision = CB.autonomy(self.cfg(auto="earned"))
        self.assertFalse(decision["auto"], decision["reason"])
        result = DS.run(self.cfg(auto="earned"), day=DAY, provider=FakeProvider([]), force=True)
        self.assertEqual(result["write"]["to"], "inbox")


if __name__ == "__main__":
    unittest.main()
