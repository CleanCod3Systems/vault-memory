"""distill: redaction, transcript parsing, verification, apply modes, providers, confirm-by-use.
Invented data only; the provider is always a fake callable (no model, no network)."""
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import distill as DS  # noqa: E402


class TestRedaction(unittest.TestCase):
    def check(self, raw, kind):
        self.assertIn(kind, DS.find_sensitive(raw), raw)
        out = DS.redact(raw)
        self.assertNotEqual(out, raw)
        self.assertIn("[REDACTED", out)
        self.assertEqual(DS.find_sensitive(out), [], out)

    def test_email(self):
        self.check("write to jane@example.com please", "email")

    def test_phone(self):
        self.check("call +1 415 555 0132 now", "phone")
        self.check("call (415) 555-0132 now", "phone")

    def test_card_needs_luhn(self):
        self.check("card 4111 1111 1111 1111 expires", "card")
        self.assertEqual(DS.find_sensitive("order 1234 5678 9012 3456"), [])
        self.assertEqual(DS.redact("order 1234 5678 9012 3456"), "order 1234 5678 9012 3456")

    def test_tokens(self):
        self.check("key sk-test-00000000000000000000", "api-key")
        self.check("gh ghp_" + "0" * 36, "api-key")
        self.check("slack xoxb-0000000000-fake-token", "api-key")
        self.check("aws AKIA" + "0" * 16, "api-key")
        self.check("jwt eyJhbGciOiJIUzI1.eyJzdWIiOiJmYWtl.c2lnbmF0dXJl00", "jwt")
        self.check("Authorization: Bearer abcdef0123456789xyz", "bearer")

    def test_assignments(self):
        self.check("password=hunter2hunter2", "secret")
        self.check("DB_PASSWD = 'fake-value'", "secret")
        self.check('{"token": "fake-value-123"}', "secret")

    def test_long_hex_and_base64(self):
        self.check("hash " + "ab12" * 10, "secret-blob")
        self.check("blob Zm9vQmFyQmF6MDEyMzQ1Njc4OWFiY2RlZkdISUpLTE1O", "secret-blob")

    def test_plain_text_is_untouched(self):
        text = "billing-api retries 3 times on 2026-11-09; see ticket BIL-123 at 10:30 (v1.2.3)."
        self.assertEqual(DS.find_sensitive(text), [])
        self.assertEqual(DS.redact(text), text)


def stamp(day, hour=12, minute=0):
    """ISO timestamp (UTC, as Claude Code writes it) of a *local* date/time."""
    local = dt.datetime.fromisoformat(f"{day}T{hour:02d}:{minute:02d}:00").astimezone()
    return local.astimezone(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def user(text, day, hour=12, minute=0, sid="s1", cwd="/work/billing-api", **extra):
    return {"type": "user", "timestamp": stamp(day, hour, minute), "sessionId": sid, "cwd": cwd,
            "message": {"role": "user", "content": text}, **extra}


def assistant(text, day, hour=12, minute=1, sid="s1", cwd="/work/billing-api"):
    return {"type": "assistant", "timestamp": stamp(day, hour, minute), "sessionId": sid, "cwd": cwd,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def tool_result(day, sid="s1"):
    return {"type": "user", "timestamp": stamp(day, 12, 2), "sessionId": sid,
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                                      "content": "tool output that must not count"}]}}


