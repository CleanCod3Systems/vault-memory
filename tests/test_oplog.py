"""Operations log (`log.md`): a derived view of reviews.jsonl and the telemetry. Invented data only."""
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))

import bench as BN      # noqa: E402
import core as C        # noqa: E402
import indexer as IX    # noqa: E402
import oplog as OL      # noqa: E402
import recall as RC     # noqa: E402

TODAY = dt.date(2026, 11, 10)
HEADING = re.compile(r"^## \[(\d{4}-\d{2}-\d{2})\] ([a-z-]+) \| (.+)$")


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.vault = self.tmp / "vault"
        shutil.copytree(REPO / "tests" / "example_vault", self.vault)
        old = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(old)))
        os.environ["VAULT_MEMORY_TODAY"] = TODAY.isoformat()
        os.environ.pop("VAULT_MEMORY_VAULT", None)
        self.state = self.vault / ".vault-memory"

    def cfg(self, **log):
        cfg = C.load_config(self.vault)
        if log:
            cfg["log"] = log
        return cfg

    def append(self, name, *events):
        with (self.state / name).open("a", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(event) + "\n")

    def review(self, day, rel, verdict="confirmed", confidence=9, **extra):
        self.append("reviews.jsonl", {"t": f"{day}T10:00:00", "day": day, "p": rel, "verdict": verdict,
                                      "confidence": confidence, **extra})

    def event(self, t, ev, **fields):
        self.append("telemetry.jsonl", {"t": t, "sid": "s1", "ev": ev, **fields})

    def text(self, **log):
        cfg = self.cfg(path="log.md", **log)
        OL.write(cfg, TODAY)
        return (self.vault / "log.md").read_text(encoding="utf-8")

    def headings(self, text):
        return [m.groups() for m in map(HEADING.match, text.splitlines()) if m]


class TestOff(Env):
    def test_off_by_default_writes_nothing(self):
        self.review("2026-11-09", "Memory/A.md")
        self.assertIsNone(OL.write(self.cfg(), TODAY))
        self.assertFalse((self.vault / "log.md").exists())

    def test_refresh_without_log_path_writes_nothing(self):
        subprocess.run([sys.executable, "-B", str(SCRIPTS / "cli.py"), "--vault", str(self.vault), "refresh"],
                       capture_output=True, text=True, check=True)
        self.assertFalse((self.vault / "log.md").exists())

    def test_refresh_regenerates_the_log_when_on(self):
        cfg = C.load_config(self.vault)
        cfg["log"] = {"path": "Memory/log.md"}
        C.save_config(self.vault, cfg)
        self.review("2026-11-09", "Memory/A.md", confidence=8)
        result = subprocess.run([sys.executable, "-B", str(SCRIPTS / "cli.py"), "--vault", str(self.vault),
                                 "refresh"], capture_output=True, text=True, check=True,
                                env={**os.environ})
        text = (self.vault / "Memory" / "log.md").read_text(encoding="utf-8")
        self.assertIn("## [2026-11-09] review | A", text)
        self.assertIn("log.md", result.stdout)

    def test_path_outside_the_vault_is_refused(self):
        with self.assertRaises(ValueError):
            OL.write(self.cfg(path="../log.md"), TODAY)
        with self.assertRaises(ValueError):
            OL.write(self.cfg(path="log.txt"), TODAY)


