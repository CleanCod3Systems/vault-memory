"""vault-memory note — write correct memory without relying on the model remembering the contract.

Before creating, it looks for duplicates (normalized similarity or the same current `key`)
and exits with code 3 and the list of candidates, unless --force or --replaces points to one
of them: so "save a fact" ends in updating or replacing the note that already exists, not in
a second version that recall returns mixed with the first. It validates type, area, dates and
required fields (never writes placeholders), writes atomically and, when updating, keeps a
backup and touches only the requested keys (the rest of the frontmatter stays as written).

The same function is used by the CLI and the MCP server: it returns data, it does not print.
"""
import datetime as dt
import os
import re
import tempfile
from collections import defaultdict

import contract as CT
import core as C
import freshness as FR
import indexer as IX
import recall as RC


class NoteError(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details or {}


FORBIDDEN = re.compile(r'[\\/:*?"<>|#^\[\]]')
DATE_FIELDS = ("verified", "expires", "date", "since")


def _date(value, field):
    if value in (None, ""):
        return value
    if value == "today":
        return C.today().isoformat()
    try:
        return dt.date.fromisoformat(str(value)).isoformat()
    except ValueError:
        raise NoteError(2, f"{field}: invalid date {value!r} (use YYYY-MM-DD or 'today')")


def _yaml_value(value):
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(str(x) for x in value) + "]"
    value = str(value)
    if value.startswith(("[", "{", "@", "!", "&", "*", "'")) or ": " in value or " #" in value:
        return '"' + value.replace('"', '\\"') + '"'
    return value


def _draft(title, note_type, body, system=""):
    """In-memory doc with the same shape as the indexed ones, to compare it."""
    term_freq = defaultdict(int)
    for term in C.tokens(title):
        term_freq[term] += 3
    for term in C.tokens(body):
        term_freq[term] += 1
    return {"p": "", "n": title, "t": title, "type": note_type, "system": system, "ids": [],
            "tf": dict(term_freq), "words": sum(1 for _ in C.TOK.finditer(C.norm(body))) or 1,
            "archived": False, "template": False}


def duplicates(cfg, idx, draft, key=None, family=None):
    threshold = float(cfg["freshness"].get("near_duplicate", 0.85))
    own = RC.similarity_score(idx, draft, draft, prefix=False)
    known_families = C.families(cfg)
    out = []
    for d in idx["docs"]:
        if d["archived"] or d.get("template") or (family and known_families.get(d["type"]) != family):
            continue
        if key and d.get("key") == key and FR.state(d) in FR.CURRENT:
            out.append({"p": d["p"], "n": d["n"], "score": 1.0, "reason": f"same key '{key}'"})
            continue
        if own > 0:
            score = RC.similarity_score(idx, draft, d, prefix=False) / own
            if score >= threshold:
                out.append({"p": d["p"], "n": d["n"], "score": round(score, 2), "reason": "near-identical content"})
    return sorted(out, key=lambda x: (-x["score"], x["p"]))[:5]


def _set_keys(head, keys):
    """Replace (or add) only those keys in the raw frontmatter, keeping the rest exactly as
    written: lists, comments, order."""
    lines = head.split("\n")
    for key, value in keys.items():
        new_line = f"{key}: {_yaml_value(value)}"
        index = next((j for j, line in enumerate(lines) if re.match(rf"^{re.escape(key)}:", line)), None)
        if index is None:
            lines.append(new_line)
            continue
        end = index + 1
        while end < len(lines) and (lines[end][:1] in (" ", "\t") or lines[end].strip().startswith("- ")):
            end += 1
        lines[index:end] = [new_line]
    return "\n".join(lines)


def _write_atomic(target, text):
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".vault-memory-", suffix=".md")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, target)


def _resolve_replaced(idx, name):
    name_to_paths = defaultdict(list)
    for d in idx["docs"]:
        name_to_paths[d["n"]].append(d["p"])
    target = name.strip().strip("[]")
    candidates = name_to_paths.get(target) or [d["p"] for d in idx["docs"]
                                               if d["p"] == target or d["p"] == target + ".md"]
    if not candidates:
        raise NoteError(2, f"replaces: note {name!r} does not exist")
    return candidates[0]


def _link_to(path):
    return f"[[{os.path.splitext(os.path.basename(path))[0]}]]"


