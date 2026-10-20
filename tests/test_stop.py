"""End-of-session memory prompt (Stop) and context after compaction (SessionStart compact)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import telemetry as TL  # noqa: E402

EXAMPLE = REPO / "tests" / "example_vault"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        shutil.copytree(EXAMPLE, self.vault)
        self.env = {**os.environ, "CLAUDE_CONFIG_DIR": str(self.tmp), "VAULT_MEMORY_VAULT": str(self.vault),
                    "VAULT_MEMORY_TODAY": "2026-10-01"}
        subprocess.run([sys.executable, "-B", str(REPO / "scripts" / "cli.py"), "--vault", str(self.vault), "index"],
                       check=True, capture_output=True, env=self.env)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def event(self, **fields):
        TL.record(self.vault, fields, {}, "s1")

    def run_hook(self, hook, **payload):
        result = subprocess.run([sys.executable, "-B", str(REPO / "hooks" / hook)], text=True, capture_output=True,
                                input=json.dumps({"session_id": "s1", **payload}), env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()


class TestStop(Base):
    def test_no_evidence_no_interruption(self):
        self.event(ev="hook", q="x", emitted=["Atlas/Docs/System map.md"])
        self.assertEqual(self.run_hook("stop.py"), "")

    def test_gap_proposes_only_once(self):
        self.event(ev="recall", origin="cli", q="vacation policy", n=0)
        out = json.loads(self.run_hook("stop.py"))
        self.assertEqual(out["decision"], "block")
        self.assertIn("vault-memory note", out["reason"])
        self.assertEqual(self.run_hook("stop.py"), "", "once per session")

    def test_expired_note_read_proposes_verifying(self):
        self.event(ev="read", p="Memory/Temporary shipping provider.md")
        out = json.loads(self.run_hook("stop.py"))
        self.assertIn("--verified today", out["reason"])

    def test_respects_stop_hook_active_and_off_switch(self):
        self.event(ev="recall", origin="cli", q="something without an answer", n=0)
        self.assertEqual(self.run_hook("stop.py", stop_hook_active=True), "")
        config_file = self.vault / ".vault-memory" / "config.json"
        config = json.loads(config_file.read_text(encoding="utf-8"))
        config["hook"] = {"stop_prompt": False}
        config_file.write_text(json.dumps(config), encoding="utf-8")
        self.assertEqual(self.run_hook("stop.py"), "")


class TestCompact(Base):
    def test_reminds_what_was_read_briefly(self):
        for path in ("Atlas/Docs/System map.md", "Atlas/Runbooks/Runbook - Renew TLS certificate.md",
                     "Atlas/Docs/System map.md"):
            self.event(ev="read", p=path)
        out = self.run_hook("compact.py")
        self.assertTrue(out.startswith("[vault] already read"))
        self.assertEqual(out.count("System map"), 1)
        self.assertLessEqual(len(out), 200)

    def test_without_reads_says_nothing(self):
        self.assertEqual(self.run_hook("compact.py"), "")


if __name__ == "__main__":
    unittest.main()
