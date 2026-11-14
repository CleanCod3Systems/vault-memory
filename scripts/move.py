"""vault-memory move — move and rename notes, folders and attachments by a plan, without
breaking what points at them.

A move plan is a TSV file, one line per file or folder: `old<TAB>new`, both relative to the
vault and both as they are BEFORE the move (blank lines and `#` comments are skipped). A folder
line moves everything under it; a line for something inside a moved folder wins over the folder
line. Two folders may go to the same destination (they merge) as long as no two files land on
the same path.

The whole plan is refused, touching nothing, when an origin does not exist (letter case
included), a destination already exists, two files would land on the same path (letter case
ignored, as Obsidian does), lines form a cycle (a→b, b→a), a folder would move into itself, a
note would lose its extension, or a line touches a hidden folder (.obsidian, the state folder).

Applying it, in this order:
1. backs up everything it touches in `<state>/backups/move-<timestamp>/`: each file at its old
   path, and the plan, config.json and the state files under `<state name>/`;
2. moves the files, through a staging folder so a rename that only changes letter case works on
   any disk;
3. rewrites, in every note, the links whose target moved or whose meaning changed because the
   note itself moved: `[[path/note]]`, `[[note]]` when the file name changes or stops being
   unique, `[[note#section]]`, `[[note|alias]]`, `![[…]]`, relative paths, markdown links
   `[x](path.md)`, and the plain names in `related:` and `replaces:`;
4. rewrites the config keys that hold vault paths and the state saved by note path.

Links resolve like Obsidian: by path when they have a `/`, otherwise by file name, ignoring
letter case. A name shared by several files is reported, never guessed, and left as it is.
Links inside code are examples and are left alone. A capture is never rewritten (its sha256 must
keep matching its body): when one links to something that moved, it is reported instead. Only
links change: every other byte of a note stays as it was.

Returns data and raises MoveError; it does not print. Derived files (index, graph, related
notes, patterns, panel) are rebuilt by the caller.
"""
import datetime as dt
import json
import os
import posixpath
import re
import shutil
import unicodedata
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote, unquote

import answer as AN
import core as C
import distill as DS
import hookfilter as HF
import indexer as IX
import legacy as LG
import note as N

RE_WIKI = re.compile(r"(!?)\[\[([^\[\]\n]+?)\]\]")
# destination: <with spaces>, or plain with balanced parentheses (Retry%20policy%20(old).md)
RE_MD = re.compile(r"(!?\[[^\]\n]*\]\()(<[^>\n]+>|(?:[^()\s]|\([^()\s]*\))+)((?:\s+\"[^\"\n]*\")?\))")
RE_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
RE_ENCODED = re.compile(r"%(?!20)[0-9A-Fa-f]{2}")
RE_ITEM = re.compile(r"[^,\[\]]+")
# Frontmatter keys whose plain values are note names (indexer `rel` and freshness `replaces`).
NAME_KEYS = ("related", "replaces", "reemplaza")
# Config keys that hold one vault path (a folder or a note); a missing key uses the module default.
CONFIG_PATHS = (("taxonomy",), ("captures", "folder"), ("distill", "folder"), ("distill", "archive"),
                ("answers", "folder"), ("hook", "memory_prefix"), ("catalog", "path"), ("log", "path"))
CONFIG_PREFIX_DICTS = ("areas", "types_by_folder")      # keys are folder prefixes
CONFIG_PREFIX_LISTS = (("recall", "skip_paths"),)       # items are path prefixes
STATE_JSONL = ("reviews.jsonl", "flags.jsonl")          # one event per line, `p` = note path
TELEMETRY = "telemetr*.jsonl*"                          # telemetry.jsonl, its rotation, the legacy name
TELEMETRY_FIELDS = ("p", "emitted", "ps")
VIEWS = (".base", ".canvas")                            # Obsidian files that may name folders


class MoveError(Exception):
    def __init__(self, code, message, errors=None):
        super().__init__(message)
        self.code, self.message, self.errors = code, message, list(errors or [])


def nfc(text):
    return unicodedata.normalize("NFC", text)


