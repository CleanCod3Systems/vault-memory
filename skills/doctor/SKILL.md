---
name: doctor
description: 'Health of the Obsidian vault memory: frontmatter contract, broken links, orphans, duplicates, freshness (expired notes, contradictions), what repeats and what got stuck; normalize and build the panel. Use for "check the vault", "vault doctor", "what is broken".'
---

# doctor

Reply in the user's language. Read-only diagnosis; nothing is written without `--apply`.

```bash
vault-memory doctor              # health + freshness (--json to chain)
vault-memory fix                 # which frontmatter is missing (dry run)
vault-memory fix --apply         # fill it in, with a backup in the state folder
vault-memory usage               # does memory help? (pointers used, missed recall, gaps)
vault-memory refresh             # index + patterns + related + panel
vault-memory panel --open
```

Report order: 1) contradictions (same `key` current twice: a sure error), 2) expired and
replaced, 3) near duplicates (propose `vault-memory note --update A --replaces B`), 4) contract
and broken links, 5) stale records and what repeats without a runbook.
If doctor says legacy names are in use, propose `vault-memory migrate` (dry run first).
Propose concrete fixes; do not rewrite notes on your own initiative.

Detail of each block, modules and edge cases: [reference.md](reference.md).