def write(cfg, title=None, note_type=None, folder=None, body="", area=None, tags=None,
          source=None, verified=None, replaces=None, key=None, expires=None, fields=None,
          update=None, append=False, force=False, dry_run=False):
    fields = dict(fields or {})
    for name in list(fields):
        if name in DATE_FIELDS:
            fields[name] = _date(fields[name], name)
    verified, expires = _date(verified, "verified"), _date(expires, "expires")
    idx = RC.load(cfg)
    freshness_keys = {k: v for k, v in (("source", source), ("verified", verified), ("key", key),
                                        ("expires", expires)) if v}

    if update:
        return _update(cfg, idx, update, body, append, area, tags, replaces,
                       {**freshness_keys, **fields}, dry_run)

    if not (title and note_type and folder):
        raise NoteError(2, "creating needs --title, --type and --folder (or --update PATH)")
    known_families = C.families(cfg)
    if note_type not in known_families:
        raise NoteError(2, f"unknown type {note_type!r}. Valid: {', '.join(sorted(known_families))}")
    folder = folder.strip().strip("/")
    if folder.startswith(".") or ".." in folder.split("/") or os.path.isabs(folder):
        raise NoteError(2, f"invalid folder: {folder!r} (relative to the vault, no '..')")
    name = FORBIDDEN.sub("-", title).strip(" .-")
    rel = f"{folder}/{name}.md" if folder else f"{name}.md"
    if (cfg["_vault"] / rel).exists():
        raise NoteError(2, f"{rel} already exists: to change it use --update", {"p": rel})
    area = area or C.area_of(rel, cfg)
    valid_areas = set(cfg.get("areas", {}).values()) | {cfg.get("default_area", "general")}
    if cfg.get("areas") and area not in valid_areas:
        raise NoteError(2, f"area {area!r} is not in the config. Valid: {', '.join(sorted(valid_areas))}")

    replaced = _resolve_replaced(idx, replaces) if replaces else None
    if not force:
        found = duplicates(cfg, idx, _draft(title, note_type, body, fields.get("system", "")),
                           key=key, family=known_families.get(note_type))
        found = [x for x in found if x["p"] != replaced]
        if found:
            raise NoteError(3, "there are notes that already cover this: update one (--update) or "
                               "replace it (--replaces), or force with --force", {"duplicates": found})

    required = C.required_fields(note_type, cfg)
    fm = {"type": note_type, "area": area, "tags": tags or [note_type, area]}
    for field in required:
        if field in fm:
            continue
        if field in fields:
            fm[field] = fields.pop(field)
        elif field == "date":
            fm[field] = C.today().isoformat()
        elif field == "project":
            fm[field] = name
    missing = [k for k in required if k not in fm]
    if missing:
        raise NoteError(2, f"type {note_type!r} requires {', '.join(missing)}: pass them with --field k=v",
                        {"missing": missing})
    fm.update(freshness_keys)
    if replaced:
        fm["replaces"] = _link_to(replaced)
    fm.update(fields)
    text = "---\n" + "".join(f"{k}: {_yaml_value(v)}\n" for k, v in fm.items()) + "---\n"
    text += f"\n# {title}\n\n{body.strip()}\n" if body.strip() else f"\n# {title}\n"
    if dry_run:
        return {"p": rel, "mode": "create", "dry_run": True, "text": text}
    _write_atomic(cfg["_vault"] / rel, text)
    IX.build(cfg, quiet=True, incremental=True)
    return {"p": rel, "mode": "create", **({"replaces": replaced} if replaced else {})}


def _update(cfg, idx, rel, body, append, area, tags, replaces, keys, dry_run):
    rel = rel.strip().lstrip("/")
    path = cfg["_vault"] / rel
    if not path.exists():
        raise NoteError(2, f"{rel} does not exist")
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---") or text.find("\n---", 3) < 0:
        raise NoteError(2, f"{rel} has no frontmatter: normalize it with  vault-memory fix --apply")
    end = text.find("\n---", 3)
    head, rest = text[4:end], text[end + 4:]
    if area:
        keys["area"] = area
    if tags:
        keys["tags"] = tags
    if replaces:
        keys["replaces"] = _link_to(_resolve_replaced(idx, replaces))
    if body.strip():
        if append:
            rest = rest.rstrip() + f"\n\n## {C.today().isoformat()}\n\n{body.strip()}\n"
        else:
            heading = re.search(r"^#\s+.+$", rest, re.M)
            rest = f"\n{heading.group(0) if heading else '# ' + path.stem}\n\n{body.strip()}\n"
    if not keys and not body.strip():
        raise NoteError(2, "nothing to update: pass a body or some key (--verified today, --field k=v…)")
    new_text = "---\n" + _set_keys(head, keys) + "\n---" + rest
    if dry_run:
        return {"p": rel, "mode": "update", "dry_run": True, "text": new_text}
    target = CT.backup(cfg, rel)
    _write_atomic(path, new_text)
    IX.build(cfg, quiet=True, incremental=True)
    return {"p": rel, "mode": "update", "backup": str(target.relative_to(cfg["_vault"]))}