class TestFormat(Env):
    def test_entry_is_a_heading_and_one_detail_line(self):
        self.review("2026-11-09", "Memory/Retry policy.md", "confirmed", 9)
        lines = self.text().splitlines()
        i = lines.index("## [2026-11-09] review | Retry policy")
        self.assertEqual(lines[i + 1], "confirmed · confidence 9 · Memory/Retry policy.md")
        self.assertTrue(self.text().endswith("Retry policy.md\n"))

    def test_grep_lists_only_entries(self):
        self.review("2026-11-09", "Memory/A.md")
        self.event("2026-11-08T09:00:00", "note", p="Memory/B.md", mode="create")
        text = self.text()
        grepped = [x for x in text.splitlines() if x.startswith("## [")]
        self.assertEqual(len(grepped), 2)
        self.assertTrue(all(HEADING.match(x) for x in grepped))

    def test_marked_generated_and_typed_index(self):
        text = self.text()
        front, _, _ = text[4:].partition("\n---")
        self.assertIn("type: index", front)
        self.assertIn("generated: true", front)
        self.assertIn("Do not edit by hand", text)
        self.assertIn("No operations in the window.", text)

    def test_each_operation_has_its_own_op_and_detail(self):
        self.review("2026-11-09", "Memory/A.md", "unsure", None)
        self.review("2026-11-09", "inbox:ab12cd34", "outdated", None, learned=True, inbox="rejected",
                    reason="the source says 5 retries")
        self.event("2026-11-08T09:00:00", "note", p="Memory/B.md", mode="update")
        self.event("2026-11-08T09:01:00", "flag", p="Memory/B.md", origin="mcp")
        self.event("2026-11-08T09:02:00", "distill", day="2026-11-07", sessions=2, proposed=3, kept=2,
                   updated=0, replaced=0, discarded=1, write="inbox", renewed=0, archived=0)
        self.event("2026-11-08T09:03:00", "ingest", day="2026-11-08", origin="issues", sessions=1,
                   proposed=1, kept=1, updated=0, replaced=0, discarded=0)
        self.event("2026-11-08T09:04:00", "answer", cites=3, answers_cited=0, origin="cli")
        self.event("2026-11-08T09:05:00", "capture", created=True)
        self.event("2026-11-08T09:06:00", "inbox_accept", n=1, pending=4)    # the review line names it
        self.review("2026-11-08", "Memory/C.md", "confirmed", 9, learned=True, inbox="accepted")
        self.event("2026-11-08T09:07:00", "distill_upkeep", renewed=2, archived=1)
        text = self.text()
        ops = sorted(op for _, op, _ in self.headings(text))
        self.assertEqual(ops, ["answer", "capture", "distill", "flag", "inbox-accept", "inbox-reject",
                               "ingest", "note", "review", "upkeep"])
        self.assertIn("rejected: the source says 5 retries", text)
        self.assertIn("update · Memory/B.md", text)
        self.assertIn("sessions 2 · proposed 3 · kept 2 · discarded 1 · to inbox", text)
        self.assertIn("## [2026-11-08] ingest | issues", text)
        self.assertIn("renewed 2 · archived 1", text)
        self.assertIn("## [2026-11-08] inbox-accept | C\nconfirmed · confidence 9 · Memory/C.md", text)
        self.assertEqual(sum(op == "inbox-accept" for _, op, _ in self.headings(text)), 1)

    def test_an_update_proposal_names_its_target_whether_accepted_or_rejected(self):
        self.review("2026-11-09", "inbox:aa11", "confirmed", 9, learned=True, inbox="accepted",
                    proposal="update", target="Memory/Retry policy.md")
        self.review("2026-11-09", "inbox:bb22", "outdated", None, learned=True, inbox="rejected",
                    proposal="update", target="Memory/Retry policy.md", reason="already fixed")
        text = self.text()
        self.assertIn("inbox-accept | Retry policy\nconfirmed · confidence 9 · Memory/Retry policy.md", text)
        self.assertIn("inbox-reject | Retry policy\nrejected: already fixed", text)

    def test_a_distill_run_with_only_update_proposals_is_logged(self):
        self.event("2026-11-08T09:02:00", "distill", day="2026-11-07", sessions=1, proposed=0,
                   update_proposals=2, write="inbox")
        self.assertIn("update_proposals 2", self.text())

    def test_reads_recalls_and_hooks_are_not_operations(self):
        for ev, fields in (("read", {"p": "Memory/A.md"}), ("recall", {"q": "billing", "n": 2}),
                           ("hook", {"q": "billing", "emitted": ["Memory/A.md"]}), ("hook_silent", {"q": "x"}),
                           ("stop", {}), ("review", {"p": "Memory/A.md", "verdict": "confirmed"}),
                           ("distill", {"day": "2026-11-07", "proposed": 0, "kept": 0}),
                           ("capture", {"created": False})):
            self.event("2026-11-09T09:00:00", ev, **fields)
        self.assertEqual(self.headings(self.text()), [])


