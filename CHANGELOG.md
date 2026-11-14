# Changelog

## 0.9.0 — 2026-11-14

- Captures: raw material kept exactly as it arrived, so a note can always be checked against
  it. `vault-memory capture FILE|- [--title] [--url] [--date]` writes it, redacted, to
  `captures.folder` (`Captures`) with its `sha256`, and never overwrites (the same content
  returns the existing path). `vault-memory ingest --capture PATH` learns from it and every
  candidate carries `capture: "[[…]]"`. vault-memory never edits, moves or archives a capture,
  and captures stay out of recall, the hook and the gate. `doctor` reports captures edited
  after capture and notes whose `capture` link points nowhere.
- Saved answers: `vault-memory answer --question … --body … --cites a,b` (MCP `save_answer`)
  keeps an answer that combines at least `answers.min_cites` (2) current notes as a note in
  `answers.folder` (`Memory/answers`): redacted, with `derived_from`, `source: ask <date>` and
  `expires` (`answers.expires_days`, 90), never verified. It enters the review queue like a
  learned note, is renewed when read and archived when it expires unread, and its reviews do not
  count toward learned-note precision. The `ask` skill offers to save an answer once and saves
  it only on an explicit yes. Duplicates are judged on the question and the answer, not the
  cited names.
- `doctor` lists notes longer than `doctor.long_words` (600 words of body, without frontmatter
  or code; 0 = off), which dilute recall; daily, period, home and index notes are skipped
  (`doctor.long_skip_types`). `doctor --json` → `long_notes`, `captures`.

## 0.8.0 — 2026-11-11

