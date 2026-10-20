# 🧠 vault-memory

Turns an Obsidian vault into memory any coding agent can **ask**.

A normal vault is a place you write to. This plugin adds the half that is almost always
missing: reading it back. A local index, a frontmatter contract, a link graph, freshness
tracking and a synthesis of what repeats — all deterministic, no network, no model, zero tokens.

```
vault-memory recall "why did the gateway return 502"

1. 🔴 2026-06-11 - Payments API 502 at peak time   [22.3]
   incident · support · 2026-06-11 · open · P1 · payments-api
   Support/Cases/2026-06-11 - Payments API 502 at peak time.md
   The gateway returned 502 on pre-authorization. The service was restarted and orders
   stayed Pending; they had to be approved by hand. See order 884213.
   ids: 884213
```

Under a second. The answer was already written; what was missing was being able to find it.

Works with **Claude Code, Codex, Gemini CLI, GitHub Copilot CLI, Cursor and any MCP client**,
with any model. The plugin is written in English and the agent **answers in the user's
language**; search works over notes written in English or Spanish (others: `stemmer: none`).

## What it does

| | |
|---|---|
| **Recall** | Search by text, by ID (order, case, record) and by ticket key. Returns the exact paragraph, not the whole file. Refreshes itself when the vault changed |
| **Automatic recall** (Claude Code) | A hook leaves up to 3 pointers to relevant notes on each prompt — and stays silent on chat ("ok, go ahead", "thanks") |
| **Freshness** | Replaced, expired and unverified notes rank lower; two current notes about the same fact are reported as a contradiction |
| **Safe writing** | `vault-memory note` validates the contract and refuses duplicates: it makes you update or replace instead |
| **Contract** | Predictable frontmatter is what makes everything else possible. `doctor` says how much is missing; `fix` fills it **without overwriting anything and without moving files** |
| **Graph** | Backlinks, orphan notes, broken wikilinks and duplicate names |
| **Patterns** | What repeats, what repeats **without a written solution**, what has been open too long |
| **Panel** | A self-contained HTML page that searches the whole vault in the browser, no backend |
| **Curation** | The `curate` skill turns doctor + usage data into a short weekly list of what to merge, close or stop suggesting |

## Install

### Claude Code

```text
/plugin marketplace add CleanCod3Systems/vault-memory
/plugin install vault-memory@vault-memory
```

Without git (Claude Code ≥ 2.1.224), from the hosted catalog:

```text
/plugin marketplace add https://vault-memory-vert.vercel.app/marketplace.json
/plugin install vault-memory@vault-memory
```

Then, in the profile `settings.json`: `"env": {"VAULT_MEMORY_VAULT": "/path/to/vault"}`, and
ask the agent to *"set up vault memory"* (skill `setup`) or run
`vault-memory init --vault /path/to/vault --wizard`.

The `vault-memory` command for your terminal: `python3 <plugin>/scripts/cli.py install-cli`
(writes `~/.local/bin/vault-memory`).

### Codex

```bash
codex plugin marketplace add CleanCod3Systems/vault-memory
codex plugin add vault-memory@vault-memory
```

### GitHub Copilot CLI

```bash
copilot plugin marketplace add CleanCod3Systems/vault-memory
copilot plugin install vault-memory@vault-memory
```

### Gemini CLI

```bash
gemini extensions install https://github.com/CleanCod3Systems/vault-memory
```

Gemini asks for the vault path on install (setting `VAULT_MEMORY_VAULT`).

### Any agent with Agent Skills or MCP

- Skills: `npx skills add CleanCod3Systems/vault-memory`, or copy `skills/` into your agent's
  skills folder.
- MCP: `vault-memory mcp` (stdio). Per-client configuration: [docs/mcp.md](docs/mcp.md).

Full guide, profiles and updates: [docs/install.md](docs/install.md).

## Commands

```bash
vault-memory recall "query" [--brief] [--type incident] [--json]
vault-memory note --title … --type doc --folder Memory --body … --source … --verified today
vault-memory doctor            # health, read only
vault-memory fix [--apply]     # fill missing frontmatter (dry run without --apply)
vault-memory refresh           # index + patterns + related notes + panel
vault-memory usage             # is memory helping? pointers used, missed recall, gaps
vault-memory eval [--hook]     # measure recall against your own questions
vault-memory migrate [--apply] # vaults made by obsidian-cerebro (Spanish names)
```

## Modules — turn on only what you use

Nothing is mandatory except the index and recall.

| Module | What it does |
|---|---|
| `contract` | Validates and normalizes frontmatter (`fix`, `retype`) |
| `graph` | Backlinks, orphans, broken wikilinks, duplicate names |
| `patterns` | What repeats, what got stuck |
| `panel` | The self-contained HTML page |
| `bases` | Installs the standard `.base` views inside the vault |
| `templates` | Installs the standard note templates inside the vault |
| `tickets` | Extracts `ABC-123` keys and flags what has no ticket |
| `chat` | Extracts team chat permalinks |
| `ids` | Extracts order/case/record numbers for exact search |
| `schedule` | Runs `vault-memory refresh` at a fixed time with the system scheduler |

Profiles: `minimal` (memory only), `standard` (memory + graph, patterns, panel, Bases,
templates), `team` (everything). `vault-memory modules --profile minimal`.

## The standard

What makes recall work is the frontmatter, not the folders: [STANDARD.md](STANDARD.md).
Freshness fields: [docs/freshness.md](docs/freshness.md).

## What leaves your machine

Nothing. The index, telemetry (`.vault-memory/telemetry.jsonl`) and panel live inside the
vault's state folder. The panel embeds note titles and excerpts: open it locally, never
publish it.

## Development

```bash
make test       # stdlib unittest, Python 3.9+
make eval       # recall and hook gates on the example vault
make validate   # claude plugin validate .
make release    # build public/ (marketplace.json + zip) for the hosted catalog
```

License: MIT.
