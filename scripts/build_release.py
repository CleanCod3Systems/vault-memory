#!/usr/bin/env python3
"""Build the hosted plugin catalog: <out>/vault-memory-<version>.zip and <out>/marketplace.json.

The catalog is what Claude Code downloads when a user adds the marketplace by URL
(`/plugin marketplace add https://<host>/marketplace.json`): the plugin entry is an `archive`
source with the zip's sha256, so a tampered or truncated download is refused. Vercel runs this
as its build step; nothing generated is committed.

Usage: python3 scripts/build_release.py --out public [--base-url https://host]
"""
import argparse
import hashlib
import json
import os
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INCLUDE = [".claude-plugin", ".codex-plugin", ".github/plugin", ".agents", "docs", "hooks",
           "scripts", "skills", "standard", "AGENTS.md", "CHANGELOG.md", "LICENSE", "README.md",
           "STANDARD.md", "codex-mcp.json", "gemini-extension.json"]
SKIP_PARTS = {"__pycache__", ".DS_Store"}


def files_to_pack():
    for entry in INCLUDE:
        path = ROOT / entry
        if path.is_file():
            yield path
        elif path.is_dir():
            for child in sorted(path.rglob("*")):
                if child.is_file() and not (set(child.parts) & SKIP_PARTS) and child.suffix != ".pyc":
                    yield child


def build_zip(target):
    # Fixed timestamps and order: the same tree always gives the same sha256.
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files_to_pack()):
            info = zipfile.ZipInfo(path.relative_to(ROOT).as_posix(), date_time=(2026, 1, 1, 0, 0, 0))
            info.external_attr = ((0o755 if os.access(path, os.X_OK) else 0o644) << 16)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
    return hashlib.sha256(target.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="public")
    parser.add_argument("--base-url", default=os.environ.get("VAULT_MEMORY_BASE_URL", ""),
                        help="public URL of the catalog (default: Vercel's production URL)")
    args = parser.parse_args()

    manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    marketplace = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    version = manifest["version"]
    base_url = args.base_url or ("https://" + os.environ["VERCEL_PROJECT_PRODUCTION_URL"]
                                 if os.environ.get("VERCEL_PROJECT_PRODUCTION_URL") else "")
    if not base_url:
        raise SystemExit("--base-url (or VERCEL_PROJECT_PRODUCTION_URL) is required")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    zip_name = f"vault-memory-{version}.zip"
    digest = build_zip(out / zip_name)
    entry = {**marketplace["plugins"][0], "version": version,
             "source": {"source": "archive", "url": f"{base_url.rstrip('/')}/{zip_name}", "sha256": digest}}
    catalog = {**marketplace, "plugins": [entry]}
    (out / "marketplace.json").write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n",
                                          encoding="utf-8")
    print(f"{out / zip_name}  sha256 {digest}")
    print(f"{out / 'marketplace.json'}")


if __name__ == "__main__":
    main()