- Talk with your memory: skill `ask` (`/vault-memory:ask <question or claim>`, or "ask my
  memory…", "let's discuss…"). Ask mode answers only from the notes, each claim with its note
  and how verified it is; discuss mode lays out the evidence for and against, holds a position
  unless new evidence appears and flags what turns out disputed.
- `vault-memory review PATH --flag "reason"`: a note disputed in a conversation goes first in
  the review queue (above a detected contradiction) until a review is recorded after it. The
  note is never edited; the flag lives in `<state>/flags.jsonl`, redacted.
- MCP: prompts `ask` and `discuss`, tool `flag_for_review`, and `recall` results carry
  `verified`, `confidence`, `checked` and `source` so desktop chat answers say how sure they are.
- CI is green again: the gate tests build the example index themselves (a fresh checkout has
  none), and Python 3.9 runs on Ubuntu 24.04 (setup-python has no 3.9 for Ubuntu 26.04).

## 0.7.0 — 2026-11-11

- Recall matches a note's `aliases`, its `key` and an optional `keys:` list of questions it
  answers, as if they were its title (`recall.expand_keys`, on).
- Recall resolves time expressions in Spanish and English ("ayer", "last week", "en octubre",
  `2026-10-05`) to a date range and boosts notes dated in it, without filtering
  (`recall.time_boost`, 0.5; 0 = off). Index format v5: old indexes rebuild on the next recall.
- Every `review` check is logged in `<state>/reviews.jsonl` (no note text).
  `vault-memory review --calibration [--json]` shows how often a check at confidence 10, 9, 8
  and ≤7 held up at the next review, with a Wilson 95% interval, plus learned-note precision.
- `distill.auto: "earned"`: learned notes are written directly only while the Wilson lower
  bound of learned-note precision is ≥ `distill.earned_min_precision` (0.8) over ≥
  `distill.earned_min_audits` (20) audits; otherwise the inbox. `distill` says which and why.
- `vault-memory review --budget [MAX]` and `review.daily` (`base` 4, `max` 8): a scheduled check
  covers more while contradicted or never-audited learned notes wait.
- Distill providers `openai-compat` (OpenAI, LM Studio, vLLM, LiteLLM; JSON schema with a
  `json_object` fallback) and `ollama` (native structured outputs). `doctor` reports whether the
  provider is ready, without network calls.
- `distill.sources`: also learn from Codex CLI and Gemini CLI sessions, and from your pull
  requests through `gh` (`gh-prs`, `distill.gh_repos`). Default unchanged (`claude-code`).
  `distill --sources a,b` runs only some sources once.
- `vault-memory ingest --file item.json|item.md|-`: learn from a thread, issue or PR an agent
  collected, with the same redaction, quote and support checks; candidates carry `origin` and
  `url`. New `ingest` skill.
- `schedule` on Windows (Task Scheduler) and, on Linux, a systemd user timer with
  `Persistent=true` (crontab as fallback). `install-cli` writes a `.cmd` shim on Windows.
- CI runs the tests on Linux, macOS and Windows (Python 3.9 and 3.13).

## 0.6.0 — 2026-11-11

- Safe writes next to Obsidian: `note --update`, `review` and distill's renewal re-read the
  note right before writing and stop with exit 4 (nothing written) if it changed since it was
  read, so a save from Obsidian in between is never lost.
- `doctor` checks the Obsidian Properties rules: deprecated `tag`, `alias`, `cssclass`,
  unquoted wikilinks in values, nested values and repeated keys (`doctor --json` →
  `properties`). Report only.
- `standard/bases/Memory.base`: a view of the review fields (never verified, checked at the
  source, live facts, outdated or unsure). `vault-memory bases [NAME…] [--install] [--folder F]`
  adds the standard views a vault lacks (or only the named ones) and never overwrites one.
- `vault-memory links`: similar notes with no wikilink between them (same similarity as near
  duplicates, from 0.35 up to the duplicate threshold), leaving out dated siblings and
  "Prefix - X" series of one folder. Read only.

## 0.5.0 — 2026-11-11

- `vault-memory bench` (`scripts/bench.py`): recall measured on the vault's own history. Cases
  are mined from telemetry (a question, then a note read within 15 minutes in the same
  session, leaving out notes written in that session and prompts of automatic runs) plus
  `eval.tsv`; hit@1, hit@3 and MRR@10 with a seeded bootstrap interval. Writes `bench.json`,
  `bench.tsv` and `usage.json`; `refresh` runs it.
- `bench --tune` tries `recall.k1`, `recall.b`, `recall.title_bonus` and `recall.prior_weight`
  (a bounded lift from past reads, off by default, computed without leaks: only earlier reads
  of other sessions). It picks on one half of the cases and measures on the other; `--apply`
  writes only when that held-out gain has a 95% interval above zero with 30+ cases.
- Reads are counted when a note is opened with a shell command (`cat`, `sed`, `head`…) or the
  MCP `read_passage`, not only the Read tool (PostToolUse matcher `Read|Bash`). Recall events
  record the top 3 paths.
- The gate reads list fields too (search tools that take `keywords`).
- On Mondays the daily summary adds one line: suggestions used, recall MRR and notes verified.

## 0.4.0 — 2026-11-11

- Review against the source (`scripts/review.py`, `vault-memory review`, skill `verify`). The
  queue ranks what to check now: notes contradicted (same `key`, or named by a learned note as
  `Contradicts [[X]]`), learned notes never audited, notes people read or the hook suggests,
  and notes past their review interval; a note checked in the last `review.gap_days` (7) waits.
  `review PATH --verdict confirmed|outdated|unsure --evidence … [--confidence N]
  [--volatility live|stable]` writes `checked`, `evidence` and, when confirmed, `verified` and
  `confidence` (8-10 only). Frontmatter only, with a backup.
- New state `outdated` (`review: outdated`): ×0.5 in recall, reported by `doctor`, never a hook
  pointer. `volatility: live` notes are due after `freshness.review_days_by_volatility.live`
  (21) days, whatever their type.
- Recall JSON adds `confidence`, `checked` and `volatility`; the hook pointer shows
  `verified 2026-11 9/10`. The `recall` skill says how sure each answer is.
- distill: reading a learned note no longer verifies it, it only moves `expires` forward
  (`renewed` replaces `confirmed` in the output and telemetry). Each fact carries `volatility`.

## 0.3.1 — 2026-11-11

- distill never learns the same day twice (`--force` to redo one) and catches up the days it
  missed since its last run (machine off, scheduler failed), up to `distill.catch_up_days` (7).
  The first run only reads yesterday.
- Before saving, the model compares each fact with the closest current notes: `same` is
  dropped, `adds` is appended to a learned note, `contradicts` replaces a learned note. A note
  the person wrote is never edited or replaced: the learned note links to it instead. Reworded
  duplicates used to slip past the text-similarity check.
- Learned notes that expire without being read move to `_archive/` (`distill.archive`): out of
  default searches, never deleted. The proposal prompt asks for absolute dates.
- The `recall` skill answers "what do I have today?" with `vault-memory today` and cites learned
  notes as such.

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