class Env(unittest.TestCase):
    """Temp CLAUDE_CONFIG_DIR with fake transcripts and a temp copy of the example vault."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.config_dir = self.tmp / "claude"
        (self.config_dir / "projects" / "-work-billing-api").mkdir(parents=True)
        self.vault = self.tmp / "vault"
        shutil.copytree(REPO / "tests" / "example_vault", self.vault)
        old = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(old)))
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.config_dir)
        os.environ["VAULT_MEMORY_TODAY"] = "2026-11-10"
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        os.environ.pop("CLAUDE_SESSION_ID", None)

    def transcript(self, sid, events, project="-work-billing-api", name=None):
        folder = self.config_dir / "projects" / project
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{name or sid}.jsonl"
        path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
        return path

    def cfg(self, **distill):
        import core as C
        cfg = C.load_config(self.vault)
        cfg["distill"] = {"enabled": True, **distill}
        return cfg


DAY = "2026-11-09"


class TestSessions(Env):
    def test_extracts_human_prompts_and_last_assistant_text(self):
        self.transcript("aaaa1111-0000", [
            {"type": "mode", "mode": "normal"},
            user("How should billing-api retry failed charges?", DAY, sid="aaaa1111-0000"),
            assistant("draft thought", DAY, sid="aaaa1111-0000"),
            tool_result(DAY, sid="aaaa1111-0000"),
            assistant("Retry 3 times with backoff, then park the charge.", DAY, 12, 3, sid="aaaa1111-0000"),
            user("<system-reminder>ignore me</system-reminder>", DAY, 12, 4, sid="aaaa1111-0000"),
            user("<task-notification>done</task-notification>", DAY, 12, 5, sid="aaaa1111-0000"),
            user("Decision: we park charges after 3 retries.", DAY, 12, 6, sid="aaaa1111-0000"),
            assistant("Noted.", DAY, 12, 7, sid="aaaa1111-0000"),
        ])
        [session] = DS.read_sessions(self.cfg(), DAY)
        self.assertEqual(session["sid"], "aaaa1111-0000")
        self.assertEqual(session["cwd"], "/work/billing-api")
        self.assertEqual(session["prompts"], ["How should billing-api retry failed charges?",
                                              "Decision: we park charges after 3 retries."])
        self.assertIn("Retry 3 times with backoff", session["text"])
        self.assertNotIn("draft thought", session["text"])
        self.assertNotIn("tool output", session["text"])
        self.assertNotIn("ignore me", session["text"])

    def test_assigns_session_to_the_day_of_its_first_prompt(self):
        self.transcript("s-other", [user("one", "2026-11-08", sid="s-other"), user("two", "2026-11-09", sid="s-other")])
        self.transcript("s-day", [user("one", DAY, sid="s-day"), user("two", DAY, 12, 5, sid="s-day")])
        self.assertEqual([s["sid"] for s in DS.read_sessions(self.cfg(), DAY)], ["s-day"])

    def test_skips_excluded_cwd_few_prompts_and_subagent_files(self):
        self.transcript("s-excl", [user("a", DAY, sid="s-excl", cwd="/secret/proj/x"),
                                   user("b", DAY, 12, 5, sid="s-excl", cwd="/secret/proj/x")])
        self.transcript("s-one", [user("only one", DAY, sid="s-one")])
        self.transcript("s-ok", [user("a", DAY, sid="s-ok"), user("b", DAY, 12, 5, sid="s-ok")])
        sub = self.config_dir / "projects" / "-work-billing-api" / "s-ok" / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-1.jsonl").write_text(json.dumps(user("a", DAY, sid="x")) + "\n", encoding="utf-8")
        cfg = self.cfg(exclude_paths=["/secret/proj"], min_prompts=2)
        self.assertEqual([s["sid"] for s in DS.read_sessions(cfg, DAY)], ["s-ok"])

    def test_truncates_keeping_the_end_and_redacts(self):
        self.transcript("s-long", [user("start " + "x " * 400 + "mail jane@example.com", DAY, sid="s-long"),
                                   user("the END marker", DAY, 12, 5, sid="s-long")])
        [session] = DS.read_sessions(self.cfg(max_chars=200), DAY)
        self.assertLessEqual(len(session["text"]), 200)
        self.assertTrue(session["text"].endswith("the END marker"))
        [full] = DS.read_sessions(self.cfg(), DAY)
        self.assertNotIn("jane@example.com", full["text"])


QUOTE = "we park charges after 3 retries"


def candidate(**over):
    base = {"title": "Failed charges are parked after 3 retries", "type": "decision",
            "key": "billing-retry-policy", "body": "Billing-api parks a charge after 3 failed retries.",
            "evidence": QUOTE}
    return {**base, **over}


class FakeProvider:
    def __init__(self, candidates=None, supported=True, relation=None):
        self.candidates, self.supported, self.prompts, self.calls = candidates or [], supported, [], 0
        self.relation = relation or {"match": 0, "relation": "different"}

    def __call__(self, prompt, schema):
        self.calls += 1
        self.prompts.append(prompt)
        if "supported" in schema["properties"]:
            return {"supported": self.supported}
        if "relation" in schema["properties"]:
            return self.relation
        return {"candidates": self.candidates}


class Pipeline(Env):
    def setUp(self):
        super().setUp()
        self.transcript("aaaa1111-0000", [
            user("Mail jane@example.com about billing-api retries", DAY, sid="aaaa1111-0000"),
            assistant("Ok.", DAY, sid="aaaa1111-0000"),
            user("Decision: " + QUOTE + ".", DAY, 12, 6, sid="aaaa1111-0000"),
        ])

    def inbox(self):
        folder = self.vault / ".vault-memory" / "inbox"
        return sorted(folder.glob("*.md")) if folder.exists() else []

    def vault_tree(self):
        return sorted(p.relative_to(self.vault).as_posix() for p in self.vault.rglob("*.md")
                      if ".vault-memory" not in p.parts)

    def telemetry(self):
        path = self.vault / ".vault-memory" / "telemetry.jsonl"
        return path.read_text(encoding="utf-8") if path.exists() else ""


class TestVerification(Pipeline):
    def run_with(self, cands, supported=True, **distill):
        provider = FakeProvider(cands, supported)
        result = DS.run(self.cfg(**distill), day=DAY, provider=provider)
        return result, provider

    def test_keeps_a_supported_candidate_in_the_inbox_only(self):
        before = self.vault_tree()
        result, provider = self.run_with([candidate()])
        self.assertEqual((result["sessions"], result["proposed"], result["kept"], result["discarded"]), (1, 1, 1, 0))
        self.assertEqual(self.vault_tree(), before)
        [file] = self.inbox()
        text = file.read_text(encoding="utf-8")
        self.assertIn("Failed charges are parked after 3 retries", text)
        self.assertIn(QUOTE, text)
        self.assertIn("aaaa1111", text)
        self.assertEqual(provider.calls, 2)

    def test_prompt_sent_to_the_provider_is_redacted(self):
        _, provider = self.run_with([])
        self.assertNotIn("jane@example.com", provider.prompts[0])
        self.assertIn("[REDACTED:email]", provider.prompts[0])

    def test_evidence_must_be_in_the_session_after_normalizing(self):
        result, provider = self.run_with([candidate(evidence="WE  park\ncharges after 3 retries"),
                                          candidate(title="Other", evidence="something never said")])
        self.assertEqual((result["proposed"], result["kept"], result["discarded"]), (2, 1, 1))
        self.assertEqual(result["discarded_reasons"], {"evidence_not_found": 1})
        self.assertEqual(provider.calls, 2)    # no "supported" call for the unverifiable one

    def test_provider_can_reject_as_unsupported(self):
        result, _ = self.run_with([candidate()], supported=False)
        self.assertEqual((result["kept"], result["discarded"]), (0, 1))
        self.assertEqual(result["discarded_reasons"], {"unsupported": 1})
        self.assertEqual(self.inbox(), [])

    def test_sensitive_title_or_body_is_discarded(self):
        result, _ = self.run_with([candidate(body="Use sk-test-00000000000000000000 for billing-api.")])
        self.assertEqual(result["discarded_reasons"], {"sensitive": 1})
        self.assertEqual(self.inbox(), [])

    def test_invalid_candidates_are_discarded(self):
        result, _ = self.run_with([candidate(type="runbook"), candidate(title=""),
                                   candidate(evidence="x" * 201)])
        self.assertEqual(result["discarded_reasons"], {"invalid": 3})

    def test_caps_candidates_per_session(self):
        cands = [candidate(title=f"Fact {i}", key=f"k{i}") for i in range(5)]
        result, _ = self.run_with(cands, max_per_session=2)
        self.assertEqual((result["proposed"], result["kept"]), (2, 2))

    def test_provider_failure_does_not_stop_the_run(self):
        def broken(prompt, schema):
            raise DS.DistillError("boom")
        result = DS.run(self.cfg(), day=DAY, provider=broken)
        self.assertEqual((result["sessions"], result["proposed"], result["errors"]), (1, 0, 1))

    def test_dry_run_writes_nothing_and_records_nothing(self):
        provider = FakeProvider([candidate()])
        before = self.telemetry()
        result = DS.run(self.cfg(), day=DAY, provider=provider, dry_run=True)
        self.assertEqual(result["kept"], 1)
        self.assertEqual(self.inbox(), [])
        self.assertEqual(self.telemetry(), before)

    def test_records_the_distill_telemetry_event(self):
        self.run_with([candidate()])
        lines = (self.vault / ".vault-memory" / "telemetry.jsonl").read_text(encoding="utf-8").splitlines()
        event = json.loads(lines[-1])
        self.assertEqual({k: event[k] for k in ("ev", "day", "sessions", "proposed", "kept", "updated",
                                                "replaced", "discarded", "confirmed")},
                         {"ev": "distill", "day": DAY, "sessions": 1, "proposed": 1, "kept": 1,
                          "updated": 0, "replaced": 0, "discarded": 0, "confirmed": 0})

    def test_disabled_does_nothing(self):
        cfg = self.cfg()
        cfg["distill"]["enabled"] = False
        provider = FakeProvider([candidate()])
        result = DS.run(cfg, day=DAY, provider=provider)
        self.assertEqual((provider.calls, result["sessions"]), (0, 0))

    def test_day_defaults_to_yesterday(self):
        result = DS.run(self.cfg(), provider=FakeProvider([]))
        self.assertEqual(result["day"], DAY)


class TestAutoApply(Pipeline):
    def existing(self, title, body, **kw):
        import note as N
        return N.write(self.cfg(), title=title, note_type="decision", folder="Memory/learned", body=body,
                       verified="today", **kw)

    def auto(self, cands):
        return DS.run(self.cfg(auto=True), day=DAY, provider=FakeProvider(cands))

    def read(self, rel):
        return (self.vault / rel).read_text(encoding="utf-8")

    def test_writes_through_note_write_with_source_and_expiry(self):
        result = self.auto([candidate()])
        self.assertEqual((result["kept"], result["updated"], result["replaced"]), (1, 0, 0))
        text = self.read("Memory/learned/Failed charges are parked after 3 retries.md")
        self.assertIn("type: decision", text)
        self.assertIn("source: distill 2026-11-09 aaaa1111", text)
        self.assertIn("expires: 2027-02-07", text)        # session day 2026-11-09 + 90 days
        self.assertIn("key: billing-retry-policy", text)
        self.assertNotIn("verified:", text)
        self.assertEqual(self.inbox(), [])

    def test_duplicate_content_updates_the_existing_note(self):
        self.existing("Failed charges parking rule", "Billing-api parks a charge after 3 failed retries.",
                      key="billing-retry-policy")
        result = self.auto([candidate(title="Failed charges parking rule", body="Parking happens only on weekdays.")])
        self.assertEqual((result["kept"], result["updated"], result["replaced"]), (0, 1, 0))
        text = self.read("Memory/learned/Failed charges parking rule.md")
        self.assertIn("Parking happens only on weekdays.", text)
        self.assertIn("Billing-api parks a charge after 3 failed retries.", text)

    def test_already_known_is_not_appended_twice(self):
        self.existing("Failed charges parking rule", "Billing-api parks a charge after 3 failed retries.")
        result = self.auto([candidate(title="Failed charges parking rule")])
        self.assertEqual((result["updated"], result["kept"], result["discarded_reasons"]), (0, 0, {"already_known": 1}))

    def test_same_key_with_other_content_replaces_the_current_note(self):
        self.existing("Charge retry limit", "Charges are retried forever until the customer pays.",
                      key="billing-retry-policy")
        result = self.auto([candidate()])
        self.assertEqual((result["kept"], result["updated"], result["replaced"]), (0, 0, 1))
        text = self.read("Memory/learned/Failed charges are parked after 3 retries.md")
        self.assertIn('replaces: "[[Charge retry limit]]"', text)

    def test_near_identical_content_with_another_title_updates(self):
        body = "Billing-api parks a charge after 3 failed retries and notifies the finance team by chat."
        self.existing("Charge parking after retries", body)
        result = self.auto([candidate(title="Charge parking after retries v", body=body, key="")])
        self.assertEqual((result["kept"], result["replaced"], result["discarded_reasons"]),
                         (0, 0, {"already_known": 1}))

    def test_dry_run_does_not_write_in_auto_mode(self):
        before = self.vault_tree()
        result = DS.run(self.cfg(auto=True), day=DAY, provider=FakeProvider([candidate()]), dry_run=True)
        self.assertEqual(result["kept"], 1)
        self.assertEqual(self.vault_tree(), before)


class TestRelate(Pipeline):
    """The model compares a new fact with the closest notes: a reworded duplicate is caught even
    when note.write's text similarity would let it through."""
    existing, read, auto = TestAutoApply.existing, TestAutoApply.read, TestAutoApply.auto

    def auto_rel(self, cands, relation):
        return DS.run(self.cfg(auto=True), day=DAY, provider=FakeProvider(cands, relation=relation))

    def test_same_fact_in_other_words_is_already_known(self):
        self.existing("Charges park after three failures", "After the third failed attempt the charge is parked.")
        before = self.vault_tree()
        result = self.auto_rel([candidate()], {"match": 1, "relation": "same"})
        self.assertEqual((result["kept"], result["discarded_reasons"]), (0, {"already_known": 1}))
        self.assertEqual(self.vault_tree(), before)

    def test_adds_to_a_learned_note_appends(self):
        self.existing("Charges park after three failures", "After the third failed attempt the charge is parked.")
        result = self.auto_rel([candidate(body="Parked charges are retried by hand on Mondays.")],
                               {"match": 1, "relation": "adds"})
        self.assertEqual(result["updated"], 1)
        self.assertIn("retried by hand on Mondays", self.read("Memory/learned/Charges park after three failures.md"))

    def test_contradicting_a_learned_note_replaces_it(self):
        self.existing("Charges park after three failures", "After the third failed attempt the charge is parked.")
        result = self.auto_rel([candidate(body="Charges are parked after 5 retries since 2026-11-09.")],
                               {"match": 1, "relation": "contradicts"})
        self.assertEqual(result["replaced"], 1)
        text = self.read("Memory/learned/Failed charges are parked after 3 retries.md")
        self.assertIn('replaces: "[[Charges park after three failures]]"', text)

    def test_a_note_the_person_wrote_is_never_edited_or_replaced(self):
        import note as N
        N.write(self.cfg(), title="Billing retry runbook", note_type="doc", folder="Docs",
                body="After the third failed attempt the charge is parked.", verified="today")
        human = self.read("Docs/Billing retry runbook.md")
        result = self.auto_rel([candidate(body="Charges are parked after 5 retries since 2026-11-09.")],
                               {"match": 1, "relation": "contradicts"})
        self.assertEqual(result["kept"], 1)
        self.assertEqual(self.read("Docs/Billing retry runbook.md"), human)
        text = self.read("Memory/learned/Failed charges are parked after 3 retries.md")
        self.assertIn("Contradicts [[Billing retry runbook]]", text)
        self.assertNotIn("replaces:", text)

    def test_unknown_answer_falls_back_to_plain_checks(self):
        result = self.auto_rel([candidate()], {"match": 9, "relation": "same"})
        self.assertEqual(result["kept"], 1)


