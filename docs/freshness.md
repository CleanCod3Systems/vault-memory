# Memory freshness

A memory that returns what was true last year with the same confidence as what is true today
is worse than no memory. vault-memory tells them apart with **optional** frontmatter fields: a
note without them behaves as always.

| Field | Format | What for |
|---|---|---|
| `verified` | `YYYY-MM-DD` | last time someone confirmed it is still true |
| `source` | text | where it comes from: ticket, meeting, doc, person |
| `replaces` | `"[[Old note]]"` (or list) | this note makes that one obsolete |
| `expires` | `YYYY-MM-DD` | from that date it no longer holds (temporary agreements) |
| `key` | slug | the fact it talks about: one key, one current note |
| `volatility` | `live` / `stable` | how fast it changes: a `live` note is due for review in weeks |

A review against the source (below) adds `checked`, `evidence`, `confidence` and, when the
source says otherwise, `review: outdated` (or `unsure` when it could not be settled).

A `status` listed in the config's `statuses.obsolete` (`obsolete`, `replaced`, `deprecated`…)
counts as expired.

## State of each note

`ok` · `replaced` (another note names it in `replaces`; recall returns `replaced_by`) ·
`expired` (`expires` passed or an obsolete status) · `outdated` (a review found the source
says otherwise) · `unverified` (of the `freshness.types`, or with a `volatility`, not verified
for longer than its limit; the reference date is `verified`, else `date`, else the
modification date). The limit is `freshness.review_days_by_volatility` for its volatility
(`live`: 21), else `review_days_by_type`, else `review_days`.

What depends on the date is computed at query time, not at index time. `--today YYYY-MM-DD`
(or `VAULT_MEMORY_TODAY`) pins the date to compare runs.

## Effect on recall

The score is multiplied by `freshness.penalty[state]`: by default `replaced` 0.4, `expired` 0.5,
`outdated` 0.5 and `unverified` 1.0 (only reported: almost no old note has `verified`, and penalizing all of
them would move the ranking without new information). An exact ID or ticket search is not
penalized. `--include-expired` turns the penalty off.

## What `doctor` reports

- **Contradiction**: two current notes with the same `key` and neither replaces the other. A
  sure error.
- **Expired** and **replaced** notes (with the one replacing it).
- **Near duplicates**: similarity normalized against the note itself ≥
  `freshness.near_duplicate` (0.85).
- **To review** (heuristic, not certainty): same type and near-identical titles, two shared IDs
  between specific notes, or the same ticket as the only reference.
- How many notes have gone more than `review_days` without verification.

`doctor --json` returns everything structured; `patterns.json` carries it under `freshness`.

## Review against the source

Age says when a note was last confirmed, not whether it is still true. `vault-memory review`
returns the notes worth checking now, most urgent first, each with its reasons:

1. contradicted: two current notes with the same `key`, or a learned note that says
   `Contradicts [[X]]` (distill writes that instead of editing a note the person wrote);
2. learned from sessions, or saved answers (`answers.folder`), never audited (no `confidence`);
   reviews of saved answers do not count toward learned-note precision;
3. used: read, or suggested by the hook, in the last `review.usage_days` (30);
4. past their review interval; `live` notes get a little extra weight.

A person can also dispute a note in a conversation: `vault-memory review "PATH" --flag
"<what was said>"` (MCP `flag_for_review`) writes a line to `<state>/flags.jsonl`, never the
note, and puts it first in the queue (above a detected contradiction) until a review is
recorded after the flag, whatever its verdict.

A note checked in the last `review.gap_days` (7) waits, whatever the verdict, unless it was
flagged after that check. Checking needs
the real sources, so an agent does it with the `verify` skill and records the result:

```bash
vault-memory review "PATH" --verdict confirmed --confidence 9 --evidence "<source>: <what>" [--volatility live]
vault-memory review "PATH" --verdict outdated --evidence "<source>: <what it says now>"
vault-memory review "PATH" --verdict unsure --evidence "<what was missing>"
```

`confidence` measures primary evidence, not plausibility: 10 every claim checked at its source
today, 9 the central claim, 8 one minor detail unseen; below `review.min_confirm` (8) the
verdict has to be `unsure`. Confirming sets `verified` and drops `review` (and `expires` on a
learned note). Only frontmatter is written, with a backup in the state folder. Reading a note
never verifies it.

### Does confidence hold up?

Each recorded check also appends a line to `<state>/reviews.jsonl`: date, path, verdict,
confidence, volatility, whether the note was learned and the note's previous check (no note
text, no evidence). `vault-memory review --calibration` reads it: for checks made at confidence
10, 9, 8 and ≤7 (an `unsure` with its confidence), how many were confirmed at the next review
on a later day (held) and how many were found outdated (failed); an `unsure` next time settles
nothing and is counted apart. Each rate comes with a Wilson 95% interval; below
`review.calibration_min` (10) rechecks it says "too few" instead of a number. Model confidence
tends to run high (arXiv 2306.13063): this is how you find out by how much.

**Earned autonomy.** With `distill.auto: "earned"`, learned notes are written directly only
while the precision of learned notes (confirmed at their first settled audit) has a Wilson
lower bound ≥ `distill.earned_min_precision` (0.8) over at least `distill.earned_min_audits`
(20) audits; otherwise they go to the inbox. `distill` and `ingest` say which path they took
and why. Deciding on an inbox candidate is an audit too: `vault-memory inbox --accept ID --evidence …`
writes the note and logs a `confirmed` review of it; `--reject ID --reason …` writes no note and
logs an `outdated` line for `inbox:<id>` with the redacted reason (the only line that carries one).

**Daily budget.** `review --budget [MAX]` cuts the queue to `review.daily.base` (4) plus one per
contradicted or never-audited learned note, up to `review.daily.max` (8), never more than are
due; `budget.n` is in the JSON for a scheduled run.

To see all of this inside Obsidian, `vault-memory bases Memory --install` adds `Memory.base`: views of
the notes never verified, the ones checked at the source with their confidence, the live facts
and the ones a review found outdated or unsure.

## How it gets written

With `vault-memory note` (or the MCP tool `write_note`), which looks for duplicates before
creating and forces a decision: update the existing one, replace it, or force. See the
`recall` skill, section "Save memory".
