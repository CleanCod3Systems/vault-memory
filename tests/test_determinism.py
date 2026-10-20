"""Determinism, read-only commands, incremental index and performance."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
EXAMPLE = REPO / "tests" / "example_vault"


def without_generated(text):
    data = json.loads(text)
    if isinstance(data, dict):
        data.pop("generated", None)
    return data


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        shutil.copytree(EXAMPLE, self.vault)
        self.state = self.vault / ".vault-memory"

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def cli(self, *args, env=None):
        result = subprocess.run([sys.executable, "-B", str(SCRIPTS / "cli.py"), "--vault", str(self.vault), *args],
                                capture_output=True, text=True, env={**os.environ, **(env or {})})
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def mtimes(self):
        return {f.name: f.stat().st_mtime_ns for f in self.state.glob("*.json")}


class TestDeterminism(Base):
    def test_index_is_reproducible(self):
        files = ("index.json", "graph.json", "contract.json")
        self.cli("index")
        first = {f: without_generated((self.state / f).read_text(encoding="utf-8")) for f in files}
        self.cli("index")
        second = {f: without_generated((self.state / f).read_text(encoding="utf-8")) for f in files}
        self.assertEqual(first, second)

    def test_today_pins_stale_notes(self):
        first = self.cli("--today", "2026-10-01", "patterns")
        second = self.cli("--today", "2026-10-01", "patterns")
        self.assertEqual(first, second)
        # With another date the ages change: proves --today is really used.
        self.cli("--today", "2027-10-01", "patterns")
        result = json.loads((self.state / "patterns.json").read_text(encoding="utf-8"))
        self.assertTrue(all(e["days"] > 300 for e in result["stale"]))


class TestReadOnly(Base):
    def test_doctor_fix_retype_do_not_write(self):
        self.cli("index")
        before = self.mtimes()
        time.sleep(0.02)
        self.cli("doctor")
        self.cli("fix")
        self.cli("retype")
        self.cli("migrate")
        self.assertEqual(self.mtimes(), before)


class TestIncremental(Base):
    def test_deletion_shows_without_index(self):
        self.cli("index")
        note = self.vault / "Atlas" / "Incidents" / "Inc - 2026-07-30 - Negative stock in inventory.md"
        note.unlink()
        rows = json.loads(self.cli("recall", "negative stock inventory", "--json"))
        self.assertFalse([r for r in rows if "Negative stock" in r["p"]])

    def test_new_note_enters_without_index(self):
        self.cli("index")
        (self.vault / "Atlas" / "Docs" / "Backup policy.md").write_text(
            "---\ntype: doc\narea: engineering\ntags: [doc]\n---\n# Backup policy\n\n"
            "Database backups run every night and are tested every month.\n", encoding="utf-8")
        rows = json.loads(self.cli("recall", "backup policy", "--json"))
        self.assertIn("Backup policy", rows[0]["p"])

    def test_config_change_rebuilds(self):
        self.cli("index")
        config = json.loads((self.state / "config.json").read_text(encoding="utf-8"))
        config["exclude"] = ["Memory"]
        (self.state / "config.json").write_text(json.dumps(config), encoding="utf-8")
        rows = json.loads(self.cli("recall", "retry policy", "--json"))
        self.assertFalse([r for r in rows if r["p"].startswith("Memory/")])

    def test_reuses_unchanged_notes(self):
        self.cli("index")
        index = json.loads((self.state / "index.json").read_text(encoding="utf-8"))
        self.assertTrue(all("mte" in d and "size" in d for d in index["docs"]))
        self.assertTrue(index.get("cfg"))


@unittest.skipUnless(os.environ.get("VAULT_MEMORY_PERF"), "performance: VAULT_MEMORY_PERF=1")
class TestPerformance(unittest.TestCase):
    def test_5k_notes(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            subprocess.run([sys.executable, "-B", str(REPO / "tests" / "gen_vault.py"), str(tmp / "v"), "5000"], check=True)
            base = [sys.executable, "-B", str(SCRIPTS / "cli.py"), "--vault", str(tmp / "v")]
            subprocess.run(base + ["index"], check=True, capture_output=True)
            started = time.time()
            subprocess.run(base + ["recall", "payment declined by antifraud", "--no-auto", "--json"], check=True, capture_output=True)
            no_auto = time.time() - started
            started = time.time()
            subprocess.run(base + ["recall", "payment declined by antifraud", "--json"], check=True, capture_output=True)
            with_auto = time.time() - started
            print(f"\n  5k notes: recall --no-auto {no_auto:.2f}s · with auto {with_auto:.2f}s")
            self.assertLess(no_auto, 1.0)
            self.assertLess(with_auto, 1.5)
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
