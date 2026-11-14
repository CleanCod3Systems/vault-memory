"""Update proposals: when a session or ingested material adds to a note that already exists, the
change waits in the inbox and is applied only when a person accepts it. Invented data only; the
provider is a fake callable (no model, no network)."""
import hashlib
import json
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "tests"))

import calibration as CB  # noqa: E402
import core as C          # noqa: E402
import distill as DS      # noqa: E402
import inbox as IB        # noqa: E402
import ingest as IG       # noqa: E402
import note as N          # noqa: E402
import sources as SR      # noqa: E402
from test_distill import DAY, FakeProvider, candidate  # noqa: E402
from test_inbox import EVIDENCE, Env  # noqa: E402
from test_sources import THREAD  # noqa: E402

TARGET = "Docs/Billing retry runbook.md"
ORIGINAL = "After the third failed attempt the charge is parked."
ADDS = {"match": 1, "relation": "adds"}
APPEND = {"operation": "append", "text": "Parked charges are retried by hand on Mondays.", "old": ""}
REPLACE = {"operation": "replace", "old": "third failed attempt", "text": "fifth failed attempt"}


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Proposals(Env):
    def setUp(self):
        super().setUp()
        N.write(self.cfg(), title="Billing retry runbook", note_type="doc", folder="Docs", body=ORIGINAL,
                verified="today")

    def note(self, rel=TARGET):
        return (self.vault / rel).read_text(encoding="utf-8")

    def propose(self, update=APPEND, relation=ADDS, cands=None, cfg=None, **distill):
        cfg = cfg or self.cfg(**distill)
        provider = FakeProvider(cands or [candidate()], relation=relation, update=update)
        return DS.run(cfg, day=DAY, provider=provider, force=True), provider

    def proposal_ids(self):
        return [c["id"] for c in IB.pending(self.cfg()) if c.get("kind") == "update"]


class TestPropose(Proposals):
    def test_the_proposal_waits_in_the_inbox_and_the_note_is_untouched(self):
        before, tree = self.note(), self.vault_tree()
        result, _ = self.propose()
        self.assertEqual((result["kept"], result["update_proposals"], result["discarded"]), (0, 1, 0))
        self.assertEqual(self.note(), before)
        self.assertEqual(self.vault_tree(), tree)
        [file] = self.inbox()
        fm, _ = C.raw_frontmatter(file.read_text(encoding="utf-8"))
        self.assertEqual((fm["proposal"], fm["target"], fm["operation"], fm["target_hash"]),
                         ("update", TARGET, "append", sha(before)))
        self.assertEqual(fm["source"], f"distill {DAY} aaaa1111")
        [cand] = IB.pending(self.cfg())
        self.assertEqual((cand["kind"], cand["text"], cand["quote"]),
                         ("update", APPEND["text"], "we park charges after 3 retries"))

    def test_listing_shows_the_target_and_the_diff(self):
        self.propose()
        [row] = IB.listing(self.cfg())["candidates"]
        self.assertEqual((row["kind"], row["target"], row["operation"], row["stale"]),
                         ("update", TARGET, "append", False))
        self.assertIn("+" + APPEND["text"], row["diff"].splitlines())
        text = self.cli("inbox").stdout
        self.assertIn(f"update → {TARGET}", text)
        self.assertIn("+" + APPEND["text"], text)

    def test_replace_needs_the_exact_fragment_once_or_it_becomes_a_plain_candidate(self):
        for old in ("never said in the note", "the"):
            result, _ = self.propose(update={**REPLACE, "old": old})
            self.assertEqual((result["update_proposals"], result["kept"]), (0, 1), old)
            for path in self.inbox():
                path.unlink()

    def test_an_edit_never_touches_or_creates_a_frontmatter(self):
        note = "---\nkey: third\n---\n\nThe body.\n"
        self.assertIsNone(DS.apply_update(note, "replace", "fifth", "third"))       # only in the frontmatter
        self.assertEqual(DS.apply_update(note, "replace", "A body", "The body"), "---\nkey: third\n---\n\nA body.\n")
        self.assertIsNone(DS.apply_update("The body.\n", "replace", "---\nverified: today\n---\nX", "The body."))

    def test_sensitive_or_empty_text_becomes_a_plain_candidate(self):
        for update in ({**APPEND, "text": "Mail ops@example.com on Mondays."}, {**APPEND, "text": "  "},
                       {"operation": "rewrite", "text": "x", "old": ""}, {}):
            result, _ = self.propose(update=update)
            self.assertEqual((result["update_proposals"], result["kept"]), (0, 1), update)
            for path in self.inbox():
                path.unlink()

    def test_the_note_text_sent_to_the_model_is_redacted(self):
        N.write(self.cfg(), update=TARGET, body=ORIGINAL + " Escalate to ops@example.com.")
        _, provider = self.propose()
        edit = next(p for p in provider.prompts if TARGET.split("/")[-1][:-3] in p and "operation" in p)
        self.assertNotIn("ops@example.com", edit)
        self.assertIn("[REDACTED:email]", edit)

    def test_same_fact_is_already_known_in_inbox_mode_too(self):
        result, _ = self.propose(relation={"match": 1, "relation": "same"})
        self.assertEqual((result["kept"], result["discarded_reasons"]), (0, {"already_known": 1}))
        self.assertEqual(self.inbox(), [])

    def test_dry_run_writes_no_proposal(self):
        provider = FakeProvider([candidate()], relation=ADDS, update=APPEND)
        result = DS.run(self.cfg(), day=DAY, provider=provider, dry_run=True)
        self.assertEqual(result["update_proposals"], 1)
        self.assertEqual(self.inbox(), [])

    def test_proposing_twice_is_idempotent(self):
        self.propose()
        result, _ = self.propose()
        self.assertEqual(result["discarded_reasons"], {"already_in_inbox": 1})
        self.assertEqual(len(self.inbox()), 1)