class TestDays(Pipeline):
    def test_a_processed_day_is_not_learned_twice(self):
        first = DS.run(self.cfg(), day=DAY, provider=FakeProvider([candidate()]))
        provider = FakeProvider([candidate()])
        second = DS.run(self.cfg(), day=DAY, provider=provider)
        self.assertEqual((first["days"], second["days"], second["skipped"], provider.calls),
                         ([DAY], [], [DAY], 0))
        forced = DS.run(self.cfg(), day=DAY, provider=FakeProvider([candidate()]), force=True)
        self.assertEqual(forced["days"], [DAY])

    def test_first_run_only_learns_from_yesterday(self):
        self.assertEqual(DS.pending_days(self.cfg()), ["2026-11-09"])

    def test_missed_days_are_caught_up_without_repeating(self):
        import telemetry as TL
        TL.record(self.vault, {"ev": "distill", "day": "2026-11-06"}, self.cfg())
        self.assertEqual(DS.pending_days(self.cfg()), ["2026-11-07", "2026-11-08", "2026-11-09"])

    def test_catch_up_is_capped(self):
        import telemetry as TL
        TL.record(self.vault, {"ev": "distill", "day": "2026-10-01"}, self.cfg())
        days = DS.pending_days(self.cfg())
        self.assertEqual((len(days), days[0], days[-1]), (7, "2026-11-03", "2026-11-09"))

    def test_the_prompt_asks_for_absolute_dates(self):
        provider = FakeProvider([])
        DS.run(self.cfg(), day=DAY, provider=provider)
        self.assertIn("The session happened on 2026-11-09", provider.prompts[0])
        self.assertIn("absolute dates", provider.prompts[0])


