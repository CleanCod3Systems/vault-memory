---
name: verify
description: 'Check vault notes against their real source (code, tickets, chat, data) and record a verdict with its evidence and a 1-10 confidence; a skeptic pass tries to refute every confirmation. Use for "verify the vault", "is this note still true?", "audit what was learned", "review memory", or a scheduled review run.'
---

# verify

Reply in the user's language. The score measures **evidence found at the primary source**,
never how plausible the note sounds. A note is not evidence for itself, and neither is
another note, a summary or what you remember.

## 1. Take the queue
```bash
vault-memory review --json --n 10     # most urgent first, with the reasons
```
Order: contradicted notes, notes learned from sessions and never audited, notes people use,
notes past their review interval. Work top-down; stop at the requested number.

## 2. Check each note
1. Read the note. List its checkable claims (at most 5): names, numbers, states, dates,
   who decided what, how something works.
2. Go to the primary source of each claim, read-only:
   code or config → the repository at its main branch (grep, git log since the note's date) ·
   status or decision → the ticket or the thread where it was decided · data or state → a
   read-only query, if allowed · a library → its current docs.
3. **Skeptic pass.** Try to refute it: look for anything *newer* than the note's date
   (later commits, ticket comments, messages) that changed it. When you can start a
   subagent, give a fresh one only the note and your evidence list and ask it to refute
   them; it may lower the score, never raise it.

## 3. Score and record
| Confidence | Evidence |
|---|---|
| 10 | every claim checked at its primary source today, all match |
| 9 | the central claim checked at the primary source, the rest consistent with it |
| 8 | checked at the primary source, one minor detail could not be seen |
| ≤ 7 | only secondary sources, or partial → verdict `unsure`, never `confirmed` |

```bash
vault-memory review "PATH" --verdict confirmed --confidence 9 \
  --evidence "<source>: <what it shows>" --volatility live|stable
vault-memory review "PATH" --verdict outdated --evidence "<source>: <what it says now>"
vault-memory review "PATH" --verdict unsure --evidence "<what was missing>"
```
- `volatility`: `live` for a state that changes within weeks (what is pending, who waits,
  what is enabled, a version, a role); `stable` for how something works, a decision, a
  contract. Live notes come back for review in weeks.
- Review writes only frontmatter, with a backup; it never touches the body.
- **Outdated and the source shows what is true now:** write it, replacing the old note,
  then record the new one as confirmed:
  `vault-memory note --title "…" --type … --folder <distill.folder> --body "…" --source "review <date>: <source>" --replaces "Old note"`.

## Rules
- Never `confirmed` from memory, from the vault itself or from a model's opinion.
- Sources are read-only: never change a ticket, message or data to make it match.
- A source you cannot reach (no access, needs approval in an unattended run) → `unsure`
  with "needs <source>", so a person can finish it.
- No secrets, tokens or personal data in `--evidence`: name where, not the value.

## Report
One line per note: verdict, confidence, the evidence in a few words. Then the coverage line
of `vault-memory review` (verified and current · outdated · unsure · due) and anything that
needs a person.