class TestWindowAndOrder(Env):
    def test_newest_first_with_a_total_order_on_ties(self):
        self.review("2026-11-05", "Memory/Old.md")
        self.review("2026-11-09", "Memory/New.md")
        self.event("2026-11-09T18:00:00", "note", p="Memory/Late.md", mode="create")
        self.review("2026-11-09", "Memory/Zeta.md")
        days = [(d, t) for d, _, t in self.headings(self.text())]
        self.assertEqual(days, [("2026-11-09", "Late"), ("2026-11-09", "Zeta"), ("2026-11-09", "New"),
                                ("2026-11-05", "Old")])

    def test_log_days_is_the_window(self):
        self.review("2026-10-01", "Memory/Old.md")           # 40 days before
        self.review("2026-10-25", "Memory/Recent.md")        # 16 days before
        self.review("2026-11-10", "Memory/Today.md")
        self.assertEqual([t for _, _, t in self.headings(self.text())], ["Today", "Recent"])
        self.assertEqual([t for _, _, t in self.headings(self.text(days=7))], ["Today"])
        self.assertEqual([t for _, _, t in self.headings(self.text(days=60))], ["Today", "Recent", "Old"])

    def test_future_dates_are_left_out(self):
        self.review("2026-12-31", "Memory/Future.md")
        self.assertEqual(self.headings(self.text()), [])


class TestRedactionAndStability(Env):
    def test_every_line_is_redacted(self):
        self.review("2026-11-09", "inbox:ab12cd34", "outdated", None, reason="mail jane@example.com, key sk-abcdefghijklmnopqrstuv1234")
        text = self.text()
        self.assertNotIn("jane@example.com", text)
        self.assertNotIn("sk-abcdefghijklmnopqrstuv1234", text)
        self.assertIn("[REDACTED:email]", text)

    def test_same_inputs_give_the_same_bytes_and_no_rewrite(self):
        self.review("2026-11-09", "Memory/A.md")
        self.event("2026-11-08T09:00:00", "note", p="Memory/B.md", mode="create")
        first = self.text()
        path = self.vault / "log.md"
        os.utime(path, (1_000_000_000, 1_000_000_000))
        cfg = self.cfg(path="log.md")
        _, count, written = OL.write(cfg, TODAY)
        self.assertEqual((count, written), (2, False))
        self.assertEqual(path.read_text(encoding="utf-8"), first)
        self.assertEqual(path.stat().st_mtime, 1_000_000_000)
        self.review("2026-11-10", "Memory/C.md")
        self.assertTrue(OL.write(cfg, TODAY)[2])
        self.assertEqual(self.text().count("\n## ["), 3)

    def test_telemetry_and_review_files_are_not_touched(self):
        self.review("2026-11-09", "Memory/A.md")
        before = (self.state / "reviews.jsonl").read_bytes()
        self.text()
        self.assertEqual((self.state / "reviews.jsonl").read_bytes(), before)
        self.assertFalse((self.state / "telemetry.jsonl").exists())


class TestOutOfRecall(Env):
    def test_indexed_as_index_and_left_out_of_recall_and_bench(self):
        self.review("2026-11-09", "Memory/Zebrafish rollout.md")
        self.text()
        cfg = self.cfg(path="log.md")
        IX.build(cfg, quiet=True)
        idx = json.loads((self.state / "index.json").read_text(encoding="utf-8"))
        [doc] = [d for d in idx["docs"] if d["p"] == "log.md"]
        self.assertEqual(doc["type"], "index")
        self.assertNotIn("log.md", [d["p"] for _, d in RC.search(idx, "zebrafish rollout review confirmed")])
        self.assertNotIn("log.md", BN.answer_paths(cfg, idx))
        cfg["recall"]["similar_skip_types"] = []             # a user override must not bring it back
        self.assertNotIn("log.md", BN.answer_paths(cfg, idx))
        self.assertTrue(doc["generated"])                    # same marker as the catalog
        self.assertEqual(RC.search(idx, "zebrafish", note_type="index"), [])

    def test_a_log_written_by_hand_is_not_overwritten(self):
        (self.vault / "log.md").write_text("# My own log\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            OL.write(self.cfg(path="log.md"))
        self.assertEqual((self.vault / "log.md").read_text(encoding="utf-8"), "# My own log\n")


if __name__ == "__main__":
    unittest.main()
