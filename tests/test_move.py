"""move: a plan of `old<TAB>new` lines moves and renames notes, folders and attachments, and
rewrites every link, config key and state line that pointed at the old paths. Invented data only."""
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
import move as MV         # noqa: E402

TODAY = "2026-11-14"
V1 = "Memory/Retry policy v1.md"
V2 = "Memory/Retry policy v2.md"


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

    def put(self, rel, text):
        path = self.vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(text, encoding="utf-8")

    def text(self, rel):
        return (self.vault / rel).read_text(encoding="utf-8")

    def move(self, *lines, apply=True):
        return MV.run(self.cfg(), "\n".join(lines) + "\n", apply=apply)

    def refused(self, *lines):
        with self.assertRaises(MV.MoveError) as ctx:
            self.move(*lines)
        self.assertEqual(ctx.exception.code, 2)
        return "\n".join(ctx.exception.errors)

    def snapshot(self, skip_backups=False):
        out = {}
        for root, _, files in os.walk(self.vault):
            for name in files:
                path = Path(root) / name
                rel = path.relative_to(self.vault).as_posix()
                if skip_backups and "/backups/" in "/" + rel:
                    continue
                out[rel] = path.read_bytes()
        return out


class TestPlan(Env):
    def test_origin_that_does_not_exist_is_refused(self):
        self.assertIn("does not exist", self.refused("Memory/Nope.md\tWiki/Nope.md"))

    def test_origin_with_another_letter_case_is_refused(self):
        self.assertIn("does not exist", self.refused("memory/retry policy v1.md\tWiki/x.md"))

    def test_destination_that_exists_is_refused(self):
        self.assertIn("already exists", self.refused(f"Home.md\t{V2}"))

    def test_two_files_landing_on_one_path_are_refused(self):
        errors = self.refused(f"{V1}\tWiki/Retry.md", f"{V2}\tWiki/Retry.md")
        self.assertIn("Wiki/Retry.md", errors)
        self.assertIn("same path", errors)

    def test_merging_folders_that_collide_is_refused(self):
        self.put("Other/Retry policy v1.md", "# other\n")
        self.assertIn("same path", self.refused("Memory\tWiki", "Other\tWiki"))

    def test_cycle_is_refused(self):
        self.assertIn("cycle", self.refused(f"{V1}\t{V2}", f"{V2}\t{V1}"))

    def test_malformed_and_unsafe_lines_are_refused(self):
        errors = self.refused("only one column", "Home.md\t../outside.md",
                              ".vault-memory/config.json\tconfig.json", "Memory\tMemory/Inside")
        self.assertIn("line 1", errors)
        self.assertIn("line 2", errors)
        self.assertIn("line 3", errors)
        self.assertIn("into itself", errors)

    def test_note_cannot_lose_its_extension(self):
        self.assertIn("extension", self.refused(f"{V1}\tWiki/Retry policy"))

    def test_empty_plan_is_refused(self):
        self.assertIn("no lines", self.refused("# only a comment"))

    def test_refused_plan_touches_nothing(self):
        before = self.snapshot()
        self.refused(f"{V1}\tWiki/Retry.md", "Memory/Nope.md\tWiki/Nope.md")
        self.assertEqual(self.snapshot(), before)

    def test_folders_may_merge_when_no_file_collides(self):
        result = self.move("Atlas/Docs\tWiki", "Atlas/Runbooks\tWiki")
        self.assertTrue((self.vault / "Wiki/System map.md").exists())
        self.assertTrue((self.vault / "Wiki/Runbook - Renew TLS certificate.md").exists())
        self.assertFalse((self.vault / "Atlas/Docs").exists())
        self.assertEqual(result["counts"]["files"], 9)


