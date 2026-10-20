---
name: curate
description: 'Weekly read-only curation of the vault memory: what to merge, close, retype or stop suggesting, from doctor, patterns and usage data. Proposes changes and never writes. Use for "curate the vault", "clean up memory", "is memory helping?", "what should I fix in my notes this week".'
---

# curate

Reply in the user's language. Read only: this skill **proposes**, the user decides, and any
write goes through `vault-memory note`, `fix` or `migrate` after an explicit OK.

## Gather (no writes)
```bash
vault-memory doctor --json
vault-memory usage --json          # pointers used, never read, missed recall, gaps
vault-memory patterns              # recurrence and stale records (refreshes derived files only)
```

## Propose, at most 5 items, most valuable first
1. **Contradictions and near duplicates** → which note survives; the command:
   `vault-memory note --update KEEP --replaces "OTHER"`.
2. **Stale open records** (open for more than `patterns.stale_days`) → close, escalate or
   update each one; never change a status without the user.
3. **Out-of-vocabulary types** → add them to `config.json → extra_types` or fix the notes.
4. **Hook noise** → notes suggested and never read (`usage.never_read`): propose
   `recall.skip_paths` for whole folders that are noise, or a better title for the note.
   Missed recall (`usage.missed_recall`): propose those queries as new `eval.tsv` cases.
5. **Gaps** (searches without results) → propose the note to write, with its type and folder.

Each item: what, why (the data that shows it), the exact command, and the effort. End with the
single highest-value action. If everything is healthy, say so in one line.
