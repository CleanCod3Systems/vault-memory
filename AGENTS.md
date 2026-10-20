# vault-memory — instructions for any agent

The user keeps an Obsidian vault as long-term memory. Use it before searching anywhere else,
and save what is worth remembering. **Reply in the user's language**, whatever the language of
these instructions or of the notes.

## Tools

- With a shell: the `vault-memory` CLI (or `python3 <plugin>/scripts/cli.py`). The vault comes
  from `VAULT_MEMORY_VAULT` or `--vault`.
- Without a shell: the MCP server `vault-memory mcp` exposes `recall`, `read_passage`,
  `write_note` and `doctor`.

## Read

1. Before answering a question about past work, decisions, incidents, IDs or tickets, run
   `vault-memory recall "<3-6 words>" --brief --n 5` (or the MCP `recall` tool).
2. Answer from the passage and **cite the note path**. Read a whole note only when the passage
   is not enough (max 3 notes, max 4 recalls per question).
3. A result marked `replaced` or `expired` is not current: say so and check `replaced_by`.
4. No match → say it is not in the vault, then search elsewhere.

## Write

Save facts that will be useful again and are not recorded elsewhere (repo, ticket, docs):

```bash
vault-memory note --title "…" --type doc --folder Memory --body "…" --source "…" --verified today --json
```

- Exit 3 (`duplicates`): update the existing note (`--update PATH`) or replace it
  (`--replaces "Old note"`). Never create a second version.
- Exit 2: a required field is missing; fill it in, never invent it.
- Never write credentials, tokens or personal data.

## Never

- Never create or rewrite notes by hand when `vault-memory note` can do it.
- Never run `fix --apply`, `retype --apply` or `migrate --apply` without the user's explicit OK.
- Never move, rename or delete notes.
