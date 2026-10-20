---
name: search
description: 'Search the vault memory: /vault-memory:search query. One line and a short passage per note.'
disable-model-invocation: true
argument-hint: "[query]"
---

Vault memory results for: $ARGUMENTS

!`python3 "${CLAUDE_SKILL_DIR}/../../scripts/cli.py" recall "$ARGUMENTS" --brief --n 5 2>&1 || echo "(vault-memory did not answer: check the vault with  vault-memory info)"`

Answer from these passages, citing the path, in the user's language. Read a whole note only if
the passage is not enough. If a line says `[replaced]` or `[expired]`, do not answer from it
without saying so. No results: say it is not in the vault before searching elsewhere.