def key(text):
    """How link targets and destinations compare: Unicode-normalized, letter case ignored."""
    return nfc(text).casefold()


def _under(path, folder):
    return bool(folder) and (path == folder or path.startswith(folder + "/"))


# ───────────────────────────────────────────────────────────────────── plan
def _clean(path):
    return nfc(path.strip().replace("\\", "/")).strip("/")


def _unsafe(path):
    if not path:
        return "empty path"
    if re.match(r"^[A-Za-z]:", path):
        return f"{path}: use a path relative to the vault"
    parts = path.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return f"{path}: '..', '.' or an empty folder name"
    if any(p.startswith(".") for p in parts):
        return f"{path}: hidden files and folders (.obsidian, the state folder) are never moved"
    return ""


def read_plan(text):
    """([(line, old, new)], errors) from the TSV text."""
    rows, errors = [], []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 2:
            errors.append(f"line {n}: expected old<TAB>new")
            continue
        old, new = _clean(parts[0]), _clean(parts[1])
        problem = _unsafe(old) or _unsafe(new)
        if problem:
            errors.append(f"line {n}: {problem}")
        elif old == new:
            errors.append(f"line {n}: {old}: old and new are the same")
        else:
            rows.append((n, old, new))
    return rows, errors


def _exact(vault, rel):
    """The real relative path when `rel` exists with exactly these names (letter case
    included, whatever the disk), else None."""
    here, real = Path(vault), []
    for part in rel.split("/"):
        try:
            names = {nfc(x): x for x in os.listdir(here)}
        except OSError:
            return None
        if part not in names:
            return None
        real.append(names[part])
        here = here / names[part]
    return "/".join(real)


def _same_file(a, b):
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _cycles(entries):
    """Lines whose destination is the origin of another line, closing a loop."""
    by_old = {key(old): (n, old, new) for n, old, new, _ in entries}
    found, seen = [], set()
    for n, old, new, _ in entries:
        chain, here = [old], new
        while key(here) in by_old and key(here) not in {key(x) for x in chain}:
            chain.append(by_old[key(here)][1])
            here = by_old[key(here)][2]
        if len(chain) > 1 and key(here) == key(old) and key(old) not in seen:
            seen.update(key(x) for x in chain)
            found.append((n, " → ".join(chain + [old])))
    return found


def plan(cfg, text):
    """Validate the plan against the vault. {"entries": [(line, old, new, is_folder)],
    "moves": {old file: new file}, "empty": {old empty folder: new}}; MoveError(2) with every
    problem found otherwise."""
    vault = Path(cfg["_vault"])
    rows, errors = read_plan(text)
    raw = _raw_config(cfg)
    if LG.is_legacy_config(raw):
        errors.append("config.json uses legacy (Spanish) keys: run  vault-memory migrate --apply  first")
    entries, origins = [], {}
    for n, old, new in rows:
        real = _exact(vault, old)
        if real is None:
            errors.append(f"line {n}: {old} does not exist (letter case counts)")
            continue
        if real in origins:
            errors.append(f"line {n}: {old} is already moved by line {origins[real]}")
            continue
        origins[real] = n
        is_folder = (vault / real).is_dir()
        if is_folder and key(new).startswith(key(real) + "/"):
            errors.append(f"line {n}: {old} cannot move into itself")
            continue
        if not is_folder and Path(real).suffix.lower() != Path(new).suffix.lower():
            errors.append(f"line {n}: {old} → {new} changes the extension")
            continue
        entries.append((n, real, new, is_folder))
    if not rows and not errors:
        errors.append("the plan has no lines: one per file or folder, old<TAB>new")
    cycles = _cycles(entries)
    errors += [f"line {n}: cycle {chain}" for n, chain in cycles]
    in_cycle = {key(x) for _, chain in cycles for x in chain.split(" → ")}

    moves, empty, line_of = {}, {}, {}
    for n, old, new, is_folder in sorted(entries, key=lambda e: (-e[1].count("/"), e[0])):
        if not is_folder:
            moves.setdefault(old, new)
            line_of.setdefault(old, n)
            continue
        for root, dirs, files in os.walk(vault / old):
            dirs.sort()
            here = Path(root).relative_to(vault).as_posix()
            for name in sorted(files):
                rel = f"{here}/{name}"
                if rel not in moves:
                    moves[rel], line_of[rel] = new + rel[len(old):], n
            if not dirs and not files:
                empty.setdefault(here, new + here[len(old):])

    landing = defaultdict(list)
    for old, new in moves.items():
        landing[key(new)].append(old)
    for olds in landing.values():
        if len(olds) > 1:
            olds.sort()
            errors.append(f"line {line_of[olds[0]]}: {', '.join(olds)} would land on the same path "
                          f"{moves[olds[0]]}")
    new_files = {key(x) for x in moves.values()}
    # letter case ignored, so the answer is the same on every disk (Obsidian ignores it too)
    existing = {}
    for _, rel in C.walk(cfg, md_only=False, prune_excluded=False):
        existing.setdefault(key(rel), rel)
    for old, new in sorted(moves.items()):
        if key(old) in in_cycle or any(_under(key(old), x) for x in in_cycle):
            continue
        target = vault / new
        taken = existing.get(key(new), old) != old
        if taken or ((target.exists() or target.is_symlink()) and not _same_file(target, vault / old)):
            errors.append(f"line {line_of[old]}: {new} already exists")
            continue
        parts = new.split("/")
        for i in range(1, len(parts)):
            parent = "/".join(parts[:i])
            if (vault / parent).is_file() or key(parent) in new_files:
                errors.append(f"line {line_of[old]}: {new}: {parent} is a file, not a folder")
                break
    if errors:
        raise MoveError(2, "the move plan is not valid: nothing was moved", sorted(set(errors), key=_line_order))
    return {"entries": sorted(entries), "moves": moves, "empty": empty}


