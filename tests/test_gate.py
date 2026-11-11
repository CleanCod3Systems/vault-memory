"""The PreToolUse gate: one line when the vault already documents what an external search is
about to look for, silence otherwise. It never blocks and never asks for permission."""
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GATE = REPO / "hooks" / "gate.py"
EXAMPLE = REPO / "tests" / "example_vault"
ENV_KEYS = ("VAULT_MEMORY_VAULT", "CEREBRO_VAULT")
STRONG = "checkout went down because the TLS certificate expired"
INCIDENT = "Atlas/Incidents/Inc - 2026-03-02 - Checkout down due to expired certificate.md"


class TestGate(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        shutil.copytree(EXAMPLE, self.vault)
        # the gate reads the index and never builds it; a fresh checkout has none (it is ignored)
        built = subprocess.run([sys.executable, "-B", str(REPO / "scripts" / "cli.py"), "--vault",
                                str(self.vault), "index"], capture_output=True, text=True,
                               env={**os.environ, "CLAUDE_CONFIG_DIR": str(self.tmp / "profile")})
        self.assertEqual(built.returncode, 0, built.stderr)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def configure(self, gate):
        config_file = self.vault / ".vault-memory" / "config.json"
        config = json.loads(config_file.read_text(encoding="utf-8"))
        config["gate"] = gate
        config_file.write_text(json.dumps(config), encoding="utf-8")

    def run_gate(self, stdin, vault=True):
        env = {k: v for k, v in os.environ.items() if k not in ENV_KEYS}
        profile = self.tmp / "profile"
        profile.mkdir(exist_ok=True)
        env["CLAUDE_CONFIG_DIR"] = str(profile)
        if vault:
            env["VAULT_MEMORY_VAULT"] = str(self.vault)
        result = subprocess.run([sys.executable, "-B", str(GATE)], input=stdin,
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def gate(self, tool_name="mcp__logs__search", tool_input=None, sid="s1", **kwargs):
        payload = {"hook_event_name": "PreToolUse", "tool_name": tool_name,
                   "tool_input": {"reason": STRONG} if tool_input is None else tool_input,
                   "session_id": sid}
        return self.run_gate(json.dumps(payload), **kwargs)

    def events(self):
        path = self.vault / ".vault-memory" / "telemetry.jsonl"
        return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]

    def test_without_gate_config_it_does_nothing(self):
        self.assertEqual(self.gate(), "")

    def test_tool_not_listed_is_silent(self):
        self.configure({"tools": {"mcp__logs__search": "reason"}})
        self.assertEqual(self.gate(tool_name="mcp__db__query"), "")

    def test_listed_tool_with_a_strong_note_adds_one_line_and_never_decides(self):
        self.configure({"tools": {"mcp__logs__search": "reason"}})
        out = json.loads(self.gate())
        self.assertEqual(list(out), ["hookSpecificOutput"])
        data = out["hookSpecificOutput"]
        self.assertEqual(data["hookEventName"], "PreToolUse")
        self.assertNotIn("permissionDecision", data)
        line = data["additionalContext"]
        self.assertTrue(line.startswith(f"[vault] already documented: {INCIDENT} — "), line)
        self.assertNotIn("\n", line)
        self.assertLessEqual(len(line.split(" — ", 1)[1]), 200)
        self.assertIn("certificate", line.split(" — ", 1)[1])

    def test_prefix_and_all_strings_field(self):
        self.configure({"tools": {"mcp__logs__": "*"}})
        out = self.gate(tool_name="mcp__logs__anything",
                        tool_input={"index": "web", "query": STRONG, "limit": 5})
        self.assertIn(INCIDENT, out)

    def test_weak_query_is_silent_and_recorded_as_miss(self):
        self.configure({"tools": {"mcp__logs__search": "reason"}})
        self.assertEqual(self.gate(tool_input={"reason": "how many orders came in this hour"}), "")
        miss = [e for e in self.events() if e["ev"] == "gate_miss"]
        self.assertEqual(len(miss), 1)
        self.assertEqual(miss[0]["tool"], "mcp__logs__search")

    def test_expired_note_is_never_suggested(self):
        # floor 0 on purpose: what keeps it out is its state, not the threshold
        self.configure({"tools": {"mcp__logs__search": "reason"}, "floor": 0})
        out = self.gate(tool_input={"reason": "temporary shipping provider until end of June"})
        self.assertNotIn("Temporary shipping provider", out)

    def test_same_note_is_not_repeated_in_a_session(self):
        self.configure({"tools": {"mcp__logs__search": "reason"}})
        self.assertIn(INCIDENT, self.gate(sid="a"))
        self.assertEqual(self.gate(sid="a"), "")
        self.assertIn(INCIDENT, self.gate(sid="b"))
        hits = [e for e in self.events() if e["ev"] == "gate_hit"]
        self.assertEqual([(e["sid"], e["p"], e["tool"]) for e in hits],
                         [("a", INCIDENT, "mcp__logs__search"), ("b", INCIDENT, "mcp__logs__search")])
        self.assertLessEqual(len(hits[0]["q"]), 200)

    def test_broken_input_exits_zero_without_output(self):
        self.configure({"tools": {"mcp__logs__search": "reason"}})
        for stdin in ("", "not json", "[]", "null", '{"tool_name": "mcp__logs__search"}',
                      '{"tool_name": "mcp__logs__search", "tool_input": [1, 2]}',
                      '{"tool_name": "mcp__logs__search", "tool_input": {"reason": 7}}'):
            self.assertEqual(self.run_gate(stdin), "", stdin)

    def test_broken_config_or_no_vault_is_silent(self):
        self.configure({"tools": ["not", "a", "dict"]})
        self.assertEqual(self.gate(), "")
        self.configure({"tools": {"mcp__logs__search": "reason"}})
        self.assertEqual(self.gate(vault=False), "")
        (self.vault / ".vault-memory" / "index.json").unlink()
        self.assertEqual(self.gate(), "")   # no index: it does not rebuild one inside a tool call

    def test_latency_on_the_example_vault(self):
        self.configure({"tools": {"mcp__logs__search": "reason"}})
        times = []
        for i in range(20):
            start = time.perf_counter()
            self.gate(sid=f"lat{i}")
            times.append((time.perf_counter() - start) * 1000)
        times.sort()
        p95 = times[int(len(times) * 0.95) - 1]
        print(f"\ngate latency ms: p50={statistics.median(times):.0f} p95={p95:.0f}", file=sys.stderr)
        # Target p95 < 300 ms. Only the median is asserted: the p95 of a whole process (interpreter
        # start included) swings with the load of the machine running the tests.
        self.assertLess(statistics.median(times), 300)


if __name__ == "__main__":
    unittest.main()
