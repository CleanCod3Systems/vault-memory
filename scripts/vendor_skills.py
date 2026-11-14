#!/usr/bin/env python3
"""Bring the Obsidian skills the plugin bundles up to date with their upstream repository.

Run before each release (`make vendor`). Every bundled skill is replaced by the upstream copy,
with the upstream license and an UPSTREAM file that records the commit it came from, so the
skills update whenever the plugin does.

    python3 scripts/vendor_skills.py               # clone REPO at --ref (default main)
    python3 scripts/vendor_skills.py --from DIR    # use a local checkout instead
"""
import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = "https://github.com/obsidianmd/obsidian-skills"
SOURCE = "plugins/obsidian/skills"
SKILLS = ("obsidian-markdown", "obsidian-bases")
ROOT = Path(__file__).resolve().parent.parent


def vendor(checkout, commit, dest=ROOT / "skills"):
    """Copy SKILLS from `checkout` into `dest`; returns the copied folders."""
    checkout, dest = Path(checkout), Path(dest)
    copied = []
    for name in SKILLS:
        source = checkout / SOURCE / name
        if not (source / "SKILL.md").is_file():
            raise SystemExit(f"{source}: no SKILL.md upstream")
        target = dest / name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target, ignore=shutil.ignore_patterns(".DS_Store"))
        shutil.copyfile(checkout / "LICENSE", target / "LICENSE")
        (target / "UPSTREAM").write_text(f"{REPO}/tree/{commit}/{SOURCE}/{name}\n", encoding="utf-8")
        copied.append(target)
    return copied


def _git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ref", default="main", help="upstream branch or tag (default main)")
    ap.add_argument("--from", dest="checkout", help="local checkout of the upstream repository")
    a = ap.parse_args(argv)
    if a.checkout:
        copied = vendor(a.checkout, _git("rev-parse", "HEAD", cwd=a.checkout))
    else:
        with tempfile.TemporaryDirectory() as tmp:
            _git("clone", "--quiet", "--depth", "1", "--branch", a.ref, REPO, tmp)
            copied = vendor(tmp, _git("rev-parse", "HEAD", cwd=tmp))
    for path in copied:
        print(f"{path.relative_to(ROOT)}  ←  {(path / 'UPSTREAM').read_text(encoding='utf-8').strip()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
