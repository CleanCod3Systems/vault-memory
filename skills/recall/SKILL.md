---
name: recall
description: 'Search the Obsidian vault (the user memory) before searching anywhere else, and save new facts with vault-memory note. Use for "did this happen before?", "what do we know or decide about X?", "what do I have today?", "what is pending?", an ID or ticket, "search my notes", or when something worth remembering was learned.'
---

# recall

Reply in the user's language, whatever the language of this file or of the notes.

The vault has its own index: local, deterministic, < 1 s, no tokens. **Query it before any
external search.** CLI `vault-memory` (vault: `VAULT_MEMORY_VAULT` or `--vault`;
`vault-memory info` says which one and which version). Without the CLI on PATH (Cowork, or
before `install-cli`): `python3 "${CLAUDE_SKILL_DIR}/../../scripts/cli.py"`. Agents without a shell use the MCP server (`recall`,
`read_passage`, `write_note`).

## Search
```bash
vault-memory recall "<3-6 words>" --brief --n 5   # default: one line and a short passage per note
vault-memory recall 48213 · vault-memory recall ABC-412   # exact ID or ticket
vault-memory recall "<kw>" --type incident|decision|runbook --n 5
```
- Queries work in any language the notes are written in; use the words the notes would use.
- Answer from the passage, **citing the path**. Read the whole note only if the passage is not
  enough (max 3 notes per question, max 4 recalls).
- `state` of each result: `replaced` (see `replaced_by`), `expired` or `outdated` (a review
  found the source says otherwise) → do not answer from it without saying so.
- **Say how sure the answer is**, from the note's own fields: "verified 2026-11-10, 9/10"
  (`verified`, `confidence`), or "not verified since <date>" when it has neither. When the
  answer matters and the note is not verified, offer to check it at the source (skill
  `verify`).
- No match → say so ("it is not in the vault") and only then search elsewhere.

## Today and pending work
```bash
vault-memory today --all     # what is next, waiting on someone, due soon (from task notes)
vault-memory today --json    # the same, structured
```
- For "what do I have today / what is pending / who am I waiting on". Off when the config has
  no `today` block: then say so and search task notes with `recall --type <task type>`.
- Notes under the `distill.folder` with `source: distill …` were learned from past sessions by
  a model: cite them as such ("learned on <date>"), and prefer a note the user wrote when both
  answer.

## Save memory
```bash
vault-memory note --title "…" --type doc --folder Memory --body "…" \
  --source "…" --verified today [--key slug] --json
```
- Save only what will be useful again and is not recorded elsewhere (repo, ticket, docs).
- Exit **3** (`duplicates`): update the existing note (`--update PATH [--append]`) or replace it
  (`--replaces "Old note"`); `--force` only if they really are different things.
- Exit **2**: a field is missing or a value is invalid; fill it in, never invent it.
- Confirm a note is still true after checking its source:
  `vault-memory review PATH --verdict confirmed --confidence 9 --evidence "<source>: <what>"`
  (skill `verify`). Reading a note is not confirming it.

Filters, `--like`, `--json`, limits and edge cases: [reference.md](reference.md).
