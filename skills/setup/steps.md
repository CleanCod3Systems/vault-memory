# setup — full reference

Read only when SKILL.md is not enough.

Make an Obsidian vault queryable, enabling **only what the user wants**. The engine does not
move files and only adds missing frontmatter keys: 100 % coverage is reached without
reorganizing anything.

CLI: `vault-memory`, or `python3 <plugin>/scripts/cli.py`.

## 1. Ask before writing

**Do not assume the profile.** Two questions, asked together:

**Question 1 — What do you want it to do?**

| Option | What it enables |
|---|---|
| Memory only | Index, recall and the standard contract. Writes nothing inside the vault except missing frontmatter. Profile `minimal` |
| Memory + panel and views | The above + link diagnostics, pattern synthesis, HTML panel, Bases and templates. No integrations. Profile `standard` |
| Everything, with integrations | The above + ticket key and team chat permalink extraction. Profile `team` |

**Question 2 — Where is the vault?** Skip if the user already gave the path. A vault is a folder
with `.obsidian/` inside; there can be several, do not guess.

If they chose `team`, also ask for the chat domain (e.g. `mycompany.slack.com`) — ticket
prefixes are detected by the engine reading the notes.

## 2. Initialize

```bash
vault-memory init --vault "/path" --profile minimal|standard|team
```

- `--scaffold` **only if the vault is empty**: creates the reference folder structure. On a
  vault with notes, never.
- `init` is idempotent: running it again keeps what the user edited by hand in the config.
- Without `--profile`, on an initialized vault it does not touch the modules.
- The search language (`language.stemmer`: `en`, `es` or `none`) is detected from the notes.

## 3. Review the mapping BEFORE touching notes

Show `<vault>/.vault-memory/config.json` and confirm:

- **`areas`** — folder → area. Inferred from the name; it almost always needs an adjustment.
- **`types_by_folder`** — which note type lives in each folder. A mistake here spreads to every
  note in that folder when `fix` runs.
- **`modules`** — what was chosen in step 1.

## 4. See what is missing, without touching anything

```bash
vault-memory doctor --vault "..."
```

Read it in plain language. **Apply nothing yet.**

## 5. Normalize

```bash
vault-memory fix --vault "..."            # dry run
vault-memory fix --vault "..." --apply    # applies, with a backup
```

Always show the dry run. Apply **only with explicit confirmation**: it writes inside the
notes. Backup in `<state>/backups/<timestamp>/`.

## 6. Close

```bash
vault-memory refresh --vault "..."
```

If the panel is on, open `<vault>/.vault-memory/panel.html` and show it.

## 6b. Offer the automatic refresh

```bash
vault-memory modules --on schedule
vault-memory schedule --at 08:30 --days weekdays            # dry run: shows what it would write
vault-memory schedule --at 08:30 --days weekdays --install
```

It costs 0 tokens per run (no network, no model). **Show the dry run before installing**,
because it writes on the user's system: launchd on macOS, crontab on Linux.

## Changing modules later

```bash
vault-memory modules                          # what is active
vault-memory modules --on panel patterns
vault-memory modules --off tickets chat
vault-memory modules --profile minimal
vault-memory modules --off panel --clean      # and deletes the files it left
```

They turn on and off live, on an already populated vault. After changing them run
`vault-memory refresh`.

The ten modules: `contract`, `graph`, `patterns`, `panel`, `bases`, `templates`, `tickets`,
`chat`, `ids`, `schedule`. Recall is not a module: it is the tool.

When the user asks to turn off something that left files inside the vault (`bases`,
`templates`) or in the state folder (`panel`, `patterns`, `contract`), offer `--clean` and
**say what will be deleted before doing it**.

## 7. Global command

```bash
python3 <plugin>/scripts/cli.py install-cli     # writes ~/.local/bin/vault-memory
```

Check that `~/.local/bin` is on `PATH`. From then on, inside the vault:
`vault-memory recall "..."` without `--vault`.

## 8. Other agents

Agents without hooks (Codex, Gemini CLI, Copilot CLI, Cursor, desktop chat) use the same engine
through `vault-memory mcp`. Configuration per client: `docs/mcp.md` in the plugin.

## Limits

- Never run `fix --apply`, `retype --apply` or `migrate --apply` without explicit confirmation.
- Never move, rename or delete notes.
- `--clean` deletes files: list them first and ask for confirmation.
- Over 5000 notes, warn that the first index takes a few seconds.
- If the user keeps secrets in notes, say so before building the panel: the panel embeds text
  excerpts of every note.