def _line_order(error):
    match = re.match(r"line (\d+)", error)
    return (int(match.group(1)) if match else 0, error)


# ───────────────────────────────────────────────────────────────── links
class Files:
    """The vault's files as Obsidian resolves links to them: by path, or by name."""

    def __init__(self, files):
        self.files = sorted(files)
        self.paths, self.notes, self.names = {}, defaultdict(list), defaultdict(list)
        for rel in self.files:
            self.paths.setdefault(key(rel), rel)
            name = rel.rsplit("/", 1)[-1]
            self.names[key(name)].append(rel)
            if name.lower().endswith(".md"):
                self.notes[key(name[:-3])].append(rel)

    def by_path(self, path):
        """A link without an extension is a note: `path.md` first, then `path` as written."""
        return self.paths.get(key(path + ".md")) or self.paths.get(key(path))

    def by_name(self, name):
        k = key(name)
        return (self.notes.get(k) or (self.notes.get(k[:-3]) if k.endswith(".md") else None)
                or self.names.get(k) or [])

    def by_suffix(self, path):
        tail = "/" + key(path)
        return [x for x in self.files if key(x).endswith(tail + ".md") or key(x).endswith(tail)]

    def resolve(self, target, source):
        """(style, file or None, ambiguous files) of a wikilink target written in `source`."""
        t = target.strip()
        if t.startswith(("./", "../")):
            p = posixpath.normpath(posixpath.join(posixpath.dirname(source or ""), t))
            return "relative", (None if p.startswith("..") else self.by_path(p)), []
        if "/" in t:
            p = t.lstrip("/")
            hit = self.by_path(p)
            if hit:
                return "path", hit, []
            found = self.by_suffix(p)
            # like the indexer: a folder that no longer exists still resolves by the note's name
            found = found or self.by_name(p.rsplit("/", 1)[-1])
        else:
            found = self.by_name(t)
        if len(found) > 1:
            found = closest(found, source)
        style = "suffix" if "/" in t else "name"
        return style, (found[0] if len(found) == 1 else None), (found if len(found) > 1 else [])

    def by_file(self, path):
        """A markdown link names the file, `.md` included; tolerate it missing."""
        return self.paths.get(key(path)) or self.paths.get(key(path + ".md"))

    def resolve_md(self, path, source):
        """(style, file or None) of a markdown link destination (already decoded)."""
        if path.startswith("/"):
            return "root", self.by_file(path.lstrip("/"))
        p = posixpath.normpath(posixpath.join(posixpath.dirname(source or ""), path))
        hit = None if p.startswith("..") else self.by_file(p)
        if hit:
            return "relative", hit
        return "vault", self.by_file(path)


