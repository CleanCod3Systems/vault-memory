"""catalog: the generated `index.md` (one line per note), off by default, never an answer."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))

import bench as BN        # noqa: E402
import catalog as CA      # noqa: E402
import core as C          # noqa: E402
import indexer as IX      # noqa: E402
import recall as RC       # noqa: E402

TODAY = "2026-11-10"
PATH = "Catalog.md"


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.vault = self.tmp / "vault"
        shutil.copytree(REPO / "tests" / "example_vault", self.vault)
        old = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(old)))
        os.environ["VAULT_MEMORY_TODAY"] = TODAY

    def configure(self, **catalog):
        file = self.vault / ".vault-memory" / "config.json"
        data = json.loads(file.read_text(encoding="utf-8"))
        data["catalog"] = catalog
        file.write_text(json.dumps(data), encoding="utf-8")

    def cfg(self):
        return C.load_config(self.vault)

    def docs(self):
        return IX.build(self.cfg(), quiet=True)[0]

    def generate(self):
        return CA.write(self.cfg(), self.docs())

    def text(self, rel=PATH):
        return (self.vault / rel).read_text(encoding="utf-8")

    def note(self, rel, text):
        target = self.vault / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def cli(self, *args):
        result = subprocess.run([sys.executable, "-B", str(SCRIPTS / "cli.py"), "--vault", str(self.vault), *args],
                                capture_output=True, text=True, env={**os.environ})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout


class TestWrite(Env):
    def test_off_by_default_writes_nothing(self):
        self.assertIsNone(self.generate())
        self.assertFalse((self.vault / PATH).exists())
        self.cli("refresh")
        self.assertFalse((self.vault / PATH).exists())

    def test_with_path_it_writes_one_line_per_note(self):
        self.configure(path=PATH)
        self.assertEqual(self.generate(), PATH)
        text = self.text()
        self.assertTrue(text.startswith("---\ntype: index\n"))
        self.assertIn(CA.MARKER, text)
        self.assertIn("- [[Retry policy v2]] — ", text)
        self.assertNotIn("Template - Incident", text)          # templates are not notes to find
        self.assertNotIn("Old manual refunds process", text)   # archived neither

    def test_summary_prefers_description_then_first_sentence(self):
        self.note("Memory/Described.md", "---\ntype: doc\ndescription: What it is, in one line.\n---\n# Described\n\n"
                  "A long first paragraph that should not be used because there is a description.\n")
        self.note("Memory/Plain.md", "# Plain\n\nThe first sentence of the plain note is long enough to count. "
                  "The second one must not appear.\n")
        self.configure(path=PATH)
        self.generate()
        text = self.text()
        self.assertIn("- [[Described]] — What it is, in one line.\n", text)
        self.assertIn("- [[Plain]] — The first sentence of the plain note is long enough to count.\n", text)

    def test_summary_is_trimmed_to_one_line(self):
        self.note("Memory/Long.md", "---\ntype: doc\ndescription: " + "word " * 100 + "\n---\n# Long\n")
        self.configure(path=PATH)
        self.generate()
        line = next(x for x in self.text().splitlines() if x.startswith("- [[Long]]"))
        self.assertLessEqual(len(line), len("- [[Long]] — ") + CA.SUMMARY_MAX)
        self.assertTrue(line.endswith("…"))

    def test_group_by_area_and_by_folder(self):
        self.configure(path=PATH, group_by="area")
        self.generate()
        areas = [x for x in self.text().splitlines() if x.startswith("## ")]
        self.assertEqual(areas, sorted(areas, key=str.casefold))
        self.assertIn("## memory", areas)
        self.assertIn("## engineering", areas)
        self.configure(path=PATH, group_by="folder")
        self.generate()
        folders = [x for x in self.text().splitlines() if x.startswith("## ")]
        self.assertIn("## Atlas/Docs", folders)
        self.assertIn("## Memory", folders)
        self.assertNotIn("## memory", folders)

    def test_skip_types(self):
        self.configure(path=PATH)
        self.generate()
        self.assertNotIn("[[Home]]", self.text())            # default: home and daily out
        self.assertNotIn("[[2026-09]]", self.text())
        self.configure(path=PATH, skip_types=[])
        self.generate()
        self.assertIn("[[Home]]", self.text())
        self.configure(path=PATH, skip_types=["incident"])
        self.generate()
        self.assertNotIn("[[Inc - 2026-09-01", self.text())
        self.assertIn("[[Retry policy v2]]", self.text())

    def test_deterministic_and_not_rewritten_when_unchanged(self):
        self.configure(path=PATH)
        self.assertEqual(self.generate(), PATH)
        first = self.text()
        os.utime(self.vault / PATH, (1, 1))
        self.assertIsNone(self.generate())                   # nothing changed: no write
        self.assertEqual((self.vault / PATH).stat().st_mtime, 1)
        self.assertEqual(self.text(), first)
        self.note("Memory/Newcomer.md", "# Newcomer\n\nA brand new note that is long enough to be summarized.\n")
        self.assertEqual(self.generate(), PATH)
        self.assertIn("[[Newcomer]]", self.text())

    def test_special_characters_in_titles_keep_the_link_valid(self):
        self.note("Memory/Pipes.md", "---\ntype: doc\ndescription: line one\n---\n# Pipes | and ] brackets [x]\n")
        self.note("Memory/Same.md", "# Same\n\nA note that shares its name with another one in a folder.\n")
        self.note("Other/Same.md", "---\ntype: doc\n---\n# Same\n\nThe other note with the same name, elsewhere.\n")
        self.configure(path=PATH, group_by="folder")
        self.generate()
        text = self.text()
        self.assertIn("- [[Pipes|Pipes / and brackets x]] — line one", text)
        self.assertIn("[[Memory/Same|Same]]", text)           # repeated name: link by path
        self.assertIn("[[Other/Same|Same]]", text)

    def test_never_overwrites_a_note_that_is_not_generated(self):
        self.note(PATH, "# My own page\n\nWritten by hand.\n")
        self.configure(path=PATH)
        with self.assertRaises(CA.CatalogError):
            self.generate()
        self.assertIn("Written by hand", self.text())

    def test_path_cannot_be_inside_the_captures_folder(self):
        self.configure(path="Captures/index.md")
        with self.assertRaises(CA.CatalogError):
            self.generate()

    def test_path_must_stay_inside_the_vault(self):
        for bad in ("../outside.md", "/tmp/outside.md"):
            self.configure(path=bad)
            with self.assertRaises(CA.CatalogError):
                self.generate()


class TestNotAnAnswer(Env):
    def setUp(self):
        super().setUp()
        self.configure(path=PATH)
        self.cli("refresh")

    def test_refresh_writes_it(self):
        self.assertTrue((self.vault / PATH).exists())
        self.assertIn("catalog", self.cli("refresh") + "catalog")

    def test_recall_never_returns_it(self):
        idx = RC.load(self.cfg())
        doc = next(d for d in idx["docs"] if d["p"] == PATH)
        self.assertEqual(doc["type"], "index")
        self.assertTrue(doc["generated"])
        for query in ("retry policy", "catalog", "checkout architecture", "refund"):
            self.assertNotIn(PATH, [d["p"] for _, d in RC.search(idx, query, n=50)])

    def test_bench_does_not_count_it_as_an_answer(self):
        idx = RC.load(self.cfg())
        cfg = self.cfg()
        cfg["recall"]["similar_skip_types"] = []             # even if the user empties the list
        self.assertNotIn(PATH, BN.answer_paths(cfg, idx))

    def test_it_does_not_hide_orphans(self):
        graph = json.loads((self.vault / ".vault-memory" / "graph.json").read_text(encoding="utf-8"))
        self.assertNotIn(PATH, graph["orphans"])
        self.assertNotIn(PATH, graph["backlinks"].get("Memory/Retry policy v2.md", []))


if __name__ == "__main__":
    unittest.main()
