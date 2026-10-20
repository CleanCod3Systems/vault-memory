"""The UserPromptSubmit hook: pointers when there is an answer, silence otherwise."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HOOK = REPO / "hooks" / "recall.py"
EXAMPLE = REPO / "tests" / "example_vault"
ENV_KEYS = ("VAULT_MEMORY_VAULT", "CEREBRO_VAULT")


class TestHook(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        shutil.copytree(EXAMPLE, self.vault)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def hook(self, prompt, vault=True, hook_config=None, session_id=None, profile_vault=None, env_vault=None):
        if hook_config is not None:
            config_file = self.vault / ".vault-memory" / "config.json"
            config = json.loads(config_file.read_text(encoding="utf-8"))
            config["hook"] = hook_config
            config_file.write_text(json.dumps(config), encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k not in ENV_KEYS}
        profile = self.tmp / "profile"
        profile.mkdir(exist_ok=True)
        if profile_vault:
            (profile / "settings.json").write_text(json.dumps({"env": {"VAULT_MEMORY_VAULT": str(profile_vault)}}))
        env["CLAUDE_CONFIG_DIR"] = str(profile)
        if vault:
            env["VAULT_MEMORY_VAULT"] = str(env_vault or self.vault)
        payload = {"prompt": prompt, **({"session_id": session_id} if session_id else {})}
        result = subprocess.run([sys.executable, "-B", str(HOOK)], input=json.dumps(payload),
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_pointer_when_there_is_an_answer(self):
        out = self.hook("checkout went down because of the certificate, what do we do")
        self.assertIn("[vault]", out)
        self.assertIn("expired certificate", out)

    def test_spanish_prompt_on_english_vault_still_finds_ids(self):
        out = self.hook("qué pasó con el pedido 48213")
        self.assertIn("Payments declined by antifraud", out)

    def test_silent_on_conversational_prompts(self):
        for prompt in ("ok, go ahead with what I asked before and let me know",
                       "dale, seguí con lo que te pedí antes y avisame",
                       "thanks, see you tomorrow"):
            self.assertEqual(self.hook(prompt), "", prompt)

    def test_skips_automatic_prompts(self):
        notice = "<task-notification> <task-id>b1</task-id> checkout went down because of the certificate 12345"
        self.assertEqual(self.hook(notice), "")
        config = {"skip_prefixes": ["AUTOMATIC MODE"]}
        self.assertEqual(self.hook("AUTOMATIC MODE: checkout went down because of the certificate",
                                   hook_config=config), "")

    def test_profile_vault_beats_environment(self):
        # A project settings.json can override the environment with another profile's vault:
        # the active profile settings.json must win.
        other = self.tmp / "other"
        other.mkdir()
        self.assertIn("[vault]", self.hook("checkout went down because of the certificate",
                                           env_vault=other, profile_vault=self.vault))
        self.assertEqual(self.hook("checkout went down because of the certificate",
                                   env_vault=self.vault, profile_vault=other), "")

    def test_without_vault_does_nothing(self):
        self.assertEqual(self.hook("checkout went down because of the certificate", vault=False), "")

    def test_skip_paths_from_config(self):
        # The daily mentions everything; recall.skip_paths takes it out of the pointers.
        out = self.hook("checkout payments refund stock login search deploy")
        self.assertNotIn("Atlas/Daily/", out)

    def test_domain_filter_from_config(self):
        config = {"domain": r"\b(order|payment)", "memory_only": ""}
        self.assertEqual(self.hook("checkout went down because of the certificate", hook_config=config), "")
        self.assertIn("[vault]", self.hook("payments declined by the antifraud rule", hook_config=config))

    def test_json_additional_context(self):
        out = json.loads(self.hook("checkout went down because of the certificate, what do we do"))
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertIn("[vault]", out["hookSpecificOutput"]["additionalContext"])

    def test_does_not_repeat_the_same_pointer(self):
        query = "checkout went down because of the certificate, what do we do"
        self.assertTrue(self.hook(query, session_id="s1"))
        self.assertEqual(self.hook(query, session_id="s1"), "", "same set as the previous pointer")
        self.assertTrue(self.hook(query, session_id="other"), "another session does not inherit the dedupe")

    def test_stops_suggesting_what_nobody_reads(self):
        config = {"repeat_max": 2}
        prompts = ["checkout went down because of the certificate, what do we do",
                   "renew the checkout TLS certificate, how was it",
                   "expired certificate on the checkout load balancer again"]
        outputs = [self.hook(q, session_id="s2", hook_config=config) for q in prompts]
        texts = [json.loads(x)["hookSpecificOutput"]["additionalContext"] if x else "" for x in outputs]
        times = sum("expired certificate.md" in t for t in texts)
        self.assertLessEqual(times, 2)


if __name__ == "__main__":
    unittest.main()
