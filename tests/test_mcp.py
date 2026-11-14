"""The MCP server speaks the protocol: initialize → tools/list → tools/call."""
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


class TestMCP(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        shutil.copytree(EXAMPLE, self.vault)
        self.proc = subprocess.Popen([sys.executable, "-B", str(REPO / "scripts" / "cli.py"), "mcp"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, env={**os.environ, "CLAUDE_CONFIG_DIR": str(self.tmp),
                                                     "VAULT_MEMORY_VAULT": str(self.vault),
                                                     "VAULT_MEMORY_TODAY": "2026-10-01"})
        self.count = 0

    def tearDown(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=10)
        self.proc.stdout.close()
        self.proc.stderr.close()
        shutil.rmtree(self.tmp)

    def call(self, method, params=None, notification=False):
        message = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notification:
            self.count += 1
            message["id"] = self.count
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()
        if notification:
            return None
        response = json.loads(self.proc.stdout.readline())
        self.assertEqual(response["id"], self.count)
        return response

    def tool(self, name, args):
        result = self.call("tools/call", {"name": name, "arguments": args})["result"]
        return json.loads(result["content"][0]["text"]), result["isError"]

    def test_full_cycle(self):
        result = self.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                          "clientInfo": {"name": "test", "version": "0"}})["result"]
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        self.assertIn("tools", result["capabilities"])
        self.assertIn("user", result["instructions"])
        self.call("notifications/initialized", notification=True)
        names = {t["name"] for t in self.call("tools/list")["result"]["tools"]}
        self.assertEqual(names, {"recall", "read_passage", "write_note", "doctor", "flag_for_review",
                                 "save_answer"})

        rows, error = self.tool("recall", {"q": "duplicate refund"})
        self.assertFalse(error)
        self.assertIn("Duplicate refund", rows[0]["p"])

        passage, error = self.tool("read_passage", {"p": rows[0]["p"], "q": "idempotency"})
        self.assertFalse(error)
        self.assertIn("Idempotency", passage["text"])

        duplicate, error = self.tool("write_note", {
            "title": "Request prod access", "type": "doc", "folder": "Memory",
            "body": "To request production access you have to open a ticket to the platform team "
                    "with the justification and the duration."})
        self.assertTrue(error)
        self.assertTrue(duplicate["duplicates"])

        report, error = self.tool("doctor", {})
        self.assertFalse(error)
        self.assertEqual(report["totals"]["contradictions"], 1)

    def test_path_outside_the_vault(self):
        self.call("initialize", {"protocolVersion": "2025-06-18"})
        _, error = self.tool("read_passage", {"p": "../../etc/passwd"})
        self.assertTrue(error)

    def test_unknown_method(self):
        self.assertEqual(self.call("does/not-exist")["error"]["code"], -32601)

    def test_unknown_tool(self):
        response = self.call("tools/call", {"name": "nope", "arguments": {}})
        self.assertEqual(response["error"]["code"], -32602)

    def test_ask_and_discuss_prompts(self):
        result = self.call("initialize", {"protocolVersion": "2025-06-18"})["result"]
        self.assertIn("prompts", result["capabilities"])
        prompts = {p["name"]: p for p in self.call("prompts/list")["result"]["prompts"]}
        self.assertEqual(set(prompts), {"ask", "discuss"})
        self.assertTrue(prompts["discuss"]["arguments"][0]["required"])
        got = self.call("prompts/get", {"name": "discuss", "arguments": {"claim": "refunds never use queues"}})
        text = got["result"]["messages"][0]["content"]["text"]
        self.assertIn("refunds never use queues", text)
        self.assertIn("flag_for_review", text)
        self.assertEqual(self.call("prompts/get", {"name": "ask", "arguments": {}})["error"]["code"], -32602)
        self.assertEqual(self.call("prompts/get", {"name": "nope"})["error"]["code"], -32602)

    def test_recall_says_how_sure_and_a_dispute_is_flagged(self):
        self.call("initialize", {"protocolVersion": "2025-06-18"})
        rows, _ = self.tool("recall", {"q": "duplicate refund"})
        self.assertIn("confidence", rows[0])
        self.assertIn("verified", rows[0])
        flagged, error = self.tool("flag_for_review", {"p": rows[0]["p"], "reason": "user says it changed"})
        self.assertFalse(error)
        self.assertEqual(flagged["origin"], "mcp")
        lines = (self.vault / ".vault-memory" / "flags.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(json.loads(lines[-1])["p"], rows[0]["p"])
        _, error = self.tool("flag_for_review", {"p": "../outside.md", "reason": "x"})
        self.assertTrue(error)

    def test_save_answer_writes_the_note_only_with_enough_cites(self):
        result = self.call("initialize", {"protocolVersion": "2025-06-18"})["result"]
        self.assertIn("save_answer", result["instructions"])
        refund = "Atlas/Incidents/Inc - 2026-06-18 - Duplicate refund to customer.md"
        queues = "Atlas/Decisions/Decision - 2026-02-10 - Use queues for refunds.md"
        saved, error = self.tool("save_answer", {"question": "How do we avoid paying a refund twice?",
                                                 "answer": "A queue and an idempotency key.",
                                                 "cites": [refund, queues]})
        self.assertFalse(error)
        text = (self.vault / saved["p"]).read_text(encoding="utf-8")
        self.assertTrue(saved["p"].startswith("Memory/answers/"))
        self.assertIn("source: ask 2026-10-01", text)
        self.assertNotIn("verified:", text)
        response = self.call("tools/call", {"name": "save_answer", "arguments": {
            "question": "Another question", "answer": "Another answer.", "cites": [refund]}})
        self.assertEqual(response["error"]["code"], -32602)
        duplicate, error = self.tool("save_answer", {"question": "How do we avoid paying a refund twice?",
                                                     "answer": "A queue and an idempotency key.",
                                                     "cites": [refund, queues]})
        self.assertTrue(error)
        self.assertEqual(duplicate["code"], 3)


if __name__ == "__main__":
    unittest.main()
