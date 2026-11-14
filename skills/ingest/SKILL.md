---
name: ingest
description: 'Feed vault memory from places other than agent sessions: collect a chat thread, an issue or a pull request with your own tools (MCP or CLI) and hand it to vault-memory ingest, which redacts it, proposes candidate notes and keeps only what the material supports. Use for "learn from this thread", "remember what we decided in this issue", "feed yesterday''s PRs to memory", or a scheduled collection run.'
---

# ingest

Reply in the user's language. vault-memory never connects to chat, trackers or code hosts:
**you** collect with the tools you already have, and the CLI does the rest (redact → propose
→ check the quote is in the material → inbox, or the vault when `distill.auto` is on).

## 1. Pick what is worth it
Only material where something was **decided, explained or fixed**: a thread that ended in a
decision, an issue with its root cause, a PR discussion that settled how something works.
Skip status pings, chatter and anything the repo or ticket already records well. One item per
call; at most 10 per run.

## 2. Collect, read-only
With your own tools, read the full item (all replies or comments) and write it as JSON:
```json
{"source": "chat", "id": "<id in that system>", "url": "<link to the original>",
 "date": "YYYY-MM-DD", "title": "<short subject>",
 "messages": [{"author_role": "user", "text": "…", "ts": "2026-11-09T10:00:00Z"}]}
```
- `source`: one lowercase word per kind of system (`chat`, `issues`, `prs`, `docs`), always the same.
- `author_role`: a role, never a name: `user` (the person you work for), `teammate`,
  `reviewer`, `author`, `bot`. Leave out bot noise and tool output.
- Copy the text as written; do not summarize (the quote check needs the real words).
- Never put credentials, tokens or personal data in it. Redaction is a safety net, not a filter.

## 3. Hand it over
```bash
vault-memory ingest --file item.json --dry-run --json    # first time: see what it would keep
vault-memory ingest --file item.json --json               # or: … | vault-memory ingest --file -
vault-memory ingest --source docs --file notes.md --url "<link>" --date 2026-11-09   # plain Markdown
```
Exit 2: the material is unreadable (no `source`, no `messages`, bad date); fix it, never
invent a field. Ingesting the same item again is safe: what is already there is not repeated.

When the material adds to or contradicts a note that already exists, the CLI leaves an
**update proposal** in the inbox instead of a new note: the target note, the text to append or
the exact fragment to replace, and the quote that backs it (at most `ingest.max_updates` per
item, 10 by default). It is never applied on its own, whatever `distill.auto` says, and a
capture is never a target. Never edit the target note yourself to "apply" it: the person
decides with `vault-memory inbox` (it shows the diff), `--accept ID --evidence "…"` or
`--reject ID --reason "…"`.

GitHub PRs need no agent: with `gh` logged in, `distill.sources: ["claude-code", "gh-prs"]`
and `distill.gh_repos: ["owner/repo"]` make the daily distill read them.

## Report
One line per item: source and id, then proposed · kept · update proposals · discarded (with
reasons). Kept candidates wait in the inbox for review unless `distill.auto` is on; update
proposals always wait there; say so.