class TestDryRun(Env):
    def test_dry_run_writes_nothing_and_lists_every_change(self):
        before = self.snapshot()
        result = self.move("Atlas/Docs\tWiki/Docs", f"{V1}\tWiki/Retry policy (old).md", apply=False)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(result["applied"])
        moved = {(f["from"], f["to"]) for f in result["files"]}
        self.assertIn(("Atlas/Docs/System map.md", "Wiki/Docs/System map.md"), moved)
        self.assertIn((V1, "Wiki/Retry policy (old).md"), moved)
        self.assertIn({"p": V2, "from": "[[Retry policy v1]]", "to": "[[Retry policy (old)]]"},
                      result["links"])
        self.assertIn({"key": "areas", "from": "Atlas/Docs", "to": "Wiki/Docs"}, result["config"])


POLICY = "Atlas/Docs/Refund policy.md"
LINKER = """---
type: doc
area: general
tags:
  - doc
related: Refund policy, Checkout architecture
---
# Linker

See [[Refund policy]] and [[Atlas/Docs/Refund policy]] and [[Refund policy#Limits]] and
[[Refund policy|the policy]] and [[refund policy]] and [[Refund policy.md]].
| table | [[Refund policy\\|policy]] |
Embeds: ![[diagram.png]] and ![[Refund policy#Limits]]
Markdown: [policy](../Atlas/Docs/Refund%20policy.md) and [root](Atlas/Docs/Refund%20policy.md#limits)
and ![img](<../Atlas/Docs/diagram.png>)
Code: `[[Refund policy]]` and

```
[[Refund policy]] [x](../Atlas/Docs/Refund%20policy.md)
```

Unrelated: [[System map]] and [site](https://example.com/Refund%20policy.md) and [[Missing note]]
"""
LINKER_AFTER = """---
type: doc
area: general
tags:
  - doc
related: Refunds, Checkout architecture
---
# Linker

See [[Refunds]] and [[Wiki/Policies/Refunds]] and [[Refunds#Limits]] and
[[Refunds|the policy]] and [[Refunds]] and [[Refunds.md]].
| table | [[Refunds\\|policy]] |
Embeds: ![[refund-diagram.png]] and ![[Refunds#Limits]]
Markdown: [policy](../Wiki/Policies/Refunds.md) and [root](Wiki/Policies/Refunds.md#limits)
and ![img](<../Wiki/Attachments/refund-diagram.png>)
Code: `[[Refund policy]]` and

```
[[Refund policy]] [x](../Atlas/Docs/Refund%20policy.md)
```

Unrelated: [[System map]] and [site](https://example.com/Refund%20policy.md) and [[Missing note]]
"""


def mask(text):
    """The text with every link target blanked: what must not change."""
    import re
    text = re.sub(r"\[\[[^\]\n]*\]\]", "[[]]", text)
    text = re.sub(r"\]\([^)\n]*\)", "]()", text)
    return re.sub(r"(?m)^(related|replaces):.*$", r"\1:", text)


