"""Living next to Obsidian: never drop what the person typed, follow the Properties rules,
ship the memory view and suggest the links that are missing."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))

import contract as CT   # noqa: E402
import core as C        # noqa: E402
import links as LK      # noqa: E402
import note as N        # noqa: E402
import review as RV     # noqa: E402

ACCESS = "Memory/Request production access.md"


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.vault = self.tmp / "vault"
        shutil.copytree(REPO / "tests" / "example_vault", self.vault)
        old = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(old)))
        os.environ["VAULT_MEMORY_TODAY"] = "2026-11-10"

    def cfg(self):
        return C.load_config(self.vault)

    def write(self, rel, text):
        path = self.vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def cli(self, *args):
        return subprocess.run([sys.executable, "-B", str(SCRIPTS / "cli.py"), "--vault", str(self.vault), *args],
                              capture_output=True, text=True, env={**os.environ})


class TestSafeWrite(Env):
    def test_a_save_in_between_is_never_overwritten(self):
        path = self.vault / ACCESS
        before = path.read_text(encoding="utf-8")
        path.write_text(before + "\nTyped in Obsidian.\n", encoding="utf-8")
        with self.assertRaises(N.NoteError) as error:
            N.replace_if_unchanged(path, before, "replaced")
        self.assertEqual(error.exception.code, 4)
        self.assertIn("Typed in Obsidian.", path.read_text(encoding="utf-8"))

    def test_review_keeps_what_was_typed_meanwhile(self):
        path = self.vault / ACCESS
        real_backup = CT.backup

        def obsidian_saves(cfg, rel, target=None):
            result = real_backup(cfg, rel, target)
            path.write_text(path.read_text(encoding="utf-8") + "\nTyped meanwhile.\n", encoding="utf-8")
            return result
        with mock.patch.object(CT, "backup", obsidian_saves):
            with self.assertRaises(N.NoteError):
                RV.record(self.cfg(), ACCESS, "confirmed", "ticket shows it", 9)
        text = path.read_text(encoding="utf-8")
        self.assertIn("Typed meanwhile.", text)
        self.assertNotIn("confidence:", text)


class TestProperties(Env):
    def test_each_rule(self):
        self.write("Memory/Broken.md", "---\ntype: doc\ntag: memory\nrelated: [[Home]]\nsee:\n  - [[Home]]\n"
                                       "meta:\n  owner: ana\ntype: note\n---\n# Broken\n")
        issues = {(i["issue"], i["key"]) for i in CT.property_issues(self.cfg()) if i["p"] == "Memory/Broken.md"}
        self.assertEqual(issues, {("deprecated key (use tags)", "tag"), ("unquoted wikilink", "related"),
                                  ("unquoted wikilink", "see"), ("nested value", "meta"), ("repeated key", "type")})

    def test_clean_notes_and_quoted_links_pass(self):
        self.write("Memory/Clean.md", '---\ntype: doc\nreplaces: "[[Home]]"\ntags:\n  - doc\n---\n# Clean\n')
        self.assertFalse([i for i in CT.property_issues(self.cfg()) if i["p"] == "Memory/Clean.md"])

    def test_doctor_json_carries_them(self):
        import json
        self.write("Memory/Broken.md", "---\ntype: doc\nalias: x\n---\n# Broken\n")
        out = self.cli("doctor", "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(any(i["key"] == "alias" for i in json.loads(out.stdout)["properties"]))


class TestBases(Env):
    def test_memory_view_ships_and_install_never_overwrites(self):
        self.assertTrue((REPO / "standard" / "bases" / "Memory.base").exists())
        self.write("Map.base", "# mine\n")
        dry = self.cli("bases")
        self.assertIn("Memory.base", dry.stdout)
        self.assertNotIn("Map.base", dry.stdout)
        self.assertFalse((self.vault / "Memory.base").exists(), "without --install nothing is written")
        done = self.cli("bases", "Memory", "--install", "--folder", "Memory")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue((self.vault / "Memory" / "Memory.base").exists())
        self.assertEqual(sorted(p.name for p in (self.vault / "Memory").glob("*.base")), ["Memory.base"],
                         "naming a view installs only that one")
        self.assertEqual((self.vault / "Map.base").read_text(encoding="utf-8"), "# mine\n")

    def test_memory_view_reads_the_review_fields(self):
        text = (REPO / "standard" / "bases" / "Memory.base").read_text(encoding="utf-8")
        for field in ("verified", "confidence", "evidence", "review", "volatility", "checked"):
            self.assertIn(field, text)


class TestLinks(Env):
    BODY = ("Card payments retry three times with exponential backoff before the order is parked "
            "for manual review by the payments team on call.")

    def test_similar_unlinked_notes_are_suggested_until_linked(self):
        self.write("Memory/Payments/Card retries.md", "---\ntype: doc\n---\n# Card retries\n\n" + self.BODY)
        self.write("Memory/Ops/Parked orders.md", "---\ntype: doc\n---\n# Parked orders\n\n" + self.BODY +
                   " The on call engineer releases parked orders.")
        pairs = {(p["a_n"], p["b_n"]) for p in LK.suggest(self.cfg(), 50)}
        self.assertIn(("Parked orders", "Card retries"), {(b, a) for a, b in pairs} | pairs)
        self.write("Memory/Ops/Parked orders.md", "---\ntype: doc\n---\n# Parked orders\n\n" + self.BODY +
                   " See [[Card retries]].")
        names = {frozenset((p["a_n"], p["b_n"])) for p in LK.suggest(self.cfg(), 50)}
        self.assertNotIn(frozenset(("Parked orders", "Card retries")), names)

    def test_series_of_one_template_are_not_suggested(self):
        for name in ("Project - Alpha", "Project - Beta"):
            self.write(f"Memory/Projects/{name}.md", "---\ntype: doc\n---\n# " + name + "\n\n" + self.BODY)
        names = {frozenset((p["a_n"], p["b_n"])) for p in LK.suggest(self.cfg(), 50)}
        self.assertNotIn(frozenset(("Project - Alpha", "Project - Beta")), names)


if __name__ == "__main__":
    unittest.main()
