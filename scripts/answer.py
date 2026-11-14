"""vault-memory answer — keep a good answer from a conversation as a new note, under control.

An answer the `ask` skill gave from several notes can be saved once the user says yes. It is
written through `note.write` (contract, duplicate detection, safe writes) into its own folder,
never verified: it says where it came from (`source: ask <day>`), which notes it was derived
from (`derived_from`), and when it expires unless someone reads it. Like a learned note it
enters the review queue until a review settles it, but its reviews do not count toward
learned-note precision (calibration.py): it was not written by distill.

Config `answers` (all optional):
    folder        "Memory/answers"   deliberately outside the learned folder
    expires_days  90                 reading the answer moves it forward (`upkeep`)
    min_cites     2                  fewer notes than this is not a combined answer

The same function is used by the CLI and the MCP server: it returns data, it does not print.
"""
import datetime as dt

import core as C
import distill as DS
import freshness as FR
import note as N
import recall as RC
import telemetry as TL

DEFAULTS = {"folder": "Memory/answers", "expires_days": 90, "min_cites": 2}
SOURCE = "ask"
TITLE_MAX = 100


def options(cfg):
    return {**DEFAULTS, **(cfg.get("answers") or {})}


def folder(cfg):
    return options(cfg)["folder"].strip().strip("/")


def is_answer(cfg, rel):
    return rel.startswith(folder(cfg) + "/")


def _title(question):
    text = " ".join(question.split())
    if len(text) <= TITLE_MAX:
        return text
    return text[:TITLE_MAX].rsplit(" ", 1)[0] or text[:TITLE_MAX]


def check_cites(cfg, cites):
    """Paths of the cited notes, in the order given and without repeats. Every cite must be a
    current note of the vault; an invalid one stops the save (exit 2) with its reason."""
    opts = options(cfg)
    by_path = {d["p"]: d for d in RC.load(cfg)["docs"]}
    captures = str((cfg.get("captures") or {}).get("folder") or "").strip().strip("/")
    paths, problems = [], []
    for raw in cites or []:
        rel = str(raw).strip().replace("\\", "/").lstrip("/")
        if not rel:
            continue
        if not rel.endswith(".md"):
            rel += ".md"
        if rel in paths:
            continue
        d = by_path.get(rel)
        state = FR.state(d, cfg["freshness"]) if d else None
        if captures and rel.startswith(captures + "/"):
            problems.append(f"{rel}: a capture, not a note")
        elif d is None:
            problems.append(f"{rel}: not a note of this vault")
        elif d["archived"]:
            problems.append(f"{rel}: archived")
        elif d.get("template"):
            problems.append(f"{rel}: a template")
        elif state not in FR.CURRENT:
            problems.append(f"{rel}: {state}, not current")
        else:
            paths.append(rel)
    if problems:
        raise N.NoteError(2, "cites must be current notes: " + "; ".join(problems), {"invalid": problems})
    need = int(opts["min_cites"])
    if len(paths) < need:
        raise N.NoteError(2, f"a saved answer needs at least {need} cited notes, got {len(paths)}")
    if all(is_answer(cfg, rel) for rel in paths):
        raise N.NoteError(2, "at least one cite must be a note that is not a saved answer")
    return paths


def save(cfg, question, body, cites, title=None, dry_run=False, origin="cli"):
    """Write the answer as a note. Raises note.NoteError: 2 invalid input, 3 duplicates."""
    question = DS.redact(" ".join((question or "").split()))
    body = DS.redact((body or "").strip())
    if not question:
        raise N.NoteError(2, "--question is required: what was asked")
    if not body:
        raise N.NoteError(2, "the answer is empty: pass --body, --body-file or --stdin")
    paths = check_cites(cfg, cites)
    title = _title(DS.redact(title) if title and title.strip() else question)
    links = [N._link_to(rel) for rel in paths]
    text = (f"## Question\n\n{question}\n\n## Answer\n\n{body}\n\n## Sources\n\n"
            + "\n".join(f"- {link}" for link in links))
    target = folder(cfg)
    name = N.FORBIDDEN.sub("-", title).strip(" .-")
    if (cfg["_vault"] / target / f"{name}.md").exists():
        raise N.NoteError(3, "a saved answer with this title already exists: update it with "
                             "vault-memory note --update, or pass another --title",
                          {"duplicates": [{"p": f"{target}/{name}.md", "n": name, "score": 1.0,
                                           "reason": "same title"}]})
    # Duplicates are judged on the question and the answer only: the Sources list repeats the
    # cited note names and would make two short answers from the same notes look identical.
    draft = N._draft(title, "note", f"{question}\n\n{body}", "")
    found = N.duplicates(cfg, RC.load(cfg), draft, family=C.families(cfg).get("note"))
    if found:
        raise N.NoteError(3, "there are notes that already cover this: update one with "
                             "vault-memory note --update, or pass another --title", {"duplicates": found})
    today = C.today()
    expires = (today + dt.timedelta(days=int(options(cfg)["expires_days"]))).isoformat()
    result = N.write(cfg, title=title, note_type="note", folder=target, body=text,
                     source=f"{SOURCE} {today.isoformat()}", expires=expires, force=True,
                     fields={"derived_from": ['"' + link + '"' for link in links]}, dry_run=dry_run)
    if not dry_run:
        TL.record(cfg["_vault"], {"ev": "answer", "cites": len(paths),
                                  "answers_cited": sum(is_answer(cfg, rel) for rel in paths),
                                  "origin": origin}, cfg)
    return {**result, "cites": paths}


def upkeep(cfg):
    """(renewed, archived): answers read lately stay alive, the ones that expired unread move
    to the archive. The same code as learned notes (distill.py)."""
    opts = options(cfg)
    mine = SOURCE + " "
    renewed = DS.renew_used(cfg, folder=folder(cfg), source=mine, expires_days=int(opts["expires_days"]))
    return renewed, DS.archive_expired(cfg, folder=folder(cfg), source=mine)
