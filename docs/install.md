# Install vault-memory

Each agent profile installs its own copy of the plugin and points to its own vault. Two
profiles never share a vault.

## 1. Plugin

Claude Code, from GitHub:

```bash
claude plugin marketplace add CleanCod3Systems/vault-memory
claude plugin install vault-memory@vault-memory
```

Claude Code without git (≥ 2.1.224), from the hosted catalog (a zip with a pinned sha256):

```bash
claude plugin marketplace add https://vault-memory-vert.vercel.app/marketplace.json
claude plugin install vault-memory@vault-memory
```

Codex, Copilot CLI, Gemini CLI and other agents: see the README.

## 2. The profile's vault

In the profile `settings.json` (Claude Code: `$CLAUDE_CONFIG_DIR/settings.json`):

```json
"env": { "VAULT_MEMORY_VAULT": "/absolute/path/to/vault" }
```

The recall hook, the `vault-memory` command and the MCP server inherit it. Restart the agent.
Other agents: export `VAULT_MEMORY_VAULT` in their environment or MCP config.

## 3. The `vault-memory` command (once per machine)

```bash
python3 <plugin>/scripts/cli.py install-cli     # writes ~/.local/bin/vault-memory
vault-memory info                               # vault, version, code and language in use
```

The shim looks up the installed plugin copy on every call (`VAULT_MEMORY_HOME`, then the active
Claude Code profile), so plugin updates need no reinstall.

## 4. New or existing vault

- New: `vault-memory init --vault "/path" --wizard` (or ask the agent to "set up vault memory",
  skill `setup`).
- Existing: `vault-memory doctor` → `vault-memory fix --apply` → `vault-memory refresh`.
- Made by obsidian-cerebro: it works as is; `vault-memory migrate` (dry run) → `--apply`
  rewrites the Spanish names and moves `.cerebro/` to `.vault-memory/`.
- Optional: `vault-memory schedule --at 07:30 --install` (daily refresh).
- Suggested: an `eval.tsv` in the state folder with 15–30 real questions
  (`query<TAB>expected path`) to measure recall with `vault-memory eval` before and after
  changing the config.

## 5. Clients without a shell

Desktop chat, Codex, Cursor, Gemini, Copilot: [mcp.md](mcp.md).

## Update

```bash
claude plugin marketplace update vault-memory && claude plugin update vault-memory@vault-memory
```

An index from an earlier version rebuilds itself on the first `recall`.
