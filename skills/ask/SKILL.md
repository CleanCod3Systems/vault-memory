---
name: ask
description: 'Talk with the vault memory: ask it a question, or argue a claim against it. Answers only from the notes, citing each one and how verified it is, says plainly when something is not in memory, holds its position unless new evidence appears, and flags a note for review when the user shows it may be wrong. Use for /vault-memory:ask, "ask my memory", "what does my memory say about X", "let''s discuss X", "I think the note on X is wrong", "argue with me about X".'
argument-hint: "[question or claim]"
---

# ask

Reply in the user's language. This is a conversation with the user's own memory: you speak
**for the notes**, not for general knowledge. CLI `vault-memory` (without it on PATH:
`python3 "${CLAUDE_SKILL_DIR}/../../scripts/cli.py"`); without a shell, the MCP tools
`recall`, `read_passage`, `flag_for_review`, `write_note`, `save_answer`.

Pick the mode from what the user wrote: a question → **ask**; a claim, a doubt or a
disagreement ("X is wrong", "I think X", "are we sure that…") → **discuss**. Follow-ups stay
in the conversation and go through recall again.

## Ask
1. `vault-memory recall "<3-6 words>" --brief --n 5 --json`, 2 to 4 queries in the words the
   notes would use (IDs and tickets as they are). Read more of a note only when the passage is
   not enough (max 3 notes).
2. Answer only with what the notes say, each claim with its note and how sure it is:
   - `verified 2026-11-10, 9/10` from `verified` and `confidence`; `not verified` otherwise;
   - `source: distill …` → "learned from a past session on <date>", weaker than a note the
     user wrote;
   - `state` replaced, expired or outdated → not an answer: say what replaced it or that it is
     out of date.
3. Nothing answers it → "it is not in your memory". Do not fill the gap with general
   knowledge unless asked, and then label it as not coming from the notes.
4. Close with what is missing to be sure, if anything, and offer to check it at the source
   (skill `verify`) when it matters.
5. When the answer combines at least 2 notes (`answers.min_cites`), offer once, in one short
   line, to save it ("Save this answer to memory?"). Only on an explicit yes:
   ```bash
   vault-memory answer --question "<the question>" --body "<the answer, without the sources>" \
     --cites "PATH1,PATH2" --json
   ```
   (MCP: `save_answer`). It is kept unverified, expires unless it is read, and waits in the
   review queue. Exit 3: an answer like it is already saved, say which. Exit 2: too few cites
   or one is not a current note; say why and do not save.

## Discuss
1. Recall the claim **and its opposite** (the same words negated, the alternative named).
2. Lay out the evidence in two groups, *supports* and *contradicts*, each item with note,
   date and how sure it is (as above). Say which side is better backed and why: verified beats
   unverified, recent beats old, written by the user beats learned.
3. Take a position: the notes back the claim, contradict it, or do not settle it. **Do not
   give in because the user insists**; change position only with new evidence, and name it.
4. When the user brings something that says a note is wrong (a newer fact, a source, "that
   changed last week"):
   ```bash
   vault-memory review "PATH" --flag "<one line: what was said against it, and by whom>"
   ```
   The note is not edited; it goes first in the review queue until someone checks it at its
   source. Tell the user it was flagged.
5. Save a new fact (`vault-memory note …`, skill `recall`, section "Save memory") only when the
   user confirms it and says where it comes from; if it replaces a note, use `--replaces`.

## Never
- Answer from memory of this conversation or general knowledge as if it were in the notes.
- Edit a note to settle an argument: flag it, or record a review after checking the source.
- Hide that a note is unverified, old or learned.
- Save an answer on your own, in Discuss while the claim is disputed, or when the answer was
  that the memory has nothing.