class TestNeverAutomatic(Proposals):
    def test_auto_true_still_leaves_the_proposal_in_the_inbox(self):
        before = self.note()
        result, _ = self.propose(auto=True)
        self.assertEqual((result["update_proposals"], result["kept"], result["updated"]), (1, 0, 0))
        self.assertEqual(self.note(), before)
        self.assertEqual(len(self.proposal_ids()), 1)
        self.assertFalse((self.vault / "Memory" / "learned").exists())

    def test_earned_autonomy_still_leaves_the_proposal_in_the_inbox(self):
        before = self.note()
        earned = {"mode": "earned", "auto": True, "reason": "precision 95% on 40 audits"}
        with mock.patch.object(DS.CB, "autonomy", return_value=earned):
            result, _ = self.propose(auto="earned")
        self.assertEqual((result["write"]["to"], result["update_proposals"]), ("notes", 1))
        self.assertEqual(self.note(), before)

    def test_a_learned_note_in_auto_mode_keeps_the_learned_append(self):
        rel = N.write(self.cfg(), title="Charges park after three failures", note_type="decision",
                      folder="Memory/learned", body=ORIGINAL + " Learned.", verified="today")["p"]
        found = ("adds", {"p": rel, "n": "Charges park after three failures", "t": ""})
        with mock.patch.object(DS, "relate", return_value=found):
            result, _ = self.propose(auto=True, cands=[candidate(body="Parked charges are retried on Mondays.")])
        self.assertEqual((result["update_proposals"], result["updated"]), (0, 1))
        self.assertIn("retried on Mondays", self.note(rel))
        self.assertEqual(self.inbox(), [])

    def test_a_learned_note_in_inbox_mode_gets_a_proposal(self):
        rel = N.write(self.cfg(), title="Charges park after three failures", note_type="decision",
                      folder="Memory/learned", body=ORIGINAL + " Learned.", verified="today")["p"]
        before = self.note(rel)
        found = ("adds", {"p": rel, "n": "Charges park after three failures", "t": ""})
        with mock.patch.object(DS, "relate", return_value=found):
            result, _ = self.propose()
        self.assertEqual((result["update_proposals"], self.note(rel)), (1, before))


