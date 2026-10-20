# vault-memory as an MCP server

`vault-memory mcp` exposes the same engine over stdio (JSON-RPC 2.0, stdlib only) with four
tools: `recall`, `read_passage`, `write_note` and `doctor`. It is for clients without a shell or
hooks. In **Claude Code it is not needed**: the plugin hook and the `vault-memory` command cover
the same without adding tool definitions to every session.

The command is the shim written by `vault-memory install-cli` (`~/.local/bin/vault-memory`),
or `python3 <plugin>/scripts/cli.py`. Each client says **which vault** it uses through
`VAULT_MEMORY_VAULT`. One client, one vault.

## Claude desktop (chat)

`claude_desktop_config.json`, inside `mcpServers`:

```json
"vault-memory": {
  "command": "/Users/<user>/.local/bin/vault-memory",
  "args": ["mcp"],
  "env": {"VAULT_MEMORY_VAULT": "/Users/<user>/path/to/Vault"}
}
```

Restart the app.

## Codex (`~/.codex/config.toml`)

The Codex plugin registers the server (`codex-mcp.json`); it needs `vault-memory` on PATH and
the vault in the environment. By hand:

```toml
[mcp_servers.vault-memory]
command = "/Users/<user>/.local/bin/vault-memory"
args = ["mcp"]
env = { VAULT_MEMORY_VAULT = "/Users/<user>/path/to/Vault" }
```

## Gemini CLI

The extension registers the server and asks for the vault path on install. By hand, in
`~/.gemini/settings.json`:

```json
{"mcpServers": {"vault-memory": {"command": "/Users/<user>/.local/bin/vault-memory", "args": ["mcp"],
  "env": {"VAULT_MEMORY_VAULT": "/Users/<user>/path/to/Vault"}}}}
```

## Cursor (`~/.cursor/mcp.json`)

```json
{"mcpServers": {"vault-memory": {"command": "/Users/<user>/.local/bin/vault-memory", "args": ["mcp"],
  "env": {"VAULT_MEMORY_VAULT": "/Users/<user>/path/to/Vault"}}}}
```

## GitHub Copilot CLI (`~/.copilot/mcp-config.json`)

```json
{"mcpServers": {"vault-memory": {"type": "local", "command": "/Users/<user>/.local/bin/vault-memory",
  "args": ["mcp"], "env": {"VAULT_MEMORY_VAULT": "/Users/<user>/path/to/Vault"}, "tools": ["*"]}}}
```

## Try it by hand

```bash
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"recall","arguments":{"q":"checkout down"}}}' \
  | VAULT_MEMORY_VAULT=/path/to/Vault vault-memory mcp
```