class TestLinks(Env):
    def setUp(self):
        super().setUp()
        self.put(POLICY, "# Refund policy\n\nSee the [map](System%20map.md) and [[System map]].\n\n## Limits\n")
        self.put("Atlas/Docs/diagram.png", b"\x89PNG fake")
        self.put("Notes/Linker.md", LINKER)

    def plan(self):
        return (f"{POLICY}\tWiki/Policies/Refunds.md", "Atlas/Docs/diagram.png\tWiki/Attachments/refund-diagram.png")

    def test_every_form_of_link_follows_the_moved_files(self):
        result = self.move(*self.plan())
        self.assertEqual(self.text("Notes/Linker.md"), LINKER_AFTER)
        self.assertEqual((self.vault / "Wiki/Attachments/refund-diagram.png").read_bytes(), b"\x89PNG fake")
        self.assertFalse((self.vault / POLICY).exists())
        self.assertEqual(result["counts"]["files"], 2)
        self.assertEqual(result["counts"]["links"], 14)        # 13 in Linker + the moved note's own
        self.assertEqual(result["counts"]["notes_rewritten"], 2)

    def test_a_moved_note_keeps_its_own_relative_links_working(self):
        self.move(*self.plan())
        self.assertEqual(self.text("Wiki/Policies/Refunds.md"),
                         "# Refund policy\n\nSee the [map](../../Atlas/Docs/System%20map.md) and [[System map]].\n\n## Limits\n")

    def test_a_name_link_stays_when_only_the_folder_changes(self):
        home = self.text("Home.md")
        self.move("Atlas/Docs\tWiki/Docs")
        self.assertEqual(self.text("Home.md"), home)
        self.assertIn("[[Wiki/Docs/Refund policy]]", self.text("Notes/Linker.md"))

    def test_a_name_that_becomes_ambiguous_is_pinned_by_path(self):
        self.move("Atlas/Docs/Code style guide.md\tAtlas/Style/System map.md")
        self.assertIn("[[Atlas/Docs/System map]]", self.text("Home.md"))

    def test_an_ambiguous_name_is_reported_not_guessed(self):
        self.put("Notes/A/Same.md", "# A\n")
        self.put("Notes/B/Same.md", "# B\n")
        self.put("Notes/Uses same.md", "Read [[Same]].\n")
        result = self.move("Notes/A/Same.md\tNotes/C/Same.md")
        self.assertEqual(self.text("Notes/Uses same.md"), "Read [[Same]].\n")
        self.assertTrue(any("[[Same]]" in w and "2 files" in w for w in result["warnings"]), result["warnings"])

    def test_accents_spaces_and_decomposed_names(self):
        import unicodedata
        self.put("Atlas/Docs/Política de devoluciones.md", "# P\n")
        decomposed = unicodedata.normalize("NFD", "Política de devoluciones")
        self.put("Notes/Accents.md", f"[[Política de devoluciones]] [[{decomposed}|x]] "
                                     "[d](../Atlas/Docs/Pol%C3%ADtica%20de%20devoluciones.md)\n")
        self.move("Atlas/Docs/Política de devoluciones.md\tWiki/Devoluciones y cambios.md")
        self.assertEqual(self.text("Notes/Accents.md"),
                         "[[Devoluciones y cambios]] [[Devoluciones y cambios|x]] "
                         "[d](../Wiki/Devoluciones%20y%20cambios.md)\n")

    def test_relative_wikilinks_are_recomputed(self):
        self.put("Atlas/Docs/Relative.md", "[[../../Memory/Retry policy v1]] [[./System map]]\n")
        self.move("Atlas/Docs/Relative.md\tWiki/Relative.md")
        self.assertEqual(self.text("Wiki/Relative.md"), "[[../Memory/Retry policy v1]] [[../Atlas/Docs/System map]]\n")

    def test_only_links_change_in_the_body(self):
        before = {rel: self.text(rel) for rel in ("Notes/Linker.md", POLICY, V2, "Home.md")}
        self.move(*self.plan(), "Atlas/Docs\tWiki/Docs", "Memory\tWiki/Memory")
        after = {"Notes/Linker.md": "Notes/Linker.md", POLICY: "Wiki/Policies/Refunds.md",
                 V2: "Wiki/Memory/Retry policy v2.md", "Home.md": "Home.md"}
        for old, new in after.items():
            self.assertEqual(mask(self.text(new)), mask(before[old]), old)

    def test_markdown_links_with_parentheses_survive_two_moves(self):
        self.put("Memory/Paren.md", "[a](Retry%20policy%20v1.md)\n")
        self.move(f"{V1}\tWiki/Retry policy (old).md")
        self.assertEqual(self.text("Memory/Paren.md"), "[a](../Wiki/Retry%20policy%20(old).md)\n")
        self.move("Wiki/Retry policy (old).md\tWiki/Retry policy (older).md")
        self.assertEqual(self.text("Memory/Paren.md"), "[a](../Wiki/Retry%20policy%20(older).md)\n")

    def test_a_link_from_the_vault_root_stays_when_only_its_note_moves(self):
        self.put("Notes/Root.md", "[r](Atlas/Docs/System%20map.md) [s](/Atlas/Docs/System%20map.md)\n")
        result = self.move("Notes/Root.md\tWiki/Root.md")
        self.assertEqual(self.text("Wiki/Root.md"), "[r](Atlas/Docs/System%20map.md) [s](/Atlas/Docs/System%20map.md)\n")
        self.assertEqual(result["counts"]["links"], 0)

    def test_a_destination_that_differs_only_in_letter_case_is_refused(self):
        self.assertIn("already exists", self.refused("Home.md\tmemory/retry policy v2.md"))

    def test_case_only_rename_works_on_any_disk(self):
        self.move(f"{V1}\tMemory/retry Policy V1.md", "Templates\ttemplates")
        self.assertIn("retry Policy V1.md", os.listdir(self.vault / "Memory"))
        self.assertNotIn("Retry policy v1.md", os.listdir(self.vault / "Memory"))
        self.assertIn("templates", os.listdir(self.vault))
        self.assertNotIn("Templates", os.listdir(self.vault))
        self.assertIn("replaces: [[retry Policy V1]]", self.text(V2))
        self.assertIn("templates", json.loads(self.text(".vault-memory/config.json"))["types_by_folder"])


