# Changelog

## 0.3.0 — 2026-11-11

- Daily summary (`scripts/today.py`, `hooks/today.py`, SessionStart `startup`): once per local
  day, interactive sessions only (`today.entrypoints`, default `cli`), up to 8 lines with what is
  next, what is waiting on someone, what is due soon and what was learned yesterday. Also
  `vault-memory today [--all] [--json]`. Off until `today.type` is set; field names come from the
  config, so task notes keep their own frontmatter.
- Learning from sessions (`scripts/distill.py`, `vault-memory distill`, and `refresh` when
  `distill.enabled`): redacts yesterday's Claude Code sessions, proposes at most 3 cross-project
  facts per session, keeps only those quoted verbatim, confirmed by a second model check and
  free of sensitive data. Inbox for review by default (`distill.auto: false`); with `auto` it
  writes through `vault-memory note` with `expires`, and a learned note that gets read is
  confirmed. Providers `claude-cli` and `anthropic-api`. Off by default.
- `VAULT_MEMORY_DISABLE=1` turns every hook off (set on the model calls distill makes).

## 0.2.0 — 2026-11-10

- `hook.mode` (`all` default, `first`, `off`): `first` searches only the first prompt of a session
  and prompts that carry an ID or ticket. The hook also ignores prompts that start with
  `<agent-message` or `<pasted_content` (not typed by a person).
- Gate before external searches (`hooks/gate.py`, PreToolUse): for tools listed in `gate.tools`
  it adds one `[vault] already documented: <path> — <passage>` line when a single current note
  clearly answers the query. Never blocks, never repeats a note in a session, logs
  `gate_hit`/`gate_miss`. Uses the existing index without reindexing; caches it as
  `gate-index-*.bin` in the state folder.
- Telemetry reads `CLAUDE_CODE_SESSION_ID` (sessions were all logged as `cli`, so the Stop hook
  never matched them) and `usage --tokens` counts hook pointers sent as a list of strings.

## 0.1.2 — 2026-10-20

- No top-level `bin/` folder: claude.ai chat and Cowork refuse a plugin that has one, so the
  plugin could not be added there. The terminal command is the `~/.local/bin/vault-memory` shim
  (`install-cli`); skills fall back to `scripts/cli.py` inside the plugin.

## 0.1.1 — 2026-10-20

- `migrate` also rewrites Obsidian Bases (`.base` views): legacy keys in filters, formulas and
  property lists, and legacy type values. Before, migrating a vault left its views filtering on
  keys that no longer existed.

## 0.1.0 — 2026-10-20

First public release of vault-memory, the successor of obsidian-cerebro.

- **English everywhere**: CLI, config, frontmatter contract, skills, hooks, MCP tools and docs.
  The agent replies in the user's language.
- **Any agent, any model**: manifests for Claude Code, Codex, GitHub Copilot CLI and Gemini CLI;
  Agent Skills format; MCP server for clients without a shell; `AGENTS.md` instructions.
- **Search in English and Spanish**: light stemmers for both, `language.stemmer` detected by
  `init` (`en`, `es` or `none`).
- **Quieter hook**: prompts without content words ("ok, go ahead", "dale, seguí", "thanks") no
  longer trigger pointers; an ID or ticket the note mentions counts as a match. Example vault:
  hook hit@3 20/20 and 0 of 7 conversational prompts with a pointer. CI gates both.
- **`curate` skill**: weekly read-only proposals from doctor, patterns and usage data.
- **`migrate`**: vaults made by obsidian-cerebro keep working as they are (Spanish keys, type
  values, config and `.cerebro/` are read transparently) and can be rewritten with
  `vault-memory migrate --apply`, every note backed up first.
- **`install-cli`**: writes a `vault-memory` shim that finds the installed plugin copy by itself.
- **Hosted catalog**: `make release` builds `marketplace.json` + a zip with a pinned sha256 for
  installing without git.
- MCP: an unknown tool now returns -32602 (it returned -32601).
