"""Skill frontmatter: what claude.ai and the Agent Skills spec reject."""
import re
import unittest
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[1] / "skills"


class TestSkills(unittest.TestCase):
    def test_frontmatter_is_uploadable(self):
        found = list(SKILLS.glob("*/SKILL.md"))
        self.assertTrue(found)
        for path in found:
            fm = path.read_text(encoding="utf-8").split("---")[1]
            description = re.search(r"^description:\s*(.+)$", fm, re.M).group(1)
            self.assertNotRegex(fm, r"<[^>\n]+>", f"{path.parent.name}: XML-like tags are rejected")
            self.assertLessEqual(len(description), 1024, path.parent.name)
            self.assertEqual(re.search(r"^name:\s*(\S+)", fm, re.M).group(1), path.parent.name)

    def test_skills_reply_in_the_users_language(self):
        for path in SKILLS.glob("*/SKILL.md"):
            self.assertIn("user's language", path.read_text(encoding="utf-8"), path.parent.name)


if __name__ == "__main__":
    unittest.main()
