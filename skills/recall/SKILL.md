---
name: recall
description: 'Search the Obsidian vault (the user memory) before searching anywhere else, and save new facts with vault-memory note. Use for "did this happen before?", "what do we know or decide about X?", an ID or ticket, "search my notes", or when something worth remembering was learned.'
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
- `state` of each result: `replaced` (see `replaced_by`) or `expired` → do not answer from it
  without saying so.
- No match → say so ("it is not in the vault") and only then search elsewhere.

## Save memory
```bash
vault-memory note --title "…" --type doc --folder Memory --body "…" \
  --source "…" --verified today [--key slug] --json
```
- Save only what will be useful again and is not recorded elsewhere (repo, ticket, docs).
- Exit **3** (`duplicates`): update the existing note (`--update PATH [--append]`) or replace it
  (`--replaces "Old note"`); `--force` only if they really are different things.
- Exit **2**: a field is missing or a value is invalid; fill it in, never invent it.
- Confirm a note is still true: `vault-memory note --update PATH --verified today`.

Filters, `--like`, `--json`, limits and edge cases: [reference.md](reference.md).