def closest(candidates, source):
    """The candidate sharing the longest folder prefix with `source` (how the indexer and
    Obsidian break a tie), or all of them when that does not single one out."""
    folder = posixpath.dirname(source or "").split("/") if source else []

    def shared(path):
        n = 0
        for a, b in zip(folder, path.split("/")[:-1]):
            if key(a) != key(b):
                break
            n += 1
        return n
    best = max(shared(x) for x in candidates)
    top = [x for x in candidates if shared(x) == best]
    return top if len(top) == 1 else candidates


class Rewriter:
    def __init__(self, before, moves):
        self.moves = moves
        self.pre = Files(before)
        self.post = Files(moves.get(x, x) for x in before)
        self.warnings = []

    def new(self, rel):
        return self.moves.get(rel, rel)

    def _bare(self, path, explicit_md):
        return path[:-3] if path.lower().endswith(".md") and not explicit_md else path

    def wiki_target(self, target, source, where):
        """New text for a wikilink target written in `source`, or None to leave it."""
        style, hit, ambiguous = self.pre.resolve(target, source)
        if ambiguous:
            if any(x in self.moves for x in ambiguous) or source in self.moves:
                self.warnings.append(f"{where}: [[{target.strip()}]] matches {len(ambiguous)} files "
                                     f"({', '.join(ambiguous)}): left as it is, link it by path")
            return None
        if not hit:
            return None
        t, s = target.strip(), self.new(source) if source else source
        new_t, explicit_md = self.new(hit), t.lower().endswith(".md")
        if style == "name":
            old_name = self._bare(hit.rsplit("/", 1)[-1], explicit_md)
            new_name = self._bare(new_t.rsplit("/", 1)[-1], explicit_md)
            # a change of letter case is a rename too: the index resolves names case-sensitively
            candidate = t if nfc(old_name) == nfc(new_name) else new_name
        elif style == "relative":
            candidate = self._bare(posixpath.relpath(new_t, posixpath.dirname(s or "") or "."), explicit_md)
            if t.startswith("./") and not candidate.startswith("."):
                candidate = "./" + candidate
            if (new_t == hit and s == source) or nfc(posixpath.normpath(candidate)) == nfc(posixpath.normpath(t)):
                candidate = t                   # the same path: leave it as it was written
        else:
            lead = "/" if t.startswith("/") else ""
            candidate = t if new_t == hit else lead + self._bare(new_t, explicit_md)
        # Whatever the style, the new text must still point at the same file after the move.
        if self.post.resolve(candidate, s)[1] != new_t:
            candidate = self._bare(new_t, explicit_md)
            if self.post.resolve(candidate, s)[1] != new_t:
                self.warnings.append(f"{where}: [[{t}]] cannot be rewritten to reach {new_t}")
                return None
        return None if candidate == t else candidate

    def md_target(self, dest, source):
        """New destination of a markdown link written in `source`, or None to leave it."""
        angle = dest.startswith("<") and dest.endswith(">")
        raw = dest[1:-1] if angle else dest
        if not raw or raw.startswith("#") or RE_SCHEME.match(raw):
            return None
        path, sep, fragment = raw.partition("#")
        decoded = unquote(path)
        style, hit = self.pre.resolve_md(decoded, source)
        if not hit:
            return None
        new_t, s = self.new(hit), self.new(source) if source else source
        if new_t == hit and s == source:
            return None
        here = posixpath.dirname(s or "") or "."
        if style == "relative":
            new = posixpath.relpath(new_t, here)
        else:
            new = ("/" if style == "root" else "") + new_t
        if not decoded.lower().endswith(".md") and hit.lower().endswith(".md"):
            new = new[:-3]
        if self.post.resolve_md(new, s)[1] != new_t:
            new = posixpath.relpath(new_t, here)
        elif nfc(posixpath.normpath(new)) == nfc(posixpath.normpath(decoded)):
            return None                         # the same path: leave it as it was written
        if angle:
            encoded = f"<{new}{sep}{fragment}>"
        else:
            # keep the original's encoding: full percent-encoding, or only spaces as %20
            encoded = (quote(new, safe="/") if RE_ENCODED.search(path) else new.replace(" ", "%20")) + sep + fragment
        return None if encoded == dest else encoded

    def _names(self, value, source, where, changes):
        def swap(match):
            token = match.group(0)
            core = token.strip().strip('"').strip("'").strip()
            new = self.wiki_target(core, source, where) if core else None
            if new is None:
                return token
            changes.append((core, new))
            return token.replace(core, new, 1)
        return RE_ITEM.sub(swap, value)

    def _head(self, head, source, where, changes):
        """Plain note names in `related:` and `replaces:` (wikilinks are rewritten everywhere)."""
        lines, current = head.split("\n"), None
        for i, line in enumerate(lines):
            match = C.RE_FM_KEY.match(line)
            if match:
                current = match.group(1)
                value = match.group(2)
                if current in NAME_KEYS and value.strip() and "[[" not in value:
                    lines[i] = line[:match.start(2)] + self._names(value, source, where, changes)
                continue
            stripped = line.lstrip()
            if current in NAME_KEYS and stripped.startswith("- ") and "[[" not in stripped:
                lines[i] = line[:len(line) - len(stripped)] + "- " + self._names(stripped[2:], source, where, changes)
        return "\n".join(lines)

    def _links(self, text, source, where, changes):
        def wiki(match):
            inner = match.group(2)
            cut = min([i for i in (inner.find("#"), inner.find("|")) if i >= 0] or [len(inner)])
            if inner[:cut].endswith("\\") and inner[cut:cut + 1] == "|":
                cut -= 1
            new = self.wiki_target(inner[:cut], source, where) if inner[:cut].strip() else None
            if new is None:
                return match.group(0)
            out = f"{match.group(1)}[[{new}{inner[cut:]}]]"
            changes.append((match.group(0), out))
            return out

        def markdown(match):
            new = self.md_target(match.group(2), source)
            if new is None:
                return match.group(0)
            out = match.group(1) + new + match.group(3)
            changes.append((match.group(0), out))
            return out

        out, last = [], 0
        for code in IX.RE_CODE.finditer(text):
            out.append(RE_MD.sub(markdown, RE_WIKI.sub(wiki, text[last:code.start()])))
            out.append(code.group(0))
            last = code.end()
        out.append(RE_MD.sub(markdown, RE_WIKI.sub(wiki, text[last:])))
        return "".join(out)

    def text(self, text, source, where=None):
        """(new text, [(old, new)]) of one note written at `source` (its path before the move)."""
        where, changes = where or self.new(source or ""), []
        end = text.find("\n---", 3) if text.startswith("---") else -1
        if end >= 0:
            text = self._head(text[:end], source, where, changes) + text[end:]
        return self._links(text, source, where, changes), changes


