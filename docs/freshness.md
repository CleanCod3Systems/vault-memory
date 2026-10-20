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

A `status` listed in the config's `statuses.obsolete` (`obsolete`, `replaced`, `deprecated`…)
counts as expired.

## State of each note

`ok` · `replaced` (another note names it in `replaces`; recall returns `replaced_by`) ·
`expired` (`expires` passed or an obsolete status) · `unverified` (of the `freshness.types`,
not verified for more than `freshness.review_days`; the reference date is `verified`, else
`date`, else the modification date).

What depends on the date is computed at query time, not at index time. `--today YYYY-MM-DD`
(or `VAULT_MEMORY_TODAY`) pins the date to compare runs.

## Effect on recall

The score is multiplied by `freshness.penalty[state]`: by default `replaced` 0.4, `expired` 0.5
and `unverified` 1.0 (only reported: almost no old note has `verified`, and penalizing all of
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

## How it gets written

With `vault-memory note` (or the MCP tool `write_note`), which looks for duplicates before
creating and forces a decision: update the existing one, replace it, or force. See the
`recall` skill, section "Save memory".
