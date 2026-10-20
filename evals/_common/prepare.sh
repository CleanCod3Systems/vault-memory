#!/usr/bin/env bash
# Scaffold for every case: the workspace becomes an indexed copy of the example vault.
# The hook and the CLI find it through EVAL_VAULT_MEMORY_VAULT=. (cwd = workspace).
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
cp -R "$REPO/tests/example_vault/." .
python3 -B "$REPO/scripts/cli.py" --vault . index >/dev/null
