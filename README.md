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
| **Automatic recall** (Claude Code) | A hook leaves up to 3 pointers to relevant notes on each prompt — and stays silent on chat ("ok, go ahead", "thanks"). `hook.mode` in `config.json`: `all` (default, every prompt), `first` (the first prompt of the session, or any prompt with an ID or ticket) or `off` |
| **Gate before external searches** (Claude Code) | Before a tool listed in `gate.tools` (a database, logs, chat, web), one line points to a current note that already answers it. Never blocks. Off until configured: `"gate": {"tools": {"mcp__db__query": "reason", "mcp__logs__": "query"}}` maps a tool name or prefix to the `tool_input` field used as the query (`"*"` joins every string). The plugin runs it on MCP tools, `WebFetch` and `WebSearch`; to cover a `Bash` command, add a `PreToolUse` hook with matcher `Bash` running `hooks/gate.py` in your own settings |
| **Daily summary** (Claude Code) | At the first session of each day, a few lines with what is next, what is waiting on someone and what is due soon, from your task notes. `vault-memory today [--all]` any time. Off until `today.type` is set: `"today": {"type": "task", "status_field": "status", "groups": {"next": ["next"], "waiting": ["waiting"]}, "priority_field": "priority", "due_field": "due", "waiting_field": "waiting_on"}` |
| **Learning from sessions** (opt-in, uses a model) | `vault-memory distill` (and `refresh`, when `distill.enabled`) reads yesterday's Claude Code sessions, redacts emails, phones, cards and keys, and asks a small model for at most 3 cross-project facts per session. Each fact must quote the session verbatim and pass a second "is this supported?" check; anything still sensitive is dropped. `distill.auto: false` (default) leaves them in `.vault-memory/inbox/` for review; `true` writes them with `vault-memory note` (update or replace instead of duplicating), with `expires` 90 days out. Before saving, the model checks the closest notes: the same fact is dropped, new detail is appended to a learned note and a contradiction replaces it (notes you wrote are never edited, only linked). A learned note that gets read stays alive (its `expires` moves forward) but is not verified: only a review against the source does that; one nobody reads expires and moves to `_archive/`. Each fact also says whether it is `live` (a state that changes in weeks) or `stable`. Each day is learned once; missed days are caught up (up to 7). `exclude_paths` keeps whole folders out. Provider: `claude-cli` (your Claude Code login, `claude -p`) or `anthropic-api` (`ANTHROPIC_API_KEY`) |
| **Freshness** | Replaced, expired and unverified notes rank lower; two current notes about the same fact are reported as a contradiction |
| **Safe writing** | `vault-memory note` validates the contract and refuses duplicates: it makes you update or replace instead |
| **Contract** | Predictable frontmatter is what makes everything else possible. `doctor` says how much is missing; `fix` fills it **without overwriting anything and without moving files** |
| **Graph** | Backlinks, orphan notes, broken wikilinks and duplicate names |
| **Patterns** | What repeats, what repeats **without a written solution**, what has been open too long |
| **Panel** | A self-contained HTML page that searches the whole vault in the browser, no backend |
| **Review against the source** | `vault-memory review` lists the notes worth checking now: contradicted ones, learned ones never audited, the ones you use, the ones past their interval (`volatility: live` notes age in weeks). The `verify` skill checks each one at its primary source (code, ticket, thread, a read-only query), tries to refute it, and records the verdict: `confirmed` with a 1-10 confidence (8+ only with primary evidence), `outdated` (recall ranks it lower and the hook stops suggesting it) or `unsure`. Only frontmatter is written, with a backup. Recall and the hook show `verified 2026-11, 9/10` so answers say how sure they are |
| **Measured on your own use** | `vault-memory bench` builds test cases from your history (a question, then the note that was read right after it, by the Read tool, a shell command or the MCP) plus the cases you write in `eval.tsv`, and reports hit@1, hit@3 and MRR with a 95% interval. `--tune` tries BM25 `k1`/`b`, the title bonus and a small lift for notes that were useful before; it picks on half the cases and checks on the other half, and `--apply` writes a change only when that held-out gain is clearly above zero with 30+ cases. `refresh` keeps `bench.json` current and the Monday summary shows last week's numbers |
| **Next to Obsidian** | Rewriting a note checks it is still what was read, so a save from Obsidian in between is never overwritten (exit 4, nothing written). `doctor` reports what breaks Obsidian Properties: deprecated `tag`/`alias`/`cssclass`, unquoted wikilinks, nested values, repeated keys. `vault-memory bases [NAME] --install` adds the standard views, now with `Memory.base` (never verified, checked at the source, live facts, outdated). `vault-memory links` lists similar notes with no link between them, leaving out series that share a template; read only |
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
vault-memory bench [--tune [--apply]]   # recall measured on your own history, tuned only with clear evidence
vault-memory bases [Memory] [--install]   # standard Obsidian views, Memory.base included
vault-memory links [--n 20]     # similar notes that do not link to each other
vault-memory today [--all]     # what is next, waiting and due soon (config `today`)
vault-memory distill [--day D] [--dry-run]   # learn from yesterday's sessions (config `distill`)
vault-memory review [--n 20]   # notes to check against their source, most urgent first
vault-memory review PATH --verdict confirmed --confidence 9 --evidence "…"   # record a check
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

Nothing, unless you turn on `distill`. The index, telemetry (`.vault-memory/telemetry.jsonl`)
and panel live inside the vault's state folder. The panel embeds note titles and excerpts: open
it locally, never publish it.

With `distill.enabled`, the redacted text of your sessions (your prompts and the assistant's
final answers, nothing from tool calls) goes to the model provider you chose, once a day.
With `claude-cli` that is the same provider your sessions already use.

## Development

```bash
make test       # stdlib unittest, Python 3.9+
make eval       # recall and hook gates on the example vault
make validate   # claude plugin validate .
make release    # build public/ (marketplace.json + zip) for the hosted catalog
```

License: MIT.
