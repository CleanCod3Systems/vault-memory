# vault-memory

Long-term memory for coding agents built on an Obsidian vault: the vault is the source of truth and vault-memory is the single way to read, write and measure it.

## Language

**Vault**:
The Obsidian folder of Markdown notes that holds what the user knows; the only source of truth.
_Avoid_: memory bank, knowledge base

**Pointer**:
A one-line reference to a note (path, plus at most a short passage) that a hook adds to the agent's context instead of the note itself.
_Avoid_: suggestion, injection

**Gate**:
A pointer added right before the agent calls an external search tool, when a current note already answers it. It informs and never blocks.
_Avoid_: guard, interceptor

**Hook mode**:
When automatic recall runs on a prompt: on every prompt, only on the first one of a session (or one that carries an ID or ticket), or never.

**Daily summary**:
A few lines, once a day, with what is next, what is waiting on someone and what is due soon, taken from the user's task notes.
_Avoid_: digest, standup

**Learned note**:
A note written by the model from the user's sessions, not by the user. It carries its origin and an expiry date until someone reads it, which confirms it.
_Avoid_: auto note, memory candidate

**Inbox**:
Where learned notes wait for review when automatic learning is off. It lives outside the vault.

**Installation config**:
The values one user's vault puts in `config.json` (tool names, domain words, thresholds). The product ships the mechanism with neutral defaults; everything specific to a person or company lives here.
_Avoid_: user settings, profile
