"""Builds a synthetic vault of N notes to measure performance (deterministic).
Usage: python3 tests/gen_vault.py TARGET N"""
import json
import random
import sys
from pathlib import Path

WORDS = ("payment refund order checkout stock inventory login sso search catalog "
         "deploy migration certificate alert queue retry customer invoice shipment "
         "provider price discount coupon card antifraud decline timeout database index "
         "server balancer cache token session permission role audit metric log").split()


def main(target, count):
    rnd = random.Random(42)
    root = Path(target)
    (root / ".vault-memory").mkdir(parents=True, exist_ok=True)
    (root / ".obsidian").mkdir(exist_ok=True)
    (root / ".vault-memory" / "config.json").write_text(json.dumps({
        "modules": {"panel": False, "bases": False, "templates": False},
        "types_by_folder": {"Incidents": "incident", "Docs": "doc"}}), encoding="utf-8")
    for i in range(count):
        folder = "Incidents" if i % 3 == 0 else "Docs"
        title = " ".join(rnd.sample(WORDS, 4))
        body = " ".join(rnd.choice(WORDS) for _ in range(rnd.randint(80, 600)))
        path = root / folder / f"{i:05d} - {title}.md"
        path.parent.mkdir(exist_ok=True)
        extra = "date: 2026-01-01\nstatus: open\nseverity: medium\nsystem: x\n" if folder == "Incidents" else ""
        path.write_text(f"---\ntype: {'incident' if folder == 'Incidents' else 'doc'}\narea: general\n"
                        f"tags: [x]\n{extra}---\n# {title}\n\n{body}\n", encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]))
