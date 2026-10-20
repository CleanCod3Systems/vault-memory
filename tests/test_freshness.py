"""Freshness, `vault-memory note` and the recall penalty, over the seeded cases."""
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
EXAMPLE = REPO / "tests" / "example_vault"
TODAY = {"VAULT_MEMORY_TODAY": "2026-10-01"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        shutil.copytree(EXAMPLE, self.vault)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def cli(self, *args, code=0, stdin=None):
        result = subprocess.run([sys.executable, "-B", str(SCRIPTS / "cli.py"), "--vault", str(self.vault), *args],
                                capture_output=True, text=True, input=stdin, env={**os.environ, **TODAY})
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result.stdout

    def tree(self):
        return sorted(p.relative_to(self.vault).as_posix() for p in self.vault.rglob("*.md"))


class TestDoctorFreshness(Base):
    def test_finds_seeded_cases(self):
        report = json.loads(self.cli("doctor", "--json"))["freshness"]
        self.assertEqual(report["expired"], ["Memory/Temporary shipping provider.md"])
        self.assertEqual(report["replaced"], [{"p": "Memory/Retry policy v1.md", "by": "Memory/Retry policy v2.md"}])
        self.assertEqual(report["contradictions"], [{"key": "on-call-schedule", "notes": [
            "Memory/On-call schedule - team A.md", "Memory/On-call schedule - team B.md"]}])
        pairs = {(d["a"], d["b"]) for d in report["near_duplicates"]}
        self.assertIn(("Memory/How to request production access.md", "Memory/Request production access.md"), pairs)


class TestRecallFreshness(Base):
    def test_prefers_the_current_note(self):
        rows = json.loads(self.cli("recall", "payment retry policy", "--json"))
        paths = [r["p"] for r in rows]
        self.assertLess(paths.index("Memory/Retry policy v2.md"), paths.index("Memory/Retry policy v1.md"))
        old = next(r for r in rows if r["p"].endswith("v1.md"))
        self.assertEqual(old["state"], "replaced")
        self.assertEqual(old["replaced_by"], "Memory/Retry policy v2.md")

    def test_expired_is_flagged(self):
        rows = json.loads(self.cli("recall", "temporary shipping provider", "--json"))
        self.assertEqual(rows[0]["state"], "expired")


class TestNote(Base):
    def test_duplicate_exits_3_without_touching_the_tree(self):
        before = self.tree()
        out = self.cli("note", "--title", "Request prod access", "--type", "doc", "--folder", "Memory",
                       "--body", "To request production access you have to open a ticket to the platform "
                       "team with the justification and the duration.", "--json", code=3)
        self.assertTrue(json.loads(out)["duplicates"])
        self.assertEqual(self.tree(), before)

    def test_creates_a_valid_note(self):
        result = json.loads(self.cli("note", "--title", "Maintenance window", "--type", "doc", "--folder",
                                     "Memory", "--body", "Database maintenance happens on Sundays from 2 to 4 h.",
                                     "--source", "platform meeting", "--verified", "today",
                                     "--key", "maintenance-window", "--json"))
        self.assertEqual(result["mode"], "create")
        text = (self.vault / result["p"]).read_text(encoding="utf-8")
        self.assertIn("verified: 2026-10-01", text)
        report = json.loads(self.cli("doctor", "--json"))
        self.assertEqual(report["health"]["contract_coverage"], 100.0)
        rows = json.loads(self.cli("recall", "database maintenance window", "--json"))
        self.assertEqual(rows[0]["p"], result["p"])

    def test_requires_fields_without_placeholders(self):
        out = self.cli("note", "--title", "Queues stopped", "--type", "incident", "--folder",
                       "Atlas/Incidents", "--body", "The queues stopped processing.", "--json", code=2)
        self.assertIn("severity", json.loads(out)["missing"])

    def test_replaces_resolves_a_duplicate(self):
        result = json.loads(self.cli("note", "--title", "On-call schedule", "--type", "doc", "--folder", "Memory",
                                     "--body", "On-call covers 8 to 22 h, every team.",
                                     "--key", "another-key", "--replaces", "On-call schedule - team A",
                                     "--force", "--json"))
        report = json.loads(self.cli("doctor", "--json"))["freshness"]
        self.assertIn({"p": "Memory/On-call schedule - team A.md", "by": result["p"]}, report["replaced"])

    def test_update_touches_only_what_was_asked(self):
        rel = "Memory/On-call schedule - team B.md"
        before = (self.vault / rel).read_text(encoding="utf-8")
        self.cli("note", "--update", rel, "--verified", "2026-09-30")
        after = (self.vault / rel).read_text(encoding="utf-8")
        self.assertEqual(before.replace("verified: 2026-08-15", "verified: 2026-09-30"), after)
        self.assertTrue(list((self.vault / ".vault-memory" / "backups").rglob("*.md")))

    def test_append_adds_a_dated_section(self):
        rel = "Memory/Temporary shipping provider.md"
        self.cli("note", "--update", rel, "--append", "--body", "Extended until December.",
                 "--expires", "2026-12-31")
        text = (self.vault / rel).read_text(encoding="utf-8")
        self.assertIn("## 2026-10-01\n\nExtended until December.", text)
        self.assertIn("expires: 2026-12-31", text)


if __name__ == "__main__":
    unittest.main()
