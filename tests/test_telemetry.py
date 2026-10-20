"""Local telemetry, usage metrics, compact pointer and --brief."""
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
import hookfilter as HF  # noqa: E402
import telemetry as TL   # noqa: E402

EXAMPLE = REPO / "tests" / "example_vault"


class TestMetrics(unittest.TestCase):
    def test_exact_metrics(self):
        events = [
            {"t": "2026-10-01T10:00:00", "sid": "s1", "ev": "hook", "q": "checkout down", "emitted": ["A.md", "B.md"]},
            {"t": "2026-10-01T10:01:00", "sid": "s1", "ev": "read", "p": "A.md"},
            {"t": "2026-10-01T10:02:00", "sid": "s1", "ev": "hook_silent", "q": "how do I rotate the smtp credential"},
            {"t": "2026-10-01T10:03:00", "sid": "s1", "ev": "read", "p": "C.md"},
            {"t": "2026-10-01T11:00:00", "sid": "s2", "ev": "hook", "q": "stock", "emitted": ["B.md"]},
            {"t": "2026-10-01T11:05:00", "sid": "s2", "ev": "recall", "q": "vacation policy", "n": 0},
        ]
        stats = TL.metrics(events)
        self.assertEqual(stats["sessions"], 2)
        self.assertEqual(stats["pointers"], 2)
        self.assertEqual(stats["pointers_read"], 1)
        self.assertEqual(stats["pct_read"], 50.0)
        self.assertEqual(stats["never_read"], ["B.md"])
        self.assertEqual(stats["missed_recall"], [{"p": "C.md", "q": "how do I rotate the smtp credential"}])
        self.assertEqual(stats["gaps"], ["vacation policy"])

    def test_reads_legacy_event_names(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        (tmp / ".cerebro").mkdir()
        (tmp / ".cerebro" / TL.LEGACY_FILE).write_text(json.dumps(
            {"t": "2999-01-01T00:00:00", "sid": "s1", "ev": "hook", "emitidos": ["A.md"]}) + "\n" + json.dumps(
            {"t": "2999-01-01T00:01:00", "sid": "s1", "ev": "leida", "p": "A.md"}) + "\n", encoding="utf-8")
        stats = TL.metrics(TL.read(tmp, days=1))
        self.assertEqual(stats["pointers_read"], 1)

    def test_can_be_disabled_and_drop_prompts(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        (tmp / ".vault-memory").mkdir()
        TL.record(tmp, {"ev": "hook", "q": "secret"}, {"telemetry": {"enabled": False}})
        self.assertFalse((tmp / ".vault-memory" / TL.FILE).exists())
        TL.record(tmp, {"ev": "hook", "q": "secret"}, {"telemetry": {"store_prompt": False}})
        event = json.loads((tmp / ".vault-memory" / TL.FILE).read_text(encoding="utf-8"))
        self.assertEqual(event["q"], "")


class TestHooksRecord(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        shutil.copytree(EXAMPLE, self.vault)
        self.env = {**os.environ, "CLAUDE_CONFIG_DIR": str(self.tmp), "VAULT_MEMORY_VAULT": str(self.vault)}

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def events(self):
        path = self.vault / ".vault-memory" / TL.FILE
        return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def test_hook_and_read(self):
        result = subprocess.run([sys.executable, "-B", str(REPO / "hooks" / "recall.py")], text=True, capture_output=True,
                                input=json.dumps({"prompt": "checkout went down because of the certificate, what now",
                                                  "session_id": "s9"}), env=self.env)
        out = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(out.startswith("[vault] "))
        emitted = self.events()[-1]
        self.assertEqual((emitted["ev"], emitted["sid"]), ("hook", "s9"))
        note = self.vault / emitted["emitted"][0]
        subprocess.run([sys.executable, "-B", str(REPO / "hooks" / "read.py")], text=True,
                       input=json.dumps({"tool_input": {"file_path": str(note)}, "session_id": "s9"}), env=self.env)
        self.assertEqual(TL.metrics(self.events())["pointers_read"], 1)

    def test_silence_is_recorded_with_its_reason(self):
        subprocess.run([sys.executable, "-B", str(REPO / "hooks" / "recall.py")], text=True, capture_output=True,
                       input=json.dumps({"prompt": "ok, go ahead with what I asked before and let me know",
                                         "session_id": "s1"}), env=self.env)
        last = self.events()[-1]
        self.assertEqual((last["ev"], last["reason"]), ("hook_silent", "conversational"))

    def test_compact_pointer_is_short(self):
        hits = [{"p": "Atlas/Runbooks/Runbook - Renew TLS certificate.md", "type": "runbook", "verified": "2026-08-01"},
                {"p": "Atlas/Incidents/Inc - 2026-03-02 - Checkout down due to expired certificate.md", "type": "incident"}]
        text = HF.pointer(hits, "compact")
        self.assertLessEqual(len(text), 240, text)
        self.assertIn("verified 2026-08", text)


class TestTokens(unittest.TestCase):
    def test_counts_hook_cli_and_vault_reads(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        vault = tmp / "vault"
        (vault / "Memory").mkdir(parents=True)
        (tmp / "projects" / "p").mkdir(parents=True)
        note = str(vault / "Memory" / "x.md")
        lines = [
            {"type": "attachment", "attachment": {"type": "hook_success", "hookName": "UserPromptSubmit",
                                                  "content": "[vault] Memory/x.md (doc)", "durationMs": "120"}},
            {"type": "attachment", "attachment": {"type": "hook_success", "hookName": "UserPromptSubmit",
                                                  "content": "another hook without a pointer"}},
            {"type": "assistant", "message": {"usage": {"input_tokens": 5, "cache_read_input_tokens": 100},
                                              "content": [{"type": "tool_use", "id": "a", "name": "Bash",
                                                           "input": {"command": "vault-memory recall \"x\" --brief"}},
                                                          {"type": "tool_use", "id": "b", "name": "Read",
                                                           "input": {"file_path": note}},
                                                          {"type": "tool_use", "id": "c", "name": "Read",
                                                           "input": {"file_path": "/elsewhere/file.md"}},
                                                          {"type": "tool_use", "id": "d", "name": "Bash",
                                                           "input": {"command": "ls vault-memoryx"}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "a", "content": "12345"},
                                                     {"type": "tool_result", "tool_use_id": "b",
                                                      "content": [{"type": "text", "text": "abcdefghij"}]},
                                                     {"type": "tool_result", "tool_use_id": "c", "content": "zzzz"},
                                                     {"type": "tool_result", "tool_use_id": "d", "content": "yyyy"}]}},
        ]
        (tmp / "projects" / "p" / "s.jsonl").write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
        cost = TL.tokens(tmp / "projects", vault, days=1)
        self.assertEqual((cost["pointers"], cost["recalls"], cost["reads"]), (1, 1, 1))
        self.assertEqual(cost["recall_bytes"], 5)
        self.assertEqual(cost["read_bytes"], 10)
        self.assertEqual(cost["hook_ms_median"], 120)
        self.assertEqual(cost["sessions_with_memory"], 1)


class TestBriefPassage(unittest.TestCase):
    def test_only_with_high_confidence_and_current(self):
        hook = HF.options({})
        one = [{"p": "a/x.md", "score": 30, "state": "ok", "passage": "text " * 80}]
        self.assertTrue(HF.brief_passage(one, hook).endswith("…"))
        self.assertLessEqual(len(HF.brief_passage(one, hook)), 241)
        doubtful = one + [{"p": "a/y.md", "score": 25, "state": "ok"}]
        self.assertEqual(HF.brief_passage(doubtful, hook), "")
        low = [{"p": "a/x.md", "score": 12, "state": "ok", "passage": "t"}]
        self.assertEqual(HF.brief_passage(low, hook), "")
        stale = [{"p": "a/x.md", "score": 30, "state": "unverified", "passage": "t"}]
        self.assertEqual(HF.brief_passage(stale, hook), "")


class TestBrief(unittest.TestCase):
    def test_one_line_per_note(self):
        # Own copy: in CI the example vault carries no index (the state folder is gitignored).
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        shutil.copytree(EXAMPLE, tmp / "v")
        result = subprocess.run([sys.executable, "-B", str(REPO / "scripts" / "cli.py"), "--vault", str(tmp / "v"),
                                 "recall", "duplicate refund", "--brief", "--n", "2"],
                                capture_output=True, text=True, env={**os.environ, "VAULT_MEMORY_ORIGIN": "hook"})
        lines = [x for x in result.stdout.splitlines() if x.strip()]
        self.assertEqual(len(lines), 2)
        self.assertIn("Duplicate refund", lines[0])
        self.assertLessEqual(len(lines[0].split("|", 1)[1]), 170)


if __name__ == "__main__":
    unittest.main()