# ──────────────────────────────────────────────────────────────── config
def _raw_config(cfg):
    path = Path(cfg["_state"]) / "config.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _folder_mapper(entries, moves):
    folders = sorted(((nfc(old), new) for _, old, new, is_folder in entries if is_folder),
                     key=lambda x: -len(x[0]))
    files = {nfc(old): new for old, new in moves.items()}

    def mapped(value):
        """New text of a config path (folder or note, trailing slash kept), or None."""
        core = nfc(value.strip()).strip("/")
        if not core:
            return None
        new = files.get(core)
        if new is None:
            new = next((n + core[len(o):] for o, n in folders if _under(core, o)), None)
        if new is None or new == core:
            return None
        return ("/" if value.strip().startswith("/") else "") + new + ("/" if value.strip().endswith("/") else "")
    return mapped


def _config(cfg, raw, entries, moves, post_files):
    """(new raw config, [changes], [warnings])."""
    raw = json.loads(json.dumps(raw))
    mapped, changes, warnings = _folder_mapper(entries, moves), [], []

    def covers_less(name, value):
        core = nfc(value).strip("/")
        lost = sorted(new for old, new in moves.items() if _under(nfc(old), core) and not _under(nfc(new), core))
        if core and lost:
            warnings.append(f"config {name}: {value!r} no longer covers {len(lost)} moved files "
                            f"(e.g. {lost[0]}): add the new folder if it should")

    for name in CONFIG_PREFIX_DICTS:
        section = raw.get(name)
        if not isinstance(section, dict):
            continue
        out = {}
        for prefix, value in section.items():
            new = mapped(prefix)
            if new and (new in out or new in section):
                warnings.append(f"config {name}: {prefix!r} would become {new!r}, which is already a key: "
                                f"left as it is, merge them by hand")
                out[prefix] = value
            elif new:
                changes.append({"key": name, "from": prefix, "to": new})
                out[new] = value
            else:
                covers_less(name, prefix)
                out[prefix] = value
        raw[name] = out
    for path in CONFIG_PREFIX_LISTS:
        items = (raw.get(path[0]) or {}).get(path[1])
        if not isinstance(items, list):
            continue
        for i, item in enumerate(items):
            new = mapped(item) if isinstance(item, str) else None
            if new:
                changes.append({"key": ".".join(path), "from": item, "to": new})
                items[i] = new
            elif isinstance(item, str):
                covers_less(".".join(path), item)
    defaults = {("captures", "folder"): C.captures_folder(cfg), ("distill", "folder"): DS.options(cfg)["folder"],
                ("distill", "archive"): DS.options(cfg)["archive"], ("answers", "folder"): AN.options(cfg)["folder"],
                ("hook", "memory_prefix"): HF.options(cfg)["memory_prefix"]}
    for path in CONFIG_PATHS:
        section = raw if len(path) == 1 else raw.get(path[0])
        value = section.get(path[-1]) if isinstance(section, dict) else None
        if not isinstance(value, str):
            value = defaults.get(path, "")
        new = mapped(value) if value else None
        if not new:
            continue
        if len(path) == 1:
            raw[path[0]] = new
        else:
            if not isinstance(raw.get(path[0]), dict):
                raw[path[0]] = {}
            raw[path[0]][path[1]] = new
        changes.append({"key": ".".join(path), "from": value, "to": new})
    # `exclude` holds folder NAMES (matched at any depth): follow a renamed folder only when no
    # other folder keeps the old name.
    names = {p for f in post_files for p in f.split("/")[:-1]}
    renamed = {old.rsplit("/", 1)[-1]: new.rsplit("/", 1)[-1] for _, old, new, is_folder in entries
               if is_folder and old.rsplit("/", 1)[-1] != new.rsplit("/", 1)[-1]}
    excluded = raw.get("exclude")
    if isinstance(excluded, list):
        for i, item in enumerate(excluded):
            if item in renamed and item not in names:
                changes.append({"key": "exclude", "from": item, "to": renamed[item]})
                excluded[i] = renamed[item]
            elif item in renamed:
                warnings.append(f"config exclude: {item!r} still names other folders; "
                                f"add {renamed[item]!r} if the moved one should stay excluded")
    return raw, changes, warnings


