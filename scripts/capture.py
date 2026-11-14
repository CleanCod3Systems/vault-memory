"""vault-memory capture — keep raw material exactly as it arrived, so notes can cite it.

A capture is the evidence a note is checked against: a thread, an issue, a page, pasted as it
came. It lives in `<captures.folder>/<date> <slug>.md` (default `Captures/`) with frontmatter
`type: capture`, `title`, `captured`, `url` (optional) and `sha256` (hex of the body as
written), then the body as received.

Redaction comes first: the body (and title and url) goes through `distill.redact` before
anything is written, so credentials, tokens and personal data never land in the vault. What is
stored is the redacted text; `sha256` is computed over that.

A capture is never edited by vault-memory: it is never overwritten (the same content returns the
existing path, other content gets a numeric suffix), it stays out of the index, recall, hook and
gate, and note/review/distill refuse to write to it (note.refuse_capture). `ingest --capture`
learns from one and every candidate carries `capture: "[[<path>]]"`, a link to its evidence.
`doctor` reports captures whose body no longer matches their hash and notes that cite a missing one.

Returns data and raises CaptureError; it does not print.
"""
import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path

import core as C
import distill as DS
import sources as SR
import telemetry as TL

TITLE_MAX = 120
SLUG_MAX = 60
RE_LINK = re.compile(r"\[\[([^\]|#\n]+)")


class CaptureError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def folder(cfg):
    """The captures folder relative to the vault, refused if it would leave the vault."""
    rel = C.captures_folder(cfg)
    if ".." in rel.split("/") or os.path.isabs(rel) or rel.startswith("."):
        raise CaptureError(2, f"captures.folder: invalid folder {rel!r} (relative to the vault, no '..')")
    return rel


def sha256(body):
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _default_title(text):
    """The `title` of a JSON object, else the first heading, else the first non-empty line."""
    if text.lstrip().startswith("{"):
        try:
            data = json.loads(text)
            if isinstance(data, dict) and isinstance(data.get("title"), str) and data["title"].strip():
                return data["title"]
        except ValueError:
            pass
    heading = re.search(r"^#{1,6}\s+(.+)$", text, re.M)
    if heading:
        return heading.group(1)
    return next((line for line in text.splitlines() if line.strip()), "")


def _clean_title(title):
    return re.sub(r"\s+", " ", str(title or "")).strip().lstrip("#").strip()[:TITLE_MAX]


