"""Core behavior: what the rest of the system assumes.

Each test copies the example vault to a temp folder, so the repo stays clean.
Stdlib only: `python3 -m unittest discover tests`.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
EXAMPLE = REPO / "tests" / "example_vault"
sys.path.insert(0, str(SCRIPTS))

import core as C        # noqa: E402
import indexer as IX    # noqa: E402
import recall as RC     # noqa: E402
import schedule as SC   # noqa: E402

# Keys consumed by the hook and the skills: they can be added, never removed.
RECALL_JSON_KEYS = {"score", "p", "n", "t", "type", "area", "date", "status", "sev",
                    "system", "ids", "refs", "state", "passage"}


class WithVault(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        shutil.copytree(EXAMPLE, self.vault)
        self.cfg = C.load_config(self.vault)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def cli(self, *args):
        result = subprocess.run([sys.executable, "-B", str(SCRIPTS / "cli.py"), "--vault",
                                 str(self.vault), *args], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout


class TestIndex(WithVault):
    def test_scan(self):
        docs, _ = IX.scan(self.cfg)
        paths = {d["p"] for d in docs}
        self.assertIn("Atlas/Incidents/Inc - 2026-06-18 - Duplicate refund to customer.md", paths)
        self.assertFalse(any(".env" in p for p in paths), "a sensitive file is never indexed")

    def test_types_by_folder_and_template(self):
        docs = {d["n"]: d for d in IX.scan(self.cfg)[0]}
        self.assertEqual(docs["Runbook - Renew TLS certificate"]["type"], "runbook")
        self.assertTrue(docs["Template - Incident"]["template"])
        self.assertTrue(docs["Old manual refunds process"]["archived"])

    def test_ids_and_tickets(self):
        docs = {d["n"]: d for d in IX.scan(self.cfg)[0]}
        doc = docs["Inc - 2026-05-20 - Payments declined by antifraud"]
        self.assertIn("48213", doc["ids"])
        self.assertEqual(doc["refs"]["tickets"], ["ATL-140"])

    def test_wikilink_inside_code_does_not_count(self):
        note = self.vault / "Atlas" / "Docs" / "System map.md"
        note.write_text(note.read_text(encoding="utf-8") + "\nExample: `[[does-not-exist]]`\n", encoding="utf-8")
        docs, name_to_paths = IX.scan(self.cfg)
        link_graph = IX.graph(docs, name_to_paths, self.cfg)
        self.assertFalse([r for r in link_graph["broken"] if r["link"] == "does-not-exist"])


class TestRecall(WithVault):
    def test_json_contract(self):
        rows = json.loads(self.cli("recall", "duplicate refund", "--json", "--n", "3"))
        self.assertTrue(rows)
        for row in rows:
            self.assertTrue(RECALL_JSON_KEYS <= set(row), set(row) ^ RECALL_JSON_KEYS)
            self.assertNotIn("/Users/", row["p"], "p is relative to the vault")

    def test_exact_ticket(self):
        rows = json.loads(self.cli("recall", "ATL-160", "--json"))
        self.assertIn("Duplicate refund", rows[0]["p"])

    def test_stemmer_joins_inflections(self):
        docs, _ = IX.scan(self.cfg)
        results = RC.search({"docs": docs}, "checkout went down because the certificate expired", n=3)
        self.assertIn("expired certificate", results[0][1]["p"])

    def test_stable_json(self):
        first = self.cli("recall", "payments", "--json")
        second = self.cli("recall", "payments", "--json", "--no-auto")
        self.assertEqual(first, second)


class TestContract(WithVault):
    def test_fix_without_apply_does_not_touch_notes(self):
        note = self.vault / "Atlas" / "Docs" / "Team onboarding.md"
        note.write_text("# no frontmatter\n", encoding="utf-8")
        before = note.read_bytes()
        self.cli("fix")
        self.assertEqual(note.read_bytes(), before)

    def test_fix_apply_only_adds(self):
        note = self.vault / "Atlas" / "Docs" / "Team onboarding.md"
        note.write_text("---\nauthor: ana\n---\n# Onboarding\n", encoding="utf-8")
        self.cli("fix", "--apply")
        fm, _ = C.frontmatter(note.read_text(encoding="utf-8"))
        self.assertEqual(fm.get("author"), "ana")
        self.assertEqual(fm.get("type"), "doc")


class TestStemmer(unittest.TestCase):
    def test_spanish_families(self):
        for family in (("cayo", "caido", "caida", "cae"), ("devoluciones", "devolucion"),
                       ("pagos", "pago"), ("cancelar", "cancelacion", "cancelado")):
            self.assertEqual(len({C.stem_es(w) for w in family}), 1, family)

    def test_english_families(self):
        for family in (("payments", "payment"), ("deploy", "deployed", "deploying", "deployment"),
                       ("refund", "refunds", "refunded"), ("crash", "crashes", "crashed")):
            self.assertEqual(len({C.stem_en(w) for w in family}), 1, family)

    def test_untouched(self):
        for token in ("api-service", "48213", "atl-160"):
            self.assertEqual(C.stem_es(token), token)
            self.assertEqual(C.stem_en(token), token)

    def test_detect_language(self):
        self.assertEqual(C.detect_language(["the checkout went down and we fixed it with the team"]), "en")
        self.assertEqual(C.detect_language(["el checkout se cayó y lo arreglamos con el equipo"]), "es")


class TestSchedule(unittest.TestCase):
    def test_plist_calls_stable_entry_with_profile(self):
        xml = SC._plist(Path("/tmp/Vault X"), 7, 30, None)
        self.assertIn("<string>--vault</string>", xml)
        self.assertIn("<string>refresh</string>", xml)
        self.assertIn("<key>CLAUDE_CONFIG_DIR</key>", xml)
        self.assertNotIn("/plugins/cache/", xml, "never the versioned plugin folder")


class TestEval(unittest.TestCase):
    def run_eval(self, *extra):
        return subprocess.run([sys.executable, "-B", str(SCRIPTS / "cli.py"), "--vault", str(EXAMPLE),
                               "eval", "--tsv", str(REPO / "tests" / "eval.tsv"), *extra],
                              capture_output=True, text=True)

    def test_recall_gate(self):
        # The minimum goes up when a case is fixed; it never goes down without an explicit decision.
        result = self.run_eval("--minimum", "20")
        self.assertEqual(result.returncode, 0, result.stdout[-400:] + result.stderr)

    def test_hook_gate(self):
        # hit@3 on every case and not a single pointer on conversational prompts.
        result = self.run_eval("--hook", "--minimum", "20")
        self.assertEqual(result.returncode, 0, result.stdout[-600:] + result.stderr)


if __name__ == "__main__":
    unittest.main()