# ───────────────────────────────────────────────────────────────── state
def _jsonl(text, rewrite):
    """(new text, changed lines): only the lines `rewrite` changes are written again."""
    out, changed = [], 0
    for line in text.splitlines(keepends=True):
        try:
            event = json.loads(line)
        except ValueError:
            out.append(line)
            continue
        if isinstance(event, dict) and rewrite(event):
            changed += 1
            out.append(json.dumps(event, ensure_ascii=False) + ("\n" if line.endswith("\n") else ""))
        else:
            out.append(line)
    return "".join(out), changed


def _state(cfg, moves, rewriter):
    """{state file relative to the state folder: (old text, new text, changed lines)} plus
    warnings for what cannot be rewritten safely."""
    state = Path(cfg["_state"])
    paths = {nfc(old): new for old, new in moves.items()}
    out, warnings = {}, []

    def one(value):
        return paths.get(nfc(value)) if isinstance(value, str) else None

    def event(e):
        hit = False
        for field in TELEMETRY_FIELDS:
            value = e.get(field)
            if isinstance(value, str) and one(value):
                e[field], hit = one(value), True
            elif isinstance(value, list):
                new = [one(x) or x for x in value]
                if new != value:
                    e[field], hit = new, True
        return hit

    def read(path):
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    files = [state / name for name in STATE_JSONL] + sorted(state.glob(TELEMETRY))
    for path in files:
        text = read(path)
        if text is not None:
            new, n = _jsonl(text, event)
            if n:
                out[path.relative_to(state).as_posix()] = (text, new, n)
    usage = state / "usage.json"
    text = read(usage)
    if text is not None:
        try:
            data = json.loads(text)
            reads = data.get("reads")
            if isinstance(reads, dict) and any(one(p) for p in reads):
                n = sum(1 for p in reads if one(p))
                data["reads"] = dict(sorted(((one(p) or p), c) for p, c in reads.items()))
                out["usage.json"] = (text, json.dumps(data, ensure_ascii=False, indent=1) + "\n", n)
        except (ValueError, AttributeError):
            pass
    vectors = state / "embeddings" / "index.json"
    text = read(vectors)
    if text is not None:
        try:
            meta = json.loads(text)
            notes = meta.get("notes")
            if isinstance(notes, dict) and any(one(p) for p in notes):
                n = sum(1 for p in notes if one(p))
                meta["notes"] = {(one(p) or p): v for p, v in notes.items()}
                out["embeddings/index.json"] = (text, json.dumps(meta, ensure_ascii=False, separators=(",", ":")), n)
        except (ValueError, AttributeError):
            pass
    inbox = state / "inbox"
    for path in sorted(inbox.glob("*.md")) + sorted((inbox / "done").glob("*.md")):
        text = read(path)
        if text is None:
            continue
        rel = path.relative_to(state).as_posix()
        new, _ = rewriter.text(text, None, where=f"{state.name}/{rel}")
        end = new.find("\n---", 3) if new.startswith("---") else -1
        if end >= 0:       # `note:` (the note an accepted candidate became) is a plain path
            new = re.sub(r'(?m)^(note:[ \t]*)("?)(.+?)("?)[ \t]*$',
                         lambda m: m.group(1) + m.group(2) + (one(m.group(3)) or m.group(3)) + m.group(4),
                         new[:end]) + new[end:]
        if new != text:
            n = sum(1 for a, b in zip(text.splitlines(), new.splitlines()) if a != b)
            out[rel] = (text, new, n)
    cases = read(state / "eval.tsv")
    if cases:
        for line in cases.splitlines():
            query, _, fragment = line.partition("\t")
            hits = [(o, n) for o, n in moves.items() if fragment.strip() and fragment.strip().lower() in o.lower()]
            if hits and all(fragment.strip().lower() not in n.lower() for _, n in hits):
                warnings.append(f"{state.name}/eval.tsv: the case {query.strip()!r} expects {fragment.strip()!r}, "
                                f"which no longer matches {hits[0][1]}: update it by hand")
    return out, warnings


