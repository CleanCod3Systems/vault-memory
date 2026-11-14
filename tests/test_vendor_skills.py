"""Bundled Obsidian skills: copied whole from upstream, with its license and the commit they came from."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import vendor_skills as V  # noqa: E402


class TestVendor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.up, self.dest = Path(self.tmp.name) / "up", Path(self.tmp.name) / "skills"
        (self.up / "LICENSE").parent.mkdir(parents=True)
        (self.up / "LICENSE").write_text("MIT License\n")
        for name in V.SKILLS:
            skill = self.up / V.SOURCE / name
            (skill / "references").mkdir(parents=True)
            (skill / "SKILL.md").write_text(f"---\nname: {name}\ndescription: x\n---\n# {name}\n")
            (skill / "references" / "more.md").write_text("more\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_copies_each_skill_with_license_and_origin(self):
        V.vendor(self.up, "abc123", self.dest)
        for name in V.SKILLS:
            target = self.dest / name
            self.assertEqual((target / "references" / "more.md").read_text(), "more\n")
            self.assertEqual((target / "LICENSE").read_text(), "MIT License\n")
            self.assertEqual((target / "UPSTREAM").read_text(),
                             f"{V.REPO}/tree/abc123/{V.SOURCE}/{name}\n")

    def test_a_file_dropped_upstream_is_dropped_here(self):
        stale = self.dest / V.SKILLS[0] / "old.md"
        stale.parent.mkdir(parents=True)
        stale.write_text("gone upstream\n")
        V.vendor(self.up, "abc123", self.dest)
        self.assertFalse(stale.exists())

    def test_refuses_an_upstream_without_the_skill(self):
        (self.up / V.SOURCE / V.SKILLS[1] / "SKILL.md").unlink()
        with self.assertRaises(SystemExit):
            V.vendor(self.up, "abc123", self.dest)


class TestBundled(unittest.TestCase):
    def test_every_bundled_skill_is_in_the_plugin_with_license_and_origin(self):
        for name in V.SKILLS:
            skill = ROOT / "skills" / name
            self.assertTrue((skill / "SKILL.md").is_file(), name)
            self.assertIn("MIT", (skill / "LICENSE").read_text(encoding="utf-8"), name)
            self.assertTrue((skill / "UPSTREAM").read_text(encoding="utf-8").startswith(V.REPO), name)


if __name__ == "__main__":
    unittest.main()