BIG_PLAN = ("Atlas/Docs\tWiki/Docs", "Atlas/Runbooks\tWiki/Runbooks",
            "Atlas/Runbooks/Runbook - Renew TLS certificate.md\tWiki/Runbooks/Renew TLS certificate.md",
            "Memory\tWiki/Memory", "Atlas/Daily\tOutput/Daily", "Captures\tRaw")


class TestResolvedLikeTheIndexer(Env):
    """move must follow a link wherever the indexer (and Obsidian) say it points."""

    def test_a_path_link_to_a_folder_that_no_longer_exists_follows_the_name(self):
        self.put("Team/Overview.md", "# Overview\n")
        self.put("Team/Start.md", "# Start\n\nSee [[01_OLD/Overview]].\n")
        self.move("Team/Overview.md\tWiki/System overview.md")
        self.assertIn("[[Wiki/System overview]]", self.text("Team/Start.md"))     # its real path now

    def test_an_ambiguous_name_follows_the_closest_note(self):
        self.put("Team/Glossary/Glossary.md", "# Glossary\n")
        self.put("Tools/copy/GLOSSARY.md", "# Tool glossary\n")
        self.put("Team/Home.md", "# Home\n\nSee [[Glossary]].\n")
        self.move("Team/Glossary/Glossary.md\tWiki/Team glossary.md")
        self.assertIn("[[Team glossary]]", self.text("Team/Home.md"))