# ───────────────────────────────────────────────────────────────── run
def _vault_files(cfg):
    """Every file Obsidian sees: notes, attachments, captures (hidden folders skipped)."""
    return [rel for _, rel in C.walk(cfg, md_only=False, prune_excluded=False)]


def _read(path):
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _backup_dir(state):
    base = state / "backups" / f"move-{dt.datetime.now():%Y%m%d-%H%M%S}"
    target, n = base, 1
    while target.exists():
        n += 1
        target = Path(f"{base}-{n}")
    return target


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".move-tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def run(cfg, plan_text, apply=False):
    """Check the plan and list every change; with `apply`, back up and make them."""
    vault, state = Path(cfg["_vault"]), Path(cfg["_state"])
    checked = plan(cfg, plan_text)
    entries, moves = checked["entries"], checked["moves"]
    before = _vault_files(cfg)
    rewriter = Rewriter(before, moves)
    raw = _raw_config(cfg)
    post_files = [moves.get(x, x) for x in before]
    new_raw, config_changes, config_warnings = _config(cfg, raw, entries, moves, post_files)
    old_captures = C.captures_folder(cfg)
    new_captures = C.captures_folder(new_raw if isinstance(new_raw.get("captures"), dict) else cfg)

    notes, links, warnings = {}, [], []
    olds = sorted({old for _, old, _, _ in entries} | {o[:-3] for o in moves if o.endswith(".md")})
    for rel in before:
        new = moves.get(rel, rel)
        if rel.endswith(VIEWS):
            text = _read(vault / rel) or ""
            named = [o for o in olds if o in text]
            if named:
                warnings.append(f"{new}: names {named[0]}, which moved: Obsidian views are not rewritten, check it")
            continue
        if not rel.lower().endswith(".md") or IX.SENSITIVE.search(rel):
            continue
        text = _read(vault / rel)
        if text is None:
            continue
        capture = any(_under(p, f) for p in (rel, new) for f in (old_captures, new_captures))
        updated, changes = rewriter.text(text, rel)
        if not changes:
            continue
        if capture:
            for old_link, new_link in changes:
                warnings.append(f"{new}: a capture is never edited; it still says {old_link} "
                                f"(now {new_link})")
            continue
        notes[rel] = (text, updated)
        links += [{"p": new, "from": a, "to": b} for a, b in changes]
    state_changes, state_warnings = _state(cfg, moves, rewriter)
    warnings = rewriter.warnings + config_warnings + warnings + state_warnings

    files = [{"from": old, "to": new} for old, new in sorted(moves.items())]
    result = {
        "applied": False, "files": files, "links": links, "config": config_changes,
        "state": [{"file": f, "lines": n} for f, (_, _, n) in sorted(state_changes.items())],
        "warnings": list(dict.fromkeys(warnings)),
        "counts": {"files": len(moves), "notes": sum(1 for x in moves if x.lower().endswith(".md")),
                   "other": sum(1 for x in moves if not x.lower().endswith(".md")),
                   "links": len(links), "notes_rewritten": len(notes), "config": len(config_changes),
                   "state": sum(n for _, _, n in state_changes.values())},
    }
    if not apply:
        return result

    # Obsidian autosaves: a note changed since it was read is not overwritten.
    changed = [rel for rel, (text, _) in notes.items() if _read(vault / rel) != text]
    if changed:
        raise MoveError(4, "notes changed while the move was being prepared: nothing moved, run it again",
                        changed)
    target = _backup_dir(state)
    for rel in sorted(set(moves) | set(notes)):
        (target / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(vault / rel, target / rel)
    saved = target / state.name
    saved.mkdir(parents=True, exist_ok=True)
    (saved / "move-plan.tsv").write_text(plan_text, encoding="utf-8")
    for rel in ["config.json"] + sorted(state_changes):
        if (state / rel).exists():
            (saved / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(state / rel, saved / rel)

    # Every file goes to a staging folder first, then to its place: a swap of letter case
    # (memory → Memory) works on disks that ignore case.
    staging = state / f"move-staging-{target.name}"
    staging.mkdir(parents=True)
    order = sorted(moves.items())
    for i, (old, _) in enumerate(order):
        os.replace(vault / old, staging / str(i))
    for _, old, _, is_folder in entries:
        if is_folder and (vault / old).is_dir():
            for root, dirs, files in os.walk(vault / old, topdown=False):
                if not os.listdir(root):
                    os.rmdir(root)
    for i, (_, new) in enumerate(order):
        (vault / new).parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging / str(i), vault / new)
    for new in checked["empty"].values():
        (vault / new).mkdir(parents=True, exist_ok=True)
    staging.rmdir()

    for rel, (_, text) in sorted(notes.items()):
        N._write_atomic(vault / moves.get(rel, rel), text)
    if config_changes:
        _write(state / "config.json", json.dumps(new_raw, ensure_ascii=False, indent=1) + "\n")
    for rel, (_, text, _) in sorted(state_changes.items()):
        _write(state / rel, text)
    result["applied"], result["backup"] = True, str(target)
    return result
