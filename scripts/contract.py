#!/usr/bin/env python3
"""Frontmatter normalization: the only code (with `note` and `migrate`) that writes INSIDE notes.

It only adds missing keys. It never overwrites a value, never touches the body, and always
leaves a copy in <state>/backups/<timestamp>/.
"""
import datetime as dt
import re
import shutil

import core as C
import indexer as IX


def plan(docs, cfg):
    """[(path, {key: value}), ...] — additions only."""
    actions = []
    for d in docs:
        if d["archived"] or d.get("template"):
            continue
        present, additions = set(d["fm"]), {}
        if "type" not in present:
            additions["type"] = d["type"]
        if "area" not in present:
            additions["area"] = d["area"]
        if "tags" not in present or not d["tags"]:
            additions["tags"] = sorted({d["type"], d["area"]})
        if "date" not in present and d["date"] and "date" in C.required_fields(d["type"], cfg):
            additions["date"] = d["date"]
        if "project" not in present and d["type"] == "project":
            additions["project"] = d["n"]          # the file name IS the project
        # a single ticket cited in the body of a record note IS its ticket
        if "ticket" not in present and d["family"] == "record" and len(d["refs"]["tickets"]) == 1:
            additions["ticket"] = d["refs"]["tickets"][0]
        for key in C.EMPTY_FIELDS.get(d["type"], []):
            if key not in present:
                additions[key] = ""
        if additions:
            actions.append((d["p"], additions))
    return actions


def _yaml(value):
    if isinstance(value, list):
        return "\n" + "\n".join(f"  - {x}" for x in value)
    if value == "":
        return ""
    return f" {value}" if re.fullmatch(r"[\w\-\./:]+", str(value)) else f' "{value}"'


def apply_additions(vault, rel, additions):
    path = vault / rel
    text = path.read_text(encoding="utf-8")
    block = "".join(f"{k}:{_yaml(v)}\n" for k, v in additions.items())
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end < 0:
            return False                       # unclosed frontmatter: leave it alone
        new_text = text[:end + 1] + block + text[end + 1:]
    else:
        new_text = "---\n" + block + "---\n\n" + text.lstrip("\n")
    path.write_text(new_text, encoding="utf-8")
    return True


def report(coverage, actions):
    print(f"contract · coverage {coverage['coverage']}% ({coverage['conforming']}/{coverage['notes']} notes)")
    for note_type, entry in coverage["by_type"].items():
        if entry["ok"] < entry["n"]:
            missing = ", ".join(f"{k}×{n}" for k, n in
                                sorted(entry["missing"].items(), key=lambda x: -x[1])[:5])
            print(f"  {note_type:<13} {entry['ok']:>3}/{entry['n']:<3}  missing: {missing}")
    if coverage["unknown_types"]:
        print("\n  type outside the standard vocabulary:")
        for note_type, examples in coverage["unknown_types"].items():
            print(f"    {note_type!r} — {len(examples)} note(s), e.g. {examples[0]}")
        print("    Add it to config.json → extra_types, or fix the notes.")
    if not actions:
        print("\nNothing to add.")
        return
    keys = {}
    for _, additions in actions:
        for key in additions:
            keys[key] = keys.get(key, 0) + 1
    print(f"\n{len(actions)} notes to normalize · additions: "
          + ", ".join(f"{k}×{n}" for k, n in sorted(keys.items(), key=lambda x: -x[1])))


def backup_dir(cfg):
    return cfg["_state"] / "backups" / dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def backup(cfg, rel, target=None):
    target = target or backup_dir(cfg)
    (target / rel).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(cfg["_vault"] / rel, target / rel)
    return target


def fix(cfg, apply=False):
    # Without --apply it is a real dry run: it writes neither the notes nor the state.
    docs, _graph, coverage = IX.build(cfg, quiet=True, write=False)
    actions = plan(docs, cfg)
    report(coverage, actions)
    if not actions:
        return
    if not apply:
        for path, additions in actions[:8]:
            print(f"  {path}\n      + " + ", ".join(f"{k}={v!r}" for k, v in additions.items()))
        if len(actions) > 8:
            print(f"  … and {len(actions) - 8} more")
        print("\nDry run. To apply:  vault-memory fix --apply")
        return
    target, changed = backup_dir(cfg), 0
    for path, additions in actions:
        backup(cfg, path, target)
        if apply_additions(cfg["_vault"], path, additions):
            changed += 1
    print(f"\n{changed} notes normalized · backup in {cfg['_state'].name}/backups/{target.name}/")
    _, _, after = IX.build(cfg, quiet=True)
    print(f"coverage now: {after['coverage']}%")


def retype(cfg, apply=False):
    """`type` that no longer matches where the note lives (moved to another folder)."""
    docs, _, _ = IX.build(cfg, quiet=True, write=False)
    drift = [d for d in docs if not d["archived"] and not d.get("template")
             and d["type"] != d["struct_type"]]
    if not drift:
        print("retype · no drift")
        return
    print(f"retype · {len(drift)} notes whose type differs from the structural one")
    target = backup_dir(cfg)
    for d in drift:
        print(f"  {d['type']:>12} → {d['struct_type']:<12} {d['p']}")
        if not apply:
            continue
        path = cfg["_vault"] / d["p"]
        text = path.read_text(encoding="utf-8")
        # Same rule as fix: a note is never rewritten without a copy first.
        backup(cfg, d["p"], target)
        text = re.sub(r"^(type|tipo_nota):.*$", f"type: {d['struct_type']}", text, count=1, flags=re.M)
        text = re.sub(rf"^(  - ){re.escape(d['type'])}$", rf"\g<1>{d['struct_type']}", text,
                      count=1, flags=re.M)
        path.write_text(text, encoding="utf-8")
    if apply:
        IX.build(cfg, quiet=True)
        print(f"  {len(drift)} fixed · backup in {cfg['_state'].name}/backups/{target.name}/")
    else:
        print("\nDry run. To apply:  vault-memory retype --apply")