class TestLimit(Proposals):
    def two(self):
        return [candidate(), candidate(title="Parked charge retries by hand", key="parked-manual",
                                       body="Parked charges after 3 retries are retried by hand.")]

    def test_max_updates_caps_the_proposals_of_one_source(self):
        cfg = self.cfg()
        cfg["ingest"] = {"max_updates": 1}
        result, _ = self.propose(cands=self.two(), cfg=cfg)
        self.assertEqual((result["update_proposals"], result["kept"]), (1, 1))

    def test_zero_turns_proposals_off(self):
        cfg = self.cfg()
        cfg["ingest"] = {"max_updates": 0}
        result, provider = self.propose(cfg=cfg)
        self.assertEqual((result["update_proposals"], result["kept"]), (0, 1))
        self.assertFalse(any("Propose the smallest edit" in p for p in provider.prompts))

    def test_default_is_ten(self):
        self.assertEqual(DS.max_updates(self.cfg()), 10)

    def test_the_limit_counts_per_ingested_item(self):
        cfg = self.cfg()
        cfg["ingest"] = {"max_updates": 1}
        provider = FakeProvider(self.two(), relation=ADDS, update=APPEND)
        result = IG.run(cfg, SR.material(json.dumps(THREAD)), provider=provider)
        self.assertEqual((result["update_proposals"], result["kept"]), (1, 1))
        [proposal] = [c for c in IB.pending(cfg) if c.get("kind") == "update"]
        self.assertEqual((proposal["origin"], proposal["url"]), ("chat", THREAD["url"]))


class TestCaptures(Proposals):
    def test_a_capture_is_never_a_target(self):
        capture = self.vault / "Captures" / "2026-11-09 Thread.md"
        capture.parent.mkdir(parents=True)
        capture.write_text("---\ntype: reference\narea: general\n---\n\n" + ORIGINAL + "\n", encoding="utf-8")
        found = ("adds", {"p": "Captures/2026-11-09 Thread.md", "n": "2026-11-09 Thread", "t": "Thread"})
        with mock.patch.object(DS, "relate", return_value=found):
            result, _ = self.propose()
        self.assertEqual((result["update_proposals"], result["kept"]), (0, 1))


class Proposed(Proposals):
    def setUp(self):
        super().setUp()
        self.propose()
        [self.ident] = self.proposal_ids()


class TestAccept(Proposed):
    def test_applies_with_a_backup_and_records_a_confirmed_audit(self):
        before = self.note()
        data = json.loads(self.cli("inbox", "--accept", self.ident, "--evidence", EVIDENCE,
                                   "--confidence", "9", "--json").stdout)
        self.assertEqual({k: data[k] for k in ("id", "outcome", "kind", "p", "verdict", "confidence")},
                         {"id": self.ident, "outcome": "accepted", "kind": "update", "p": TARGET,
                          "verdict": "confirmed", "confidence": 9})
        after = self.note()
        self.assertTrue(after.startswith(before.rstrip()))
        self.assertTrue(after.rstrip().endswith(APPEND["text"]))
        self.assertEqual((self.vault / data["backup"] / TARGET).read_text(encoding="utf-8"), before)
        fm, _ = C.frontmatter(after)
        self.assertNotIn("confidence", fm)            # the paragraph was audited, not the whole note
        [event] = self.log()
        self.assertEqual((event["p"], event["verdict"], event["confidence"], event["learned"], event["inbox"],
                          event["proposal"], event["target"]),
                         (f"inbox:{self.ident}", "confirmed", 9, True, "accepted", "update", TARGET))
        learned = CB.precision(CB.read(self.cfg()))
        self.assertEqual((learned["audited"], learned["confirmed"]), (1, 1))
        self.assertEqual(self.inbox(), [])
        [settled] = self.done()
        fm, _ = C.raw_frontmatter(settled.read_text(encoding="utf-8"))
        self.assertEqual((fm["outcome"], fm["note"], fm["evidence"]), ("accepted", TARGET, EVIDENCE))
        self.assertIn('"ev": "inbox_accept"', self.telemetry())

    def test_replace_swaps_the_exact_fragment(self):
        IB.reject(self.cfg(), self.ident, "redo as a replace")
        self.propose(update=REPLACE, cands=[candidate(title="Charges parked after five retries")])
        [ident] = self.proposal_ids()
        IB.accept(self.cfg(), ident, EVIDENCE)
        self.assertIn("After the fifth failed attempt the charge is parked.", self.note())
        self.assertNotIn("third", self.note())

    def test_a_note_changed_since_the_proposal_is_a_conflict(self):
        changed = self.note() + "\nEdited by hand.\n"
        (self.vault / TARGET).write_text(changed, encoding="utf-8")
        result = self.cli("inbox", "--accept", self.ident, "--evidence", EVIDENCE, code=4)
        self.assertIn("changed since", result.stderr)
        self.assertEqual(self.note(), changed)
        self.assertEqual(self.log(), [])
        self.assertEqual(self.proposal_ids(), [self.ident])
        [row] = IB.listing(self.cfg())["candidates"]
        self.assertTrue(row["stale"])

    def test_evidence_and_confidence_are_required_as_for_new_notes(self):
        before = self.note()
        self.cli("inbox", "--accept", self.ident, code=2)
        self.cli("inbox", "--accept", self.ident, "--evidence", EVIDENCE, "--confidence", "7", code=2)
        self.assertEqual((self.note(), self.log(), self.proposal_ids()), (before, [], [self.ident]))

    def test_accepting_twice_does_not_apply_twice(self):
        IB.accept(self.cfg(), self.ident, EVIDENCE)
        once = self.note()
        self.cli("inbox", "--accept", self.ident, "--evidence", EVIDENCE, code=2)
        self.assertEqual(self.note(), once)
        self.assertEqual(once.count(APPEND["text"]), 1)


