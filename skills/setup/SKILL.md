---
name: setup
description: 'Install or configure vault-memory on an Obsidian vault: find the vault, choose modules, map folders to areas and types, and normalize frontmatter without moving files. Use for "set up vault memory", "vault-memory init", "turn modules on or off", or when a vault is not initialized.'
---

# setup

Reply in the user's language.

1. Vault: `VAULT_MEMORY_VAULT` from the agent environment (or ask the user for the path). Never
   use another profile's vault.
2. `vault-memory init --vault "<path>" --wizard` (or `--profile minimal|standard|team`). It
   detects the search language from the notes. Show the proposed config (areas and types by
   folder) and **ask for confirmation** before writing.
3. `vault-memory doctor` → `vault-memory fix` (dry run) → with an OK, `vault-memory fix --apply`.
   A vault made by an earlier version: `vault-memory migrate` (dry run) → with an OK, `--apply`.
4. `vault-memory refresh`. Optional: `vault-memory schedule --at 07:30 --install` (daily refresh).
5. Suggest an `eval.tsv` in the state folder with 15–30 real questions
   (`query<TAB>expected path`) to measure recall with `vault-memory eval` and `--hook`.

No `vault-memory` on PATH: `python3 <plugin>/scripts/cli.py install-cli`.
Modules: `vault-memory modules --on X --off Y [--clean]`. Never move or rename notes.
Full walkthrough, folder mapping and edge cases: [steps.md](steps.md).