class TestArchive(Env):
    def learned(self, name, fm):
        folder = self.vault / "Memory" / "learned"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text("---\ntype: doc\n" + fm + "\n---\n\n# x\n\nbody\n", encoding="utf-8")

    def test_expired_unread_learned_notes_move_to_the_archive(self):
        self.learned("Old.md", "source: distill 2026-08-01 aaaa1111\nexpires: 2026-11-01")
        self.learned("Fresh.md", "source: distill 2026-11-01 aaaa1111\nexpires: 2027-01-30")
        self.learned("Confirmed.md", "source: distill 2026-08-01 aaaa1111\nverified: 2026-09-01")
        self.learned("Mine.md", "source: meeting\nexpires: 2026-11-01")
        self.assertEqual(DS.archive_expired(self.cfg()), 1)
        self.assertTrue((self.vault / "_archive" / "Memory" / "learned" / "Old.md").exists())
        left = sorted(p.name for p in (self.vault / "Memory" / "learned").glob("*.md"))
        self.assertEqual(left, ["Confirmed.md", "Fresh.md", "Mine.md"])


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestProviders(Env):
    SCHEMA = {"type": "object", "properties": {"supported": {"type": "boolean"}}}

    def test_claude_found_outside_path(self):
        # launchd and cron run with a bare PATH: fall back to the usual install folders
        with tempfile.TemporaryDirectory() as home:
            exe = Path(home) / ".local" / "bin" / "claude"
            exe.parent.mkdir(parents=True)
            exe.write_text("")
            with mock.patch.object(DS.shutil, "which", return_value=None), \
                    mock.patch.dict("os.environ", {"HOME": home}):
                self.assertEqual(DS._claude("claude"), str(exe))
            with mock.patch.object(DS.shutil, "which", return_value=None), \
                    mock.patch.dict("os.environ", {"HOME": home + "/nobody"}):
                self.assertTrue(DS._claude("claude").endswith("claude"))

    def test_claude_cli_command_stdin_env_and_result(self):
        cfg = self.cfg(model="haiku", timeout=42)
        done = mock.Mock(returncode=0, stdout=json.dumps({"result": "", "structured_output": {"supported": True}}),
                         stderr="")
        with mock.patch.object(DS.subprocess, "run", return_value=done) as run, \
                mock.patch.object(DS.shutil, "which", return_value="/opt/bin/claude"):
            out = DS.get_provider(cfg)("the prompt", self.SCHEMA)
        self.assertEqual(out, {"supported": True})
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["/opt/bin/claude", "-p", "--model", "haiku", "--no-session-persistence", "--tools", "",
                                   "--strict-mcp-config", "--setting-sources", "", "--output-format", "json",
                                   "--json-schema", json.dumps(self.SCHEMA)])
        self.assertEqual(kwargs["input"], "the prompt")
        self.assertEqual(kwargs["cwd"], str(self.vault / ".vault-memory"))
        self.assertEqual(kwargs["env"]["VAULT_MEMORY_DISABLE"], "1")
        self.assertEqual(kwargs["timeout"], 42.0)

    def test_claude_cli_errors_become_distill_errors(self):
        provider = DS.get_provider(self.cfg())
        with mock.patch.object(DS.subprocess, "run", side_effect=FileNotFoundError):
            self.assertRaises(DS.DistillError, provider, "p", self.SCHEMA)
        with mock.patch.object(DS.subprocess, "run", side_effect=DS.subprocess.TimeoutExpired("claude", 1)):
            self.assertRaises(DS.DistillError, provider, "p", self.SCHEMA)
        with mock.patch.object(DS.subprocess, "run", return_value=mock.Mock(returncode=1, stdout="", stderr="x")):
            self.assertRaises(DS.DistillError, provider, "p", self.SCHEMA)
        with mock.patch.object(DS.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="{}", stderr="")):
            self.assertRaises(DS.DistillError, provider, "p", self.SCHEMA)

    def test_anthropic_api_request_and_result(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-test-0000000000000000"
        reply = {"content": [{"type": "tool_use", "name": "result", "input": {"supported": False}}]}
        with mock.patch.object(DS.urllib.request, "urlopen", return_value=FakeResponse(reply)) as urlopen:
            out = DS.get_provider(self.cfg(provider="anthropic-api"))("the prompt", self.SCHEMA)
        self.assertEqual(out, {"supported": False})
        request = urlopen.call_args[0][0]
        self.assertEqual(request.full_url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(request.get_header("X-api-key"), "sk-test-0000000000000000")
        body = json.loads(request.data)
        self.assertEqual(body["messages"][0]["content"], "the prompt")
        self.assertEqual(body["tools"][0]["input_schema"], self.SCHEMA)

    def test_anthropic_api_needs_a_key(self):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        provider = DS.get_provider(self.cfg(provider="anthropic-api"))
        self.assertRaises(DS.DistillError, provider, "p", self.SCHEMA)

    def test_unknown_provider_is_a_clear_error(self):
        with self.assertRaisesRegex(DS.DistillError, "unknown distill provider 'magic'"):
            DS.get_provider(self.cfg(provider="magic"))

    def test_run_builds_the_default_provider_only_when_there_are_sessions(self):
        with mock.patch.object(DS, "get_provider", side_effect=AssertionError("no sessions: no provider")):
            DS.run(self.cfg(), day=DAY)


class TestConfirmUsed(Env):
    def setUp(self):
        super().setUp()
        import note as N
        cfg = self.cfg()
        self.rel = N.write(cfg, title="Parked charges", note_type="doc", folder="Memory/learned", body="Body text.",
                           source="distill 2026-11-09 aaaa1111", expires="2027-02-07", key="parked")["p"]
        self.human = N.write(cfg, title="Human note", note_type="doc", folder="Memory/learned", body="Other body.",
                             source="a meeting", expires="2027-02-07")["p"]
        self.outside = N.write(cfg, title="Outside note", note_type="doc", folder="Memory",
                               body="Third body.", source="distill 2026-11-09 bbbb2222", expires="2027-02-07")["p"]

    def read_event(self, rel):
        import telemetry as TL
        TL.record(self.vault, {"ev": "read", "p": rel}, {})

    def text(self, rel):
        return (self.vault / rel).read_text(encoding="utf-8")

    def test_read_distilled_note_becomes_verified_and_loses_expires(self):
        before = self.text(self.rel)
        self.read_event(self.rel)
        self.assertEqual(DS.confirm_used(self.cfg()), 1)
        after = self.text(self.rel)
        self.assertIn("verified: 2026-11-10", after)
        self.assertNotIn("expires:", after)
        self.assertIn("source: distill 2026-11-09 aaaa1111", after)
        self.assertEqual(after.split("---", 2)[2], before.split("---", 2)[2])    # body untouched
        self.assertEqual(DS.confirm_used(self.cfg()), 0)                         # idempotent

    def test_ignores_unread_non_distill_and_outside_the_folder(self):
        self.read_event(self.human)
        self.read_event(self.outside)
        before = (self.text(self.rel), self.text(self.human), self.text(self.outside))
        self.assertEqual(DS.confirm_used(self.cfg()), 0)
        self.assertEqual((self.text(self.rel), self.text(self.human), self.text(self.outside)), before)

    def test_run_reports_confirmed(self):
        self.read_event(self.rel)
        result = DS.run(self.cfg(), day=DAY, provider=FakeProvider([]))
        self.assertEqual(result["confirmed"], 1)
        event = json.loads((self.vault / ".vault-memory" / "telemetry.jsonl").read_text().splitlines()[-1])
        self.assertEqual((event["ev"], event["confirmed"]), ("distill", 1))


if __name__ == "__main__":
    unittest.main()
