"""doctor: long notes (body above doctor.long_words, without frontmatter or fenced code)."""
import json
import unittest

from test_freshness import Base

FENCE = "`" * 3


def words(n):
    return " ".join(f"palabra{i}" for i in range(n))


class TestLongNotes(Base):
    def put(self, rel, body, fm="type: note\n"):
        path = self.vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\n{fm}---\n# T\n{body}\n", encoding="utf-8")

    def report(self):
        return json.loads(self.cli("doctor", "--json"))["long_notes"]

    def config(self, **values):
        state = self.vault / ".vault-memory"
        state.mkdir(exist_ok=True)
        path = state / "config.json"
        cfg = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        cfg.setdefault("doctor", {}).update(values)
        path.write_text(json.dumps(cfg), encoding="utf-8")

    def test_long_listed_short_not(self):
        self.put("Memory/Big note.md", words(700))
        self.put("Memory/Small note.md", words(50))
        report = self.report()
        paths = {n["p"]: n["words"] for n in report["notes"]}
        self.assertIn("Memory/Big note.md", paths)
        self.assertGreater(paths["Memory/Big note.md"], 600)
        self.assertNotIn("Memory/Small note.md", paths)
        self.assertEqual(report["threshold"], 600)
        self.assertEqual(report["count"], len(report["notes"]))
        order = [(-n["words"], n["p"]) for n in report["notes"]]
        self.assertEqual(order, sorted(order))
        self.assertEqual(set(report), {"threshold", "count", "notes"})
        self.assertIn("notes longer than 600 words", self.cli("doctor"))

    def test_skip_type_and_fenced_code(self):
        self.put("Memory/Big daily.md", words(700), fm="type: daily\n")
        self.put("Memory/Code heavy.md", f"{FENCE}\n{words(700)}\n{FENCE}\nshort text")
        paths = {n["p"] for n in self.report()["notes"]}
        self.assertNotIn("Memory/Big daily.md", paths)
        self.assertNotIn("Memory/Code heavy.md", paths)

    def test_off(self):
        self.put("Memory/Big note.md", words(700))
        self.config(long_words=0)
        report = self.report()
        self.assertEqual((report["threshold"], report["count"], report["notes"]), (0, 0, []))
        self.assertNotIn("longer than", self.cli("doctor"))


if __name__ == "__main__":
    unittest.main()
