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
        self.assertEqual(names, {"recall", "read_passage", "write_note", "doctor"})

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


if __name__ == "__main__":
    unittest.main()