class TestVault(Env):
    def setUp(self):
        super().setUp()
        import capture as CP
        self.capture = CP.write(self.cfg(), "Thread: see [[Memory/Retry policy v1]] and [[System map]].\n",
                                title="Retry thread", date="2026-11-10")["p"]
        self.put("Memory/Learned from thread.md",
                 f'---\ntype: doc\narea: memory\ntags:\n  - doc\ncapture: "[[{self.capture[:-3]}]]"\n---\n'
                 "# Learned from thread\n\nSee [[Retry policy v2]].\n")
        self.put(".vault-memory/reviews.jsonl",
                 json.dumps({"t": "2026-11-01T10:00:00", "day": "2026-11-01", "p": V2, "verdict": "confirmed",
                             "confidence": 9}) + "\n"
                 + json.dumps({"t": "2026-11-01T10:01:00", "day": "2026-11-01", "p": "Home.md",
                               "verdict": "unsure"}) + "\n")
        self.put(".vault-memory/flags.jsonl", json.dumps({"day": "2026-11-02", "p": V1, "reason": "old"}) + "\n")
        self.put(".vault-memory/telemetry.jsonl", "\n".join([
            json.dumps({"t": "2026-11-03T09:00:00", "ev": "read", "p": "Atlas/Docs/System map.md"}),
            json.dumps({"t": "2026-11-03T09:01:00", "ev": "hook", "emitted": [V1, "Home.md"]}),
            json.dumps({"t": "2026-11-03T09:02:00", "ev": "recall", "ps": ["Home.md"]}),
            "not json"]) + "\n")
        self.put(".vault-memory/telemetria.jsonl", json.dumps({"ev": "leida", "p": V2}) + "\n"
                 + json.dumps({"ev": "hook", "emitidos": [V1, "Home.md"]}) + "\n")
        self.put(".vault-memory/usage.json", json.dumps(
            {"generated": TODAY, "reads": {"Home.md": 2, V2: 5}}, indent=1) + "\n")
        self.put(".vault-memory/embeddings/index.json", json.dumps(
            {"v": 1, "model": "m", "dim": 2, "file": "vectors-1.f32", "count": 2,
             "notes": {V2: [0, "aa"], "Home.md": [1, "bb"]}}, separators=(",", ":")))
        self.put(".vault-memory/inbox/2026-11-10-x-1-retry.md",
                 f'---\ntype: doc\nsource: distill 2026-11-10 x\ncapture: "[[{self.capture[:-3]}]]"\n---\n\n'
                 "# Retry\n\nLike [[Retry policy v1]].\n")
        self.put(".vault-memory/inbox/done/2026-11-09-x-1-old.md",
                 f"---\ntype: doc\noutcome: accepted\nnote: {V1}\n---\n\n# Old\n")
        config = json.loads(self.text(".vault-memory/config.json"))
        config["taxonomy"] = "Atlas/Docs/Orders glossary.md"
        config["distill"] = {"folder": "Memory/learned"}
        config["hook"] = {"memory_prefix": "Memory/"}
        config["exclude"] = ["Daily", "node_modules"]
        self.put(".vault-memory/config.json", json.dumps(config, indent=2))

    def test_config_keys_with_folders_follow_the_move(self):
        result = self.move(*BIG_PLAN)
        config = json.loads(self.text(".vault-memory/config.json"))
        self.assertEqual(config["areas"], {"Atlas/Incidents": "support", "Wiki/Runbooks": "support",
                                           "Atlas/Decisions": "product", "Wiki/Docs": "engineering",
                                           "Output/Daily": "log", "Wiki/Memory": "memory"})
        self.assertIn("Wiki/Docs", config["types_by_folder"])
        self.assertEqual(config["recall"]["skip_paths"], ["Output/Daily/"])
        self.assertEqual(config["captures"]["folder"], "Raw")
        self.assertEqual(config["distill"]["folder"], "Wiki/Memory/learned")
        self.assertEqual(config["answers"]["folder"], "Wiki/Memory/answers")     # was the default
        self.assertEqual(config["taxonomy"], "Wiki/Docs/Orders glossary.md")
        self.assertEqual(config["hook"]["memory_prefix"], "Wiki/Memory/")
        self.assertEqual(config["exclude"], ["Daily", "node_modules"])          # name kept: not renamed
        self.assertEqual(result["counts"]["config"], len(result["config"]))
        self.assertEqual(C.load_config(self.vault)["captures"]["folder"], "Raw")

    def test_generated_pages_follow_the_move(self):
        config = json.loads(self.text(".vault-memory/config.json"))
        config["catalog"], config["log"] = {"path": "Home.md"}, {"path": "Home.md"}
        self.put(".vault-memory/config.json", json.dumps(config, indent=2))
        self.move("Home.md\tWiki/Start here.md")
        config = json.loads(self.text(".vault-memory/config.json"))
        self.assertEqual((config["catalog"]["path"], config["log"]["path"]), ("Wiki/Start here.md",) * 2)

    def test_a_folder_moved_in_pieces_is_reported_in_the_config(self):
        result = self.move("Atlas/Incidents/Inc - 2026-08-12 - Login fails with SSO.md\tWiki/Login fails with SSO.md")
        self.assertTrue(any("'Atlas/Incidents' no longer covers 1" in w for w in result["warnings"]), result["warnings"])

    def test_state_saved_by_path_follows_the_move(self):
        result = self.move(*BIG_PLAN)
        reviews = [json.loads(x) for x in self.text(".vault-memory/reviews.jsonl").splitlines()]
        self.assertEqual([r["p"] for r in reviews], ["Wiki/Memory/Retry policy v2.md", "Home.md"])
        self.assertEqual(self.text(".vault-memory/reviews.jsonl").splitlines()[1],
                         json.dumps({"t": "2026-11-01T10:01:00", "day": "2026-11-01", "p": "Home.md",
                                     "verdict": "unsure"}))
        self.assertIn("Wiki/Memory/Retry policy v1.md", self.text(".vault-memory/flags.jsonl"))
        telemetry = self.text(".vault-memory/telemetry.jsonl").splitlines()
        self.assertEqual(json.loads(telemetry[0])["p"], "Wiki/Docs/System map.md")
        self.assertEqual(json.loads(telemetry[1])["emitted"], ["Wiki/Memory/Retry policy v1.md", "Home.md"])
        self.assertEqual(telemetry[2], json.dumps({"t": "2026-11-03T09:02:00", "ev": "recall", "ps": ["Home.md"]}))
        self.assertEqual(telemetry[3], "not json")
        self.assertIn("Wiki/Memory/Retry policy v2.md", self.text(".vault-memory/telemetria.jsonl"))
        legacy = self.text(".vault-memory/telemetria.jsonl").splitlines()
        self.assertEqual(json.loads(legacy[1])["emitidos"], ["Wiki/Memory/Retry policy v1.md", "Home.md"])
        usage = json.loads(self.text(".vault-memory/usage.json"))
        self.assertEqual(usage["reads"], {"Home.md": 2, "Wiki/Memory/Retry policy v2.md": 5})
        vectors = json.loads(self.text(".vault-memory/embeddings/index.json"))
        self.assertEqual(vectors["notes"], {"Wiki/Memory/Retry policy v2.md": [0, "aa"], "Home.md": [1, "bb"]})
        pending = self.text(".vault-memory/inbox/2026-11-10-x-1-retry.md")
        self.assertIn('capture: "[[Raw/2026-11-10 retry-thread]]"', pending)
        self.assertIn("[[Retry policy v1]]", pending)                     # a name: still unique
        self.assertIn("note: Wiki/Memory/Retry policy v1.md", self.text(".vault-memory/inbox/done/2026-11-09-x-1-old.md"))
        by_file = {s["file"]: s["lines"] for s in result["state"]}
        self.assertEqual(by_file["reviews.jsonl"], 1)
        self.assertEqual(by_file["telemetry.jsonl"], 2)
        self.assertEqual(by_file["usage.json"], 1)

    def test_review_history_follows_the_moved_note(self):
        import calibration as CB
        self.move(*BIG_PLAN)
        verdict, confidence, _ = CB.previous(self.cfg(), "Wiki/Memory/Retry policy v2.md", {})
        self.assertEqual((verdict, confidence), ("confirmed", 9))

    def test_captures_move_intact_and_are_reported_not_edited(self):
        import capture as CP
        raw = (self.vault / self.capture).read_bytes()
        result = self.move(*BIG_PLAN)
        moved = "Raw/" + self.capture.split("/", 1)[1]
        self.assertEqual((self.vault / moved).read_bytes(), raw)
        self.assertTrue(any(moved in w and "[[Memory/Retry policy v1]]" in w for w in result["warnings"]),
                        result["warnings"])
        self.assertFalse(any("[[System map]]" in w for w in result["warnings"]))   # still reaches it
        self.assertIn('capture: "[[Raw/2026-11-10 retry-thread]]"', self.text("Wiki/Memory/Learned from thread.md"))
        cfg = self.cfg()
        report = CP.report(cfg, [{"p": "Wiki/Memory/Learned from thread.md", "fm": ["capture"]}])
        self.assertEqual((report["n"], report["edited"], report["dangling"]), (1, [], []))

    def test_a_capture_that_links_to_a_renamed_note_is_reported(self):
        result = self.move(f"{V1}\tMemory/Retry policy (old).md")
        self.assertIn("[[Memory/Retry policy v1]]", self.text(self.capture))
        self.assertTrue(any(self.capture in w and "[[Memory/Retry policy (old)]]" in w for w in result["warnings"]))

    def test_everything_touched_is_backed_up(self):
        before = self.snapshot()
        result = self.move(*BIG_PLAN)
        backup = Path(result["backup"])
        self.assertTrue(backup.name.startswith("move-"))
        self.assertEqual(backup.parent, self.vault / ".vault-memory" / "backups")
        for rel in ("Atlas/Docs/System map.md", V1, V2, "Memory/Learned from thread.md", self.capture,
                    "Atlas/Runbooks/Runbook - Renew TLS certificate.md"):
            self.assertEqual((backup / rel).read_bytes(), before[rel], rel)
        for rel in ("config.json", "reviews.jsonl", "telemetry.jsonl", "usage.json", "embeddings/index.json",
                    "inbox/done/2026-11-09-x-1-old.md"):
            self.assertEqual((backup / ".vault-memory" / rel).read_bytes(), before[".vault-memory/" + rel], rel)
        self.assertEqual((backup / ".vault-memory" / "move-plan.tsv").read_text(encoding="utf-8"),
                         "\n".join(BIG_PLAN) + "\n")

    def test_the_same_plan_gives_the_same_vault(self):
        twin = self.tmp / "twin"
        shutil.copytree(self.vault, twin)
        first = self.move(*BIG_PLAN)
        second = MV.run(C.load_config(twin), "\n".join(BIG_PLAN) + "\n", apply=True)
        first.pop("backup"), second.pop("backup")
        self.assertEqual(first, second)
        mine = self.snapshot(skip_backups=True)
        self.vault = twin
        self.assertEqual(self.snapshot(skip_backups=True), mine)

    def test_no_new_broken_link(self):
        import indexer as IX
        self.put("Notes/Linker.md", LINKER)
        self.put(POLICY, "# Refund policy\n")
        self.put("Atlas/Docs/diagram.png", b"png")
        _, graph, _ = IX.build(self.cfg(), quiet=True, write=False)
        broken_before = {b["link"] for b in graph["broken"]}
        result = self.move(*BIG_PLAN, "Notes\tWiki/Notes")
        moved = {f["from"]: f["to"] for f in result["files"]}
        _, graph, _ = IX.build(self.cfg(), quiet=True, write=False)
        self.assertTrue({b["link"] for b in graph["broken"]} <= broken_before, graph["broken"])
        self.assertEqual(len(graph["broken"]), len(broken_before))
        self.assertIn("Wiki/Notes/Linker.md", moved.values())


