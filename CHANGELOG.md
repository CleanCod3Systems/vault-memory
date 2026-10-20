# Changelog

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
