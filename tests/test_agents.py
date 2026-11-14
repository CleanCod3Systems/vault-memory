"""Plugin agents (Claude Code): frontmatter, read-only, and the verify skill that uses them."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / "agents"
VERIFY = ROOT / "skills" / "verify" / "SKILL.md"


def split(path):
    _, fm, body = path.read_text(encoding="utf-8").split("---", 2)
    return fm, body


def field(fm, key):
    match = re.search(rf"^{key}:\s*(.+)$", fm, re.M)
    return match.group(1).strip() if match else None


def table(text):
    return [line for line in text.splitlines() if line.startswith("| ") and "Evidence" not in line]


class TestAgents(unittest.TestCase):
    def test_agents_are_the_two_reviewers(self):
        names = sorted(p.stem for p in AGENTS.glob("*.md"))
        self.assertEqual(names, ["memory-skeptic", "memory-verifier"])

    def test_frontmatter(self):
        for path in AGENTS.glob("*.md"):
            fm, body = split(path)
            self.assertEqual(field(fm, "name"), path.stem)
            self.assertEqual(field(fm, "model"), "inherit", path.stem)
            self.assertIsNone(field(fm, "tools"), f"{path.stem}: user sources are unknown, inherit tools")
            description = field(fm, "description")
            self.assertIn("<example>", description, path.stem)
            self.assertIn("</example>", description, path.stem)
            self.assertTrue(body.strip(), path.stem)

    def test_agents_never_write_and_answer_in_json(self):
        for path in AGENTS.glob("*.md"):
            _, body = split(path)
            self.assertRegex(body, r"(?i)\bread-only\b", path.stem)
            self.assertRegex(body, r"You never write: no `vault-memory review`", path.stem)
            self.assertIn("Only this JSON", body, path.stem)

    def test_skeptic_never_raises_confidence(self):
        _, body = split(AGENTS / "memory-skeptic.md")
        self.assertIn("never above the verifier's", body)
        self.assertIn("verdict_change", body)

    def test_verifier_scores_with_the_skill_table(self):
        _, body = split(AGENTS / "memory-verifier.md")
        rows = table(VERIFY.read_text(encoding="utf-8"))
        self.assertEqual(len(rows), 4)
        self.assertEqual(table(body), rows)

    def test_verify_skill_uses_the_agents_and_keeps_the_inline_path(self):
        text = VERIFY.read_text(encoding="utf-8")
        for name in ("memory-verifier", "memory-skeptic"):
            self.assertIn(f"`vault-memory:{name}`", text)
        self.assertIn("**Without them**, inline", text)
        self.assertIn("single writer", text)
        self.assertIn("vault-memory review \"PATH\" --verdict confirmed", text)

    def test_verify_skill_passes_the_configured_model(self):
        text = VERIFY.read_text(encoding="utf-8")
        self.assertIn("`models`", text)
        self.assertIn("`inherit`", text)


if __name__ == "__main__":
    unittest.main()
