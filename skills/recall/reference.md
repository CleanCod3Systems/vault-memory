# recall — full reference

Read only when SKILL.md is not enough.

The vault already has its own index. It is local, deterministic, takes less than a second and
spends no tokens. **Querying it first is faster and cheaper than any external sweep.**

CLI: `vault-memory`, or `python3 <plugin>/scripts/cli.py`. The vault comes from
`VAULT_MEMORY_VAULT` (set it in your agent's environment) or `--vault`; otherwise it is found by
walking up from the current folder to a `.obsidian/`. `vault-memory info` says which vault and
which version are in use.

## Search

```bash
vault-memory recall "payment declined by fraud"      # free text
vault-memory recall 884213                            # order / case / record ID
vault-memory recall PROJ-412                          # ticket key
vault-memory recall "502 gateway" --type incident --n 5
vault-memory recall --like "2026-06-11 - Payments API 502"   # similar notes
vault-memory recall "..." --json                      # to chain with another script
```

Filters: `--type` (`incident`, `runbook`, `doc`, `meeting`, `decision`, `project`,
`reference`, `daily`, `period`, `home`), `--family` (`record`, `knowledge`, `structure`),
`--area`, `--status`, `--system`, `--since YYYY-MM-DD`, `--n`, `--archive` (includes archived
folders).

Each result carries path, type, area, date, status, severity, the paragraph that actually
matches, and the references it has: ticket, IDs, link to the source conversation.

The index refreshes itself when it went stale (a new, changed or deleted note). `--no-auto`
turns that off.

Each `--json` result carries `state`: `ok`, `replaced` (with `replaced_by`), `expired` or
`unverified`. Replaced and expired notes already rank lower; **never answer from a `replaced` or
`expired` note** without saying so and checking `replaced_by`.

## What to do with the result

- **Match** → answer from the note, **citing the path**. If the user wants the detail, read
  only that note, not the whole vault.
- **Partial match** → use what the vault gives (names, IDs, dates, tickets) to sharpen the
  external search instead of starting blind.
- **No match** → say so explicitly ("it is not in the vault") and *only then* go to external
  sources. What is found outside and is worth it goes back to the vault: it is the only way
  it will be there next time.

Always separate what a note says from your own inference.

## Saving memory

A new fact worth remembering is saved with `vault-memory note`, **never by creating the file by
hand**: the command validates the contract and looks for duplicates before writing.

```bash
vault-memory note --title "Database maintenance window" --type doc --folder Memory \
  --body "Sundays from 2 to 4 h…" --source "platform meeting 2026-09-27" \
  --verified today --key maintenance-window --json
```

What deserves a note: facts that cross repos and tools, decisions without a repo, reproducible
gotchas, research results that will be reused. What does not: what the repo, a ticket or the
docs already record, temporary state of one conversation, credentials or personal data.

- **Exit 3 with `duplicates`** → a note already covers this. Read it and choose: update it
  (`--update PATH`, with `--append` to add a dated section) or, if the fact changed, create the
  new one with `--replaces "Old note name"`. `--force` only if they really are different.
- **Exit 2** → a required field is missing or a value is invalid: the message says which. Do
  not invent values to make it pass.
- `--verified today` on an existing note (`--update PATH --verified today`) confirms it is still
  true without touching anything else.
- `key` = the fact the note talks about (slug). Two current notes with the same key are a
  contradiction that `doctor` reports.

## Hard limits

- At most **4** `recall` calls per user question. If it did not show up by the fourth, it is
  not there: say so and go to external sources.
- Never read more than **3** whole notes per question; the paragraph `recall` returns is
  usually enough.
- If there is no index, run `vault-memory index` once. If it fails, report the error verbatim
  and continue without recall — do not retry in a loop.
- If the vault is not initialized, the CLI says so: that is the `setup` skill's job, do not
  improvise a search with `grep`.
- Recall works with any combination of modules. What changes is which references each result
  carries — without `tickets` there are no ticket keys, without `chat` no source conversation
  link, without `ids` no exact number search. Check `vault-memory modules` before concluding a
  piece of data is not in the vault.