class TestCli(Env):
    def cli(self, *args, code=0):
        env = {k: v for k, v in os.environ.items() if k != "VAULT_MEMORY_VAULT"}
        result = subprocess.run([sys.executable, "-B", str(SCRIPTS / "cli.py"), "--vault", str(self.vault), *args],
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def plan_file(self, *lines):
        path = self.tmp / "plan.tsv"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(path)

    def test_dry_run_lists_and_writes_nothing(self):
        before = self.snapshot()
        out = self.cli("move", self.plan_file(f"{V1}\tWiki/Retry policy (old).md")).stdout
        self.assertEqual(self.snapshot(), before)
        self.assertIn(f"{V1}  →  Wiki/Retry policy (old).md", out)
        self.assertIn("[[Retry policy v1]] → [[Retry policy (old)]]", out)
        self.assertIn("Dry run", out)

    def test_invalid_plan_exits_2(self):
        err = self.cli("move", self.plan_file("Nope.md\tX.md"), code=2).stderr
        self.assertIn("does not exist", err)
        out = json.loads(self.cli("move", self.plan_file("Nope.md\tX.md"), "--json", code=2).stdout)
        self.assertIn("does not exist", out["errors"][0])

    def test_apply_moves_and_rebuilds_the_index(self):
        out = json.loads(self.cli("move", self.plan_file("Memory\tWiki/Memory"), "--apply", "--json").stdout)
        self.assertTrue(out["applied"])
        self.assertEqual(out["counts"]["files"], 7)
        index = json.loads(self.text(".vault-memory/index.json"))
        paths = {d["p"] for d in index["docs"]}
        self.assertIn("Wiki/Memory/Retry policy v2.md", paths)
        self.assertNotIn(V2, paths)
        graph = json.loads(self.text(".vault-memory/graph.json"))
        self.assertIn("Wiki/Memory/Retry policy v1.md", graph["backlinks"])


if __name__ == "__main__":
    unittest.main()