class TestReject(Proposed):
    def test_writes_nothing_and_records_an_outdated_audit(self):
        before, tree = self.note(), self.vault_tree()
        data = json.loads(self.cli("inbox", "--reject", self.ident, "--reason", "not what the runbook says",
                                   "--json").stdout)
        self.assertEqual(data, {"id": self.ident, "outcome": "rejected", "verdict": "outdated", "kind": "update"})
        self.assertEqual((self.note(), self.vault_tree()), (before, tree))
        [event] = self.log()
        self.assertEqual((event["p"], event["verdict"], event["learned"], event["proposal"], event["target"]),
                         (f"inbox:{self.ident}", "outdated", True, "update", TARGET))
        self.assertFalse((self.vault / ".vault-memory" / "backups").exists())


class TestUnsafeTargets(Proposed):
    """A proposal is a file anyone can edit: its target is checked again when it is accepted."""

    def retarget(self, target):
        [path] = self.inbox()
        text = re.sub(r"(?m)^target: .*$", lambda _: f"target: {target}", path.read_text(encoding="utf-8"))
        path.write_text(text, encoding="utf-8")

    def test_paths_outside_the_vault_or_its_state_are_refused(self):
        outside = self.vault.parent / "outside.md"
        outside.write_text(self.note(), encoding="utf-8")
        for target in ("../outside.md", str(outside), ".vault-memory/config.json", "Docs/missing.md"):
            self.retarget(target)
            result = self.cli("inbox", "--accept", self.ident, "--evidence", EVIDENCE, code=2)
            self.assertIn("not a note of this vault", result.stderr, target)
        self.assertEqual(outside.read_text(encoding="utf-8"), self.note())
        self.assertEqual(self.log(), [])

    def test_a_capture_is_refused(self):
        capture = self.vault / "Captures" / "Thread.md"
        capture.parent.mkdir(parents=True)
        capture.write_text(self.note(), encoding="utf-8")
        self.retarget("Captures/Thread.md")
        result = self.cli("inbox", "--accept", self.ident, "--evidence", EVIDENCE, code=2)
        self.assertIn("capture", result.stderr)
        self.assertEqual(capture.read_text(encoding="utf-8"), self.note())


class TestOldCandidates(Proposed):
    def test_new_note_candidates_keep_their_shape_next_to_proposals(self):
        self.put()
        rows = IB.listing(self.cfg())["candidates"]
        plain = next(r for r in rows if r["id"] != self.ident)
        self.assertEqual(set(plain), {"id", "day", "origin", "source", "title", "type", "quote"})
        result = IB.accept(self.cfg(), plain["id"], EVIDENCE)
        self.assertEqual(result["p"], "Memory/learned/Failed charges are parked after 3 retries.md")


if __name__ == "__main__":
    unittest.main()
