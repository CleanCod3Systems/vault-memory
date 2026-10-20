"""Contracts consumed by others (hook, skills, dashboards, MCP clients).

If a test here fails, the change breaks someone outside: either it is reverted or it is a
major version with a CHANGELOG notice. Adding keys is allowed; removing or renaming them is not.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
EXAMPLE = REPO / "tests" / "example_vault"

RECALL_JSON = {"score", "p", "n", "t", "type", "area", "date", "status", "sev", "system", "ids",
               "refs", "passage", "state", "replaced_by", "verified"}
REFS = {"wiki", "tickets", "chat", "rel"}
STALE = {"p", "type", "days"}
HEALTH = {"notes", "edges", "contract_coverage", "conforming", "orphans", "broken_links",
          "records", "open_records"}
GRAPH = {"generated", "notes", "edges", "diagnostics", "orphans", "broken", "duplicates", "hubs",
         "gaps", "backlinks", "outgoing"}
FILES = {"index.json", "graph.json", "contract.json", "patterns.json", "related.json"}
SUBCOMMANDS = {"init", "modules", "index", "doctor", "recall", "fix", "retype", "patterns", "panel",
               "refresh", "schedule", "info", "config", "note", "usage", "mcp", "eval", "migrate",
               "install-cli"}


class TestExternalContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.vault = cls.tmp / "vault"
        shutil.copytree(EXAMPLE, cls.vault)
        cls.env = {**os.environ, "VAULT_MEMORY_TODAY": "2026-10-01"}
        cls.cli("refresh")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    @classmethod
    def cli(cls, *args):
        result = subprocess.run([sys.executable, "-B", str(REPO / "scripts" / "cli.py"), "--vault", str(cls.vault), *args],
                                capture_output=True, text=True, env=cls.env)
        assert result.returncode == 0, result.stderr
        return result.stdout

    def read(self, name):
        return json.loads((self.vault / ".vault-memory" / name).read_text(encoding="utf-8"))

    def test_recall_json(self):
        rows = json.loads(self.cli("recall", "checkout", "--json", "--n", "3"))
        self.assertIsInstance(rows, list)
        for row in rows:
            self.assertTrue(RECALL_JSON <= set(row), RECALL_JSON - set(row))
            self.assertTrue(REFS <= set(row["refs"]))
            self.assertIsInstance(row["score"], float)

    def test_derived_files(self):
        present = {f.name for f in (self.vault / ".vault-memory").glob("*.json")}
        self.assertTrue(FILES <= present, FILES - present)

    def test_patterns(self):
        result = self.read("patterns.json")
        self.assertTrue(HEALTH <= set(result["health"]), HEALTH - set(result["health"]))
        self.assertTrue(result["stale"], "the example has old open incidents")
        for entry in result["stale"]:
            self.assertTrue(STALE <= set(entry))

    def test_graph_and_related(self):
        self.assertTrue(GRAPH <= set(self.read("graph.json")))
        related = self.read("related.json")
        self.assertIn("items", related)
        for neighbors in related["items"].values():
            for neighbor in neighbors:
                self.assertTrue({"p", "n", "t", "type", "date", "status", "score"} <= set(neighbor))

    def test_subcommands(self):
        help_text = subprocess.run([sys.executable, "-B", str(REPO / "scripts" / "cli.py"), "--help"],
                                   capture_output=True, text=True).stdout
        missing = {c for c in SUBCOMMANDS if c not in help_text}
        self.assertFalse(missing, missing)

    def test_vault_before_subcommand(self):
        self.cli("recall", "stock", "--json")


if __name__ == "__main__":
    unittest.main()
