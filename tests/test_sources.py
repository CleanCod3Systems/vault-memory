"""Sources and ingest: Codex, Gemini and GitHub PR adapters, the `distill.sources` switch and
`vault-memory ingest`. Synthetic files only; the provider is a fake callable and `gh` is mocked."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "tests"))
import distill as DS  # noqa: E402
import ingest as IG  # noqa: E402
import sources as SR  # noqa: E402
from test_distill import DAY, QUOTE, Env, FakeProvider, candidate, stamp  # noqa: E402


# ───────────────────────────────────────────────────────────── fixtures
def codex_line(kind, payload, day=DAY, hour=12, minute=0):
    return {"timestamp": stamp(day, hour, minute), "type": kind, "payload": payload}


def codex_msg(role, text, minute=0, kind="input_text", **extra):
    return codex_line("response_item", {"type": "message", "role": role,
                                        "content": [{"type": kind, "text": text}], **extra}, minute=minute)


def codex_session(sid="0199aaaa-1111-7000-8000-000000000001", source="cli", cwd="/work/billing-api"):
    return [
        codex_line("session_meta", {"id": sid, "timestamp": stamp(DAY), "cwd": cwd, "originator": "codex_cli_rs",
                                    "cli_version": "0.0.0", "source": source}),
        codex_line("turn_context", {"cwd": cwd, "model": "fake-model"}),
        codex_msg("developer", "<permissions instructions>sandbox rules</permissions instructions>"),
        codex_msg("user", "# AGENTS.md instructions for /work/billing-api\n\nproject rules"),
        codex_msg("user", "<environment_context>\n  <cwd>/work/billing-api</cwd>\n</environment_context>"),
        codex_msg("user", "How should billing-api retry failed charges?", minute=1),
        codex_line("response_item", {"type": "reasoning", "summary": [{"type": "summary_text", "text": "hidden thought"}]}),
        codex_line("response_item", {"type": "function_call", "name": "shell", "arguments": "{}", "call_id": "c1"}),
        codex_line("response_item", {"type": "function_call_output", "call_id": "c1", "output": "tool output that must not count"}),
        codex_msg("assistant", "Looking at the retry module.", minute=2, kind="output_text", phase="commentary"),
        codex_msg("assistant", "Retry 3 times with backoff, then park the charge.", minute=3, kind="output_text",
                  phase="final_answer"),
        codex_line("event_msg", {"type": "token_count", "info": None}),
        codex_msg("user", "Decision: " + QUOTE + ".", minute=4),
        codex_msg("assistant", "Noted.", minute=5, kind="output_text", phase="final_answer"),
    ]


def gemini_meta(sid="5e551011-aaaa-4bbb-8ccc-000000000001", kind="main"):
    return {"sessionId": sid, "projectHash": "f" * 64, "startTime": stamp(DAY), "lastUpdated": stamp(DAY), "kind": kind}


def gemini_msg(mid, kind, content, minute=0, **extra):
    return {"id": mid, "timestamp": stamp(DAY, 12, minute), "type": kind, "content": content, **extra}


class SourcesEnv(Env):
    def setUp(self):
        super().setUp()
        self.home = self.tmp / "home"
        self.home.mkdir()
        os.environ["HOME"] = str(self.home)
        os.environ.pop("CODEX_HOME", None)

    def write_jsonl(self, path, records):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
        return path

    def codex_file(self, records, day=DAY, name="rollout-2026-11-09T12-00-00-0199aaaa.jsonl"):
        y, m, d = day.split("-")
        return self.write_jsonl(self.home / ".codex" / "sessions" / y / m / d / name, records)

    def gemini_file(self, records, project="billing-api", name="session-2026-11-09T12-00-5e551011.jsonl"):
        return self.write_jsonl(self.home / ".gemini" / "tmp" / project / "chats" / name, records)

    def read(self, *sources, **distill):
        return DS.read_sessions(self.cfg(sources=list(sources), **distill), DAY)


# ───────────────────────────────────────────────────────────── adapters
class TestSwitch(SourcesEnv):
    def test_default_is_claude_code_only(self):
        self.codex_file(codex_session())
        self.assertEqual(SR.options(self.cfg())["sources"], ["claude-code"])
        self.assertEqual(DS.read_sessions(self.cfg(), DAY), [])

    def test_missing_folders_yield_nothing(self):
        self.assertEqual(self.read("claude-code", "codex", "gemini", "gh-prs"), [])

    def test_unknown_source_is_a_clear_error(self):
        with self.assertRaisesRegex(DS.DistillError, "unknown distill source 'slack'"):
            self.read("slack")


class TestCodex(SourcesEnv):
    def test_reads_human_prompts_and_final_replies_only(self):
        self.codex_file(codex_session())
        [session] = self.read("codex")
        self.assertEqual(session["sid"], "0199aaaa-1111-7000-8000-000000000001")
        self.assertEqual((session["cwd"], session["origin"], session["ref"]),
                         ("/work/billing-api", "codex", "codex:0199aaaa"))
        self.assertEqual(session["prompts"], ["How should billing-api retry failed charges?",
                                              "Decision: " + QUOTE + "."])
        self.assertIn("[assistant] Retry 3 times with backoff", session["text"])
        for absent in ("Looking at the retry module", "tool output", "hidden thought", "project rules",
                       "environment_context", "sandbox rules"):
            self.assertNotIn(absent, session["text"])

    def test_skips_subagent_sessions_and_far_away_folders(self):
        self.codex_file(codex_session(source={"subagent": "review"}))
        self.codex_file(codex_session(sid="0199bbbb-0000"), day="2026-10-01", name="rollout-x-0199bbbb.jsonl")
        self.assertEqual(self.read("codex"), [])

    def test_codex_home_is_honored(self):
        os.environ["CODEX_HOME"] = str(self.tmp / "elsewhere")
        self.write_jsonl(self.tmp / "elsewhere" / "sessions" / "2026" / "11" / "09" / "rollout-a.jsonl", codex_session())
        self.assertEqual(len(self.read("codex")), 1)


class TestGemini(SourcesEnv):
    def records(self):
        return [
            gemini_meta(),
            gemini_msg("m0", "user", [{"text": "<session_context>\nworkspace data\n</session_context>"}]),
            gemini_msg("m1", "user", [{"text": "How should billing-api retry failed charges?\n--- file ---\nexpanded"}],
                       minute=1, displayContent="How should billing-api retry failed charges?"),
            gemini_msg("m2", "gemini", "", minute=2, toolCalls=[{"id": "t1", "name": "read_file"}]),
            gemini_msg("m3", "user", [{"functionResponse": {"name": "read_file", "response": {"output": "tool output"}}}],
                       minute=2),
            gemini_msg("m4", "gemini", "Retry 3 times with backoff, then park the charge.", minute=3),
            gemini_msg("m5", "info", "Request cancelled.", minute=3),
            gemini_msg("m6", "user", [{"text": "a prompt that was rewound"}], minute=4),
            {"$rewindTo": "m6"},
            gemini_msg("m7", "user", [{"text": "Decision: " + QUOTE + "."}], minute=5),
            gemini_msg("m8", "gemini", "Noted.", minute=6),
            {"$set": {"lastUpdated": stamp(DAY, 12, 6)}},
        ]

    def test_reads_jsonl_with_rewinds_and_skips_tool_and_injected_messages(self):
        path = self.gemini_file(self.records())
        (path.parent.parent / ".project_root").write_text("/work/billing-api", encoding="utf-8")
        [session] = self.read("gemini")
        self.assertEqual((session["sid"], session["cwd"], session["origin"]),
                         ("5e551011-aaaa-4bbb-8ccc-000000000001", "/work/billing-api", "gemini"))
        self.assertEqual(session["prompts"], ["How should billing-api retry failed charges?",
                                              "Decision: " + QUOTE + "."])
        self.assertIn("Retry 3 times with backoff", session["text"])
        for absent in ("expanded", "tool output", "workspace data", "Request cancelled", "rewound"):
            self.assertNotIn(absent, session["text"])

    def test_reads_the_legacy_single_json_file(self):
        legacy = {**gemini_meta(), "messages": [m for m in self.records()[1:] if "id" in m and m["id"] != "m6"]}
        path = self.home / ".gemini" / "tmp" / ("0" * 64) / "chats" / "session-2026-11-09T12-00-5e551011.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(legacy), encoding="utf-8")
        [session] = self.read("gemini")
        self.assertEqual(len(session["prompts"]), 2)

    def test_skips_subagent_chats(self):
        self.gemini_file([gemini_meta(kind="subagent")] + self.records()[1:])
        self.assertEqual(self.read("gemini"), [])


class TestGhPrs(SourcesEnv):
    PR = {"number": 12, "title": "Park charges after retries", "url": "https://github.com/acme/billing/pull/12",
          "author": {"login": "someone"}, "body": "Charges are parked after 3 retries.",
          "comments": [{"author": {"login": "other"}, "body": "Why 3?", "createdAt": "2026-11-09T10:00:00Z"}],
          "reviews": [{"author": {"login": "someone"}, "body": "Decision: " + QUOTE + ".", "state": "COMMENTED",
                       "submittedAt": "2026-11-09T11:00:00Z"},
                      {"author": {"login": "other"}, "body": "", "state": "APPROVED", "submittedAt": "2026-11-09T12:00:00Z"}]}

    def fake_gh(self, args, **kwargs):
        self.calls.append(args)
        if args[1:3] == ["search", "prs"]:
            out = [{"number": 12, "repository": {"nameWithOwner": "acme/billing"}, "url": self.PR["url"]}]
        else:
            out = self.PR
        return mock.Mock(returncode=0, stdout=json.dumps(out), stderr="")

    def setUp(self):
        super().setUp()
        self.calls = []

    def test_turns_authored_and_reviewed_prs_into_one_session_each(self):
        with mock.patch.object(SR.shutil, "which", return_value="/usr/bin/gh"), \
                mock.patch.object(SR.subprocess, "run", side_effect=self.fake_gh):
            [session] = self.read("gh-prs", gh_repos=["acme/billing"])
        searches = [c for c in self.calls if c[1:3] == ["search", "prs"]]
        self.assertEqual([c[3] for c in searches], ["--author", "--reviewed-by"])
        self.assertTrue(all(["--updated", DAY] == c[5:7] and c[-2:] == ["--repo", "acme/billing"] for c in searches))
        self.assertEqual(len([c for c in self.calls if c[1:3] == ["pr", "view"]]), 1)   # deduplicated
        self.assertEqual((session["sid"], session["origin"], session["url"]),
                         ("acme/billing#12", "gh-prs", self.PR["url"]))
        self.assertTrue(session["text"].startswith("[pr] acme/billing#12 Park charges after retries"))
        self.assertIn("[reviewer] Why 3?", session["text"])
        self.assertIn("[author] Decision: " + QUOTE, session["text"])
        self.assertNotIn("someone", session["text"])     # people by role, never by login

    def test_without_gh_or_repos_nothing_runs(self):
        with mock.patch.object(SR.shutil, "which", return_value=None), \
                mock.patch.object(SR.subprocess, "run", side_effect=AssertionError("gh must not run")):
            self.assertEqual(self.read("gh-prs", gh_repos=["acme/billing"]), [])
        with mock.patch.object(SR.shutil, "which", return_value="/usr/bin/gh"), \
                mock.patch.object(SR.subprocess, "run", side_effect=AssertionError("gh must not run")):
            self.assertEqual(self.read("gh-prs"), [])

    def test_a_failing_gh_yields_nothing(self):
        with mock.patch.object(SR.shutil, "which", return_value="/usr/bin/gh"), \
                mock.patch.object(SR.subprocess, "run", return_value=mock.Mock(returncode=1, stdout="", stderr="auth")):
            self.assertEqual(self.read("gh-prs", gh_repos=["acme/billing"]), [])


class TestDistillFromOtherSources(SourcesEnv):
    def test_candidate_records_where_it_came_from(self):
        self.codex_file(codex_session())
        result = DS.run(self.cfg(sources=["codex"]), day=DAY, provider=FakeProvider([candidate()]))
        self.assertEqual(result["kept"], 1)
        [file] = sorted((self.vault / ".vault-memory" / "inbox").glob("*.md"))
        text = file.read_text(encoding="utf-8")
        self.assertIn("source: distill 2026-11-09 codex:0199aaaa", text)
        self.assertIn("origin: codex", text)
        self.assertTrue(file.name.startswith("2026-11-09-codex-0199aaaa-1-"))


# ───────────────────────────────────────────────────────────── ingest
THREAD = {"source": "chat", "id": "C01-1731148800.000100", "url": "https://chat.example.com/archives/C01/p1731148800",
          "date": DAY, "title": "Billing retries",
          "messages": [{"author_role": "teammate", "text": "Mail jane@example.com about billing-api retries",
                        "ts": "2026-11-09T10:00:00Z"},
                       {"author_role": "user", "text": "Decision: " + QUOTE + ".", "ts": "2026-11-09T10:05:00Z"}]}


class TestMaterial(unittest.TestCase):
    def test_json_shape(self):
        session = SR.material(json.dumps(THREAD))
        self.assertEqual((session["origin"], session["sid"], session["day"], session["url"]),
                         ("chat", THREAD["id"], DAY, THREAD["url"]))
        self.assertEqual(session["ref"], "chat:" + THREAD["id"])
        self.assertEqual(session["messages"][0], ("title", "Billing retries"))
        self.assertEqual(session["messages"][1][0], "teammate")

    def test_flags_win_and_date_falls_back_to_the_first_message(self):
        data = {**THREAD, "date": None}
        session = SR.material(json.dumps(data), source="issues", ref_id="ABC-1", url="https://x.example/ABC-1")
        self.assertEqual((session["origin"], session["sid"], session["url"]), ("issues", "ABC-1", "https://x.example/ABC-1"))
        self.assertEqual(session["day"], SR._local_day("2026-11-09T10:00:00Z"))

    def test_markdown_needs_a_source_and_gets_a_stable_id(self):
        md = "# Retry policy\n\nDecision: " + QUOTE + ".\n"
        with self.assertRaisesRegex(ValueError, "source"):
            SR.material(md)
        one, two = SR.material(md, source="notes"), SR.material(md, source="notes")
        self.assertEqual(one["sid"], two["sid"])
        self.assertEqual(one["messages"], [("title", "Retry policy"), ("document", md.strip())])

    def test_invalid_material(self):
        for raw in ("{not json", "[]", json.dumps({**THREAD, "messages": []}), "   "):
            with self.assertRaises(ValueError, msg=raw):
                SR.material(raw, source="chat")
        with self.assertRaisesRegex(ValueError, "date"):
            SR.material(json.dumps({**THREAD, "date": "yesterday"}))


class TestIngest(Env):
    def run_ingest(self, raw=None, cands=None, **distill):
        provider = FakeProvider([candidate()] if cands is None else cands)
        session = SR.material(raw or json.dumps(THREAD))
        return IG.run(self.cfg(**distill), session, provider=provider), provider

    def test_redacts_before_the_provider_and_keeps_provenance_in_the_inbox(self):
        result, provider = self.run_ingest()
        self.assertEqual((result["proposed"], result["kept"], result["source"]), (1, 1, "chat:" + THREAD["id"]))
        self.assertNotIn("jane@example.com", "".join(provider.prompts))
        self.assertIn("[REDACTED:email]", provider.prompts[0])
        [file] = sorted((self.vault / ".vault-memory" / "inbox").glob("*.md"))
        text = file.read_text(encoding="utf-8")
        self.assertIn(f"source: distill {DAY} chat:{THREAD['id']}", text)
        self.assertIn("origin: chat", text)
        self.assertIn(f"url: {THREAD['url']}", text)
        event = json.loads((self.vault / ".vault-memory" / "telemetry.jsonl").read_text().splitlines()[-1])
        self.assertEqual((event["ev"], event["origin"], event["kept"]), ("ingest", "chat", 1))

    def test_evidence_must_be_in_the_material(self):
        result, _ = self.run_ingest(cands=[candidate(evidence="something never said")])
        self.assertEqual(result["discarded_reasons"], {"evidence_not_found": 1})

    def test_does_not_need_distill_enabled_and_ingesting_twice_is_idempotent(self):
        cfg = self.cfg()
        cfg["distill"]["enabled"] = False
        for _ in range(2):
            result = IG.run(cfg, SR.material(json.dumps(THREAD)), provider=FakeProvider([candidate()]))
        self.assertEqual(result["discarded_reasons"], {"already_in_inbox": 1})

    def test_auto_writes_the_note_with_origin_and_url(self):
        self.run_ingest(auto=True)
        text = (self.vault / "Memory" / "learned" / "Failed charges are parked after 3 retries.md").read_text(encoding="utf-8")
        self.assertIn(f"source: distill {DAY} chat:{THREAD['id']}", text)
        self.assertIn("origin: chat", text)
        self.assertIn(f"url: {THREAD['url']}", text)
        self.assertIn("expires: 2027-02-07", text)

    def test_earned_without_audits_goes_to_the_inbox(self):
        result, _ = self.run_ingest(auto="earned")
        self.assertEqual(result["write"]["to"], "inbox")
        self.assertFalse((self.vault / "Memory" / "learned" / "Failed charges are parked after 3 retries.md").exists())
        self.assertEqual(len(list((self.vault / ".vault-memory" / "inbox").glob("*.md"))), 1)

    def test_dry_run_writes_nothing(self):
        session = SR.material(json.dumps(THREAD))
        result = IG.run(self.cfg(), session, provider=FakeProvider([candidate()]), dry_run=True)
        self.assertEqual(result["kept"], 1)
        self.assertFalse((self.vault / ".vault-memory" / "inbox").exists())


class TestIngestCli(Env):
    def cli(self, *args, stdin=""):
        return subprocess.run([sys.executable, "-B", str(REPO / "scripts" / "cli.py"), "--vault", str(self.vault),
                               "ingest", *args], input=stdin, capture_output=True, text=True, env=dict(os.environ))

    def test_bad_material_exits_2_before_any_model_call(self):
        out = self.cli("--file", "-", stdin="# notes\n\nsome text\n")
        self.assertEqual(out.returncode, 2)
        self.assertIn("source", out.stderr)
        self.assertEqual(self.cli("--file", str(self.tmp / "missing.json")).returncode, 2)


if __name__ == "__main__":
    unittest.main()
