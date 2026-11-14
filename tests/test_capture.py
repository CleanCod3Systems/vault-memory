"""Captures: raw material kept as it arrived (redacted), cited by notes and never edited.
Temp copy of the example vault; the provider is the fake callable of the distill tests."""
import hashlib
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "tests"))
import capture as CP  # noqa: E402
import core as C  # noqa: E402
import distill as DS  # noqa: E402
import indexer as IX  # noqa: E402
import ingest as IG  # noqa: E402
import note as NT  # noqa: E402
import recall as RC  # noqa: E402
import review as RV  # noqa: E402
from test_distill import QUOTE, Env, FakeProvider, candidate  # noqa: E402

TOKEN = "ghp_" + "a1B2c3D4e5" * 4
THREAD = f"# Billing retries\n\nDecision: {QUOTE}.\nThe deploy key was {TOKEN}\n"
CAPTURE = "Captures/2026-11-10 billing-retries.md"


class CaptureEnv(Env):
    def capture(self, text=THREAD, **kwargs):
        return CP.write(self.cfg(), text, **kwargs)

    def cli(self, *args, stdin=""):
        env = {k: v for k, v in os.environ.items() if k != "VAULT_MEMORY_VAULT"}
        return subprocess.run([sys.executable, "-B", str(REPO / "scripts" / "cli.py"), "--vault", str(self.vault),
                               *args], input=stdin, capture_output=True, text=True, env=env)


class TestWrite(CaptureEnv):
    def test_writes_frontmatter_and_hash_and_redacts(self):
        result = self.capture(url="https://tracker.example/ABC-1")
        self.assertEqual((result["p"], result["created"]), (CAPTURE, True))
        text = CP._read(self.vault / CAPTURE)
        fm, body = CP.split(text)
        self.assertEqual((fm["type"], fm["title"], fm["captured"], fm["url"]),
                         ("capture", "Billing retries", "2026-11-10", "https://tracker.example/ABC-1"))
        self.assertNotIn(TOKEN, text)
        self.assertIn("[REDACTED:api-key]", body)
        self.assertEqual(fm["sha256"], hashlib.sha256(body.encode("utf-8")).hexdigest())
        self.assertEqual(body, DS.redact(THREAD))
        event = json.loads((self.vault / ".vault-memory" / "telemetry.jsonl").read_text().splitlines()[-1])
        self.assertEqual(event["ev"], "capture")
        self.assertNotIn("Billing", json.dumps(event))

    def test_same_content_twice_returns_the_same_path(self):
        first, second = self.capture(), self.capture()
        self.assertEqual(first["p"], second["p"])
        self.assertFalse(second["created"])
        self.assertEqual(len(list((self.vault / "Captures").glob("*.md"))), 1)

    def test_never_overwrites_other_content(self):
        self.capture()
        other = self.capture(THREAD + "\nOne more line.\n")
        self.assertEqual(other["p"], "Captures/2026-11-10 billing-retries-2.md")
        self.assertIn(QUOTE, CP._read(self.vault / CAPTURE))
        self.assertNotIn("One more line", CP._read(self.vault / CAPTURE))

    def test_title_and_date_flags(self):
        result = self.capture("plain first line\nsecond\n", title="Vendor call", date="2026-10-01")
        self.assertEqual(result["p"], "Captures/2026-10-01 vendor-call.md")
        self.assertEqual(self.capture("first line wins\nrest\n")["p"], "Captures/2026-11-10 first-line-wins.md")
        with self.assertRaises(CP.CaptureError):
            self.capture("   \n")
        with self.assertRaises(CP.CaptureError):
            self.capture(date="yesterday")

    def test_cli_prints_the_path_and_reads_stdin(self):
        out = self.cli("capture", "-", "--url", "https://tracker.example/ABC-1", stdin=THREAD)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), CAPTURE)
        again = self.cli("capture", "-", "--json", stdin=THREAD)
        self.assertEqual(json.loads(again.stdout)["p"], CAPTURE)
        self.assertFalse(json.loads(again.stdout)["created"])


class TestIngestCapture(CaptureEnv):
    def test_candidates_cite_the_capture(self):
        rel = self.capture(url="https://tracker.example/ABC-1", date="2026-11-08")["p"]
        session = CP.session(self.cfg(), rel)
        self.assertEqual((session["origin"], session["url"], session["day"]),
                         ("capture", "https://tracker.example/ABC-1", "2026-11-08"))
        provider = FakeProvider([candidate()])
        result = IG.run(self.cfg(), session, provider=provider)
        self.assertEqual(result["kept"], 1)
        self.assertNotIn(TOKEN, "".join(provider.prompts))
        [file] = sorted((self.vault / ".vault-memory" / "inbox").glob("*.md"))
        text = file.read_text(encoding="utf-8")
        self.assertIn('capture: "[[Captures/2026-11-08 billing-retries]]"', text)
        self.assertIn("url: https://tracker.example/ABC-1", text)

    def test_auto_note_cites_the_capture_and_the_link_is_not_broken(self):
        self.capture()
        cfg = self.cfg(auto=True)
        IG.run(cfg, CP.session(cfg, str(self.vault / CAPTURE)), provider=FakeProvider([candidate()]))
        text = (self.vault / "Memory" / "learned" / "Failed charges are parked after 3 retries.md").read_text(encoding="utf-8")
        self.assertIn('capture: "[[Captures/2026-11-10 billing-retries]]"', text)
        _, link_graph, _ = IX.build(self.cfg(), quiet=True, write=False)
        self.assertFalse([b for b in link_graph["broken"] if "billing-retries" in b["link"]])

    def test_only_paths_under_the_captures_folder(self):
        self.capture()
        for bad in ("Home.md", "../outside.md", "Captures/missing.md", str(self.tmp / "elsewhere.md")):
            with self.assertRaises(CP.CaptureError, msg=bad) as caught:
                CP.session(self.cfg(), bad)
            self.assertEqual(caught.exception.code, 2)
        out = self.cli("ingest", "--capture", "Home.md")
        self.assertEqual(out.returncode, 2)
        self.assertIn("not a capture", out.stderr)
        self.assertEqual(self.cli("ingest", "--capture", "Captures/missing.md").returncode, 2)