def split(text):
    """(frontmatter dict, body exactly as written) of a capture file's text."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    fm, _ = C.raw_frontmatter(text)
    rest = text[end + 4:]
    if rest.startswith("\r\n"):
        rest = rest[2:]
    elif rest.startswith("\n"):
        rest = rest[1:]
    return fm, rest


def _read(path):
    # newline="": the body is hashed byte for byte, so no line-ending translation on Windows
    with open(path, encoding="utf-8", newline="") as handle:
        return handle.read()


def _render(title, day, url, digest, body):
    head = {"type": "capture", "title": title, "captured": day, **({"url": url} if url else {}),
            "sha256": digest}
    lines = []
    for key, value in head.items():
        value = str(value)
        if value != value.strip() or value.startswith(("[", "{", "@", "!", "&", "*", "'", '"', "-", "#")) \
                or ": " in value or " #" in value:
            value = json.dumps(value, ensure_ascii=False)
        lines.append(f"{key}: {value}")
    return "---\n" + "\n".join(lines) + "\n---\n" + body


def write(cfg, text, title=None, url=None, date=None):
    """Store `text` as a capture. {"p", "sha256", "created"}: `created` is False when a capture
    with the same content was already there (nothing written)."""
    body = DS.redact(text or "")
    if not body.strip():
        raise CaptureError(2, "the material is empty")
    if date:
        try:
            day = dt.date.fromisoformat(str(date)).isoformat()
        except ValueError:
            raise CaptureError(2, f"--date: invalid date {date!r} (use YYYY-MM-DD)")
    else:
        day = C.today().isoformat()
    title = _clean_title(DS.redact(title) if title else _default_title(body)) or "capture"
    url = DS.redact(re.sub(r"\s+", " ", url or "").strip())[:300]
    digest = sha256(body)
    base = folder(cfg)
    stem = f"{day} {DS._slug(title)[:SLUG_MAX].strip('-') or 'capture'}"
    target_dir = cfg["_vault"] / base
    content = _render(title, day, url, digest, body)
    for n in range(1, 1000):
        rel = f"{base}/{stem}{'' if n == 1 else f'-{n}'}.md"
        path = cfg["_vault"] / rel
        if path.exists():
            fm, existing = split(_read(path))
            if fm.get("sha256") == digest and sha256(existing) == digest:
                result = {"p": rel, "sha256": digest, "created": False}
                break
            continue
        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            # "x": never overwrite, even if another process created it a moment ago
            with open(path, "x", encoding="utf-8", newline="") as handle:
                handle.write(content)
        except FileExistsError:
            continue
        result = {"p": rel, "sha256": digest, "created": True}
        break
    else:
        raise CaptureError(1, f"too many captures named {stem!r}")
    TL.record(cfg["_vault"], {"ev": "capture", "created": result["created"]}, cfg)
    return result


def resolve(cfg, given):
    """Path of a capture relative to the vault, from a path relative to it or absolute inside
    it. CaptureError(2) when it is not a capture file of this vault."""
    base = folder(cfg)
    raw = Path(str(given).strip()).expanduser()
    vault = Path(cfg["_vault"]).resolve()
    path = (raw if raw.is_absolute() else vault / raw).resolve()
    try:
        rel = path.relative_to(vault).as_posix()
    except ValueError:
        raise CaptureError(2, f"{given} is not inside the vault")
    if not rel.startswith(base + "/"):
        raise CaptureError(2, f"{rel} is not a capture: it must be under {base}/ (vault-memory capture)")
    if path.suffix != ".md" or not path.is_file():
        raise CaptureError(2, f"{rel} does not exist")
    return rel


def link(rel):
    return f"[[{rel[:-3] if rel.endswith('.md') else rel}]]"


def session(cfg, given, source=None, url=None, ref_id=None, date=None):
    """The ingest session of a capture: like `ingest --file` with its body, `--url` and
    `--date` defaulting to the capture's own, plus the `capture` link every candidate carries."""
    rel = resolve(cfg, given)
    fm, body = split(_read(cfg["_vault"] / rel))
    try:
        found = SR.material(body, source=source or "capture", url=url or fm.get("url"), ref_id=ref_id,
                            date=date or fm.get("captured"), today=C.today())
    except ValueError as e:
        raise CaptureError(2, str(e))
    found["capture"] = link(rel)
    return found


def report(cfg, docs):
    """Read only. {"n": captures, "edited": [paths whose body no longer matches sha256],
    "dangling": [{"p": note, "capture": link}] for notes citing a capture that does not exist}."""
    vault, base = cfg["_vault"], C.captures_folder(cfg)
    edited, n = [], 0
    root = vault / base
    if root.is_dir():
        for path in sorted(root.rglob("*.md")):
            if any(part.startswith(".") for part in path.relative_to(root).parts):
                continue
            n += 1
            try:
                fm, body = split(_read(path))
            except (OSError, UnicodeDecodeError):
                continue
            if fm.get("sha256") != sha256(body):
                edited.append(path.relative_to(vault).as_posix())
    dangling = []
    for d in docs:
        if "capture" not in d.get("fm", []):
            continue
        try:
            fm, _ = C.raw_frontmatter((vault / d["p"]).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        for target in RE_LINK.findall(fm.get("capture", "")) or [fm.get("capture", "")]:
            target = target.strip()
            if not target:
                continue
            rel = target if target.endswith(".md") else target + ".md"
            if not (vault / rel).is_file() and not (root / rel).is_file():
                dangling.append({"p": d["p"], "capture": target})
    return {"folder": base, "n": n, "edited": edited, "dangling": dangling}