class TestNeverEdited(CaptureEnv):
    def setUp(self):
        super().setUp()
        self.capture()
        self.before = CP._read(self.vault / CAPTURE)

    def tearDown(self):
        self.assertEqual(CP._read(self.vault / CAPTURE), self.before)

    def test_note_update_and_replaces_are_refused(self):
        with self.assertRaises(NT.NoteError) as caught:
            NT.write(self.cfg(), update=CAPTURE, body="changed")
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("capture", caught.exception.message)
        with self.assertRaises(NT.NoteError):
            NT.write(self.cfg(), update=CAPTURE, verified="today")
        with self.assertRaises(NT.NoteError):
            NT.write(self.cfg(), title="New", note_type="doc", folder="Memory", body="x",
                     replaces=CAPTURE[:-3], force=True)
        with self.assertRaises(NT.NoteError):
            NT.write(self.cfg(), title="Inside", note_type="doc", folder="Captures", body="x", force=True)
        out = self.cli("note", "--update", CAPTURE, "--body", "changed")
        self.assertEqual(out.returncode, 2)
        self.assertIn("never edited", out.stderr)

    def test_review_and_flag_are_refused(self):
        with self.assertRaises(NT.NoteError):
            RV.record(self.cfg(), CAPTURE, "confirmed", "checked", confidence=9)
        with self.assertRaises(NT.NoteError):
            RV.flag(self.cfg(), CAPTURE, "it is wrong")
        self.assertFalse((self.vault / ".vault-memory" / "flags.jsonl").exists())
        self.assertEqual(self.cli("review", CAPTURE, "--verdict", "unsure", "--evidence", "x").returncode, 2)
        self.assertEqual(self.cli("review", CAPTURE, "--flag", "wrong").returncode, 2)

    def test_distill_upkeep_skips_captures(self):
        cfg = self.cfg(folder="Captures")
        path = self.vault / "Captures" / "2026-01-01 learned.md"
        path.write_text("---\ntype: doc\nsource: distill 2026-01-01 x\nexpires: 2026-01-02\n---\nbody\n",
                        encoding="utf-8")
        self.assertEqual(DS.archive_expired(cfg), 0)
        self.assertTrue(path.exists())

    def test_writer_backstop(self):
        C.load_config(self.vault)
        with self.assertRaises(NT.NoteError):
            NT._write_atomic(self.vault / CAPTURE, "overwritten")


class TestOutsideTheIndex(CaptureEnv):
    def test_absent_from_recall(self):
        self.capture("# Zanzibar quokka ledger\n\nThe zanzibar quokka ledger closes on Fridays.\n")
        idx = RC.load(self.cfg())
        self.assertFalse([d for d in idx["docs"] if d["p"].startswith("Captures/")])
        self.assertFalse(RC.search(idx, "zanzibar quokka ledger"))

    def test_nested_captures_folder(self):
        vault_cfg = self.vault / ".vault-memory" / "config.json"
        own = json.loads(vault_cfg.read_text(encoding="utf-8"))
        own["captures"] = {"folder": "Memory/raw"}
        vault_cfg.write_text(json.dumps(own), encoding="utf-8")
        result = self.capture("# Zanzibar quokka ledger\n\ntext\n")
        self.assertTrue(result["p"].startswith("Memory/raw/"))
        self.assertFalse([p for _, p in C.walk(self.cfg()) if p.startswith("Memory/raw/")])
        with self.assertRaises(NT.NoteError):
            NT.write(self.cfg(), update=result["p"], body="changed")


class TestDoctor(CaptureEnv):
    def test_reports_edited_captures_and_missing_ones(self):
        self.capture()
        other = self.capture("# Second\n\nsome text\n")["p"]
        with open(self.vault / other, "a", encoding="utf-8", newline="") as handle:
            handle.write("edited by hand\n")
        NT.write(self.cfg(), title="Cites a missing capture", note_type="doc", folder="Memory",
                 body="Something learned from a capture that is gone.", force=True,
                 fields={"capture": "[[Captures/2026-01-01 gone]]"})
        docs, _, _ = IX.build(self.cfg(), quiet=True, write=False)
        report = CP.report(self.cfg(), docs)
        self.assertEqual(report["n"], 2)
        self.assertEqual(report["edited"], [other])
        self.assertEqual(report["dangling"], [{"p": "Memory/Cites a missing capture.md",
                                               "capture": "Captures/2026-01-01 gone"}])
        out = self.cli("doctor", "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["captures"]["edited"], [other])
        text = self.cli("doctor").stdout
        self.assertIn("edited after capture", text)
        self.assertIn("missing capture", text)


if __name__ == "__main__":
    unittest.main()
