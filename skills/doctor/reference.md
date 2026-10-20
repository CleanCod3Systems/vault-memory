# doctor — full reference

Read only when SKILL.md is not enough.

CLI: `vault-memory`, or `python3 <plugin>/scripts/cli.py`. From inside the vault `--vault` is
not needed.

## Diagnosis — writes nothing

```bash
vault-memory doctor
```

Returns: indexed notes, links, contract coverage, orphans, broken wikilinks, duplicate names,
open records, linking gaps, what is missing per note type, what has been open for more than
30 days and which classes repeat.

Translate the report into plain language and **sort by what costs money or time**, not by the
order it came out in. A broken wikilink is cosmetic; a class that repeats five times without a
written solution means someone starts from scratch every time.

## Repair

```bash
vault-memory fix              # dry run: which keys it would add and where
vault-memory fix --apply      # applies, with a backup in <state>/backups/<timestamp>/
```

It only adds missing keys. **It never overwrites a value, never touches a note's body.**
Always show the dry run and ask for explicit confirmation before `--apply`.

```bash
vault-memory retype           # notes whose type no longer matches where they live
vault-memory retype --apply
```

Useful after reorganizing folders.

```bash
vault-memory migrate          # dry run: legacy Spanish keys, config and .cerebro/ folder
vault-memory migrate --apply  # rewrites them, every note backed up first
```

## Synthesis and panel

```bash
vault-memory patterns --note "00 Index/Patterns.md"   # derived note inside the vault
vault-memory panel --open                              # self-contained HTML page
vault-memory refresh                                   # the whole pipeline at once
```

The panel is a local file: it searches the whole vault with no network and no backend, and its
links open each note in Obsidian. It embeds note titles and excerpts: never publish it.

## When coverage drops

Every new note written outside the standard lowers the percentage. It is the canary: if it
dropped, someone created notes without frontmatter. `vault-memory fix --apply` brings it back
to 100 %.

If `type` values outside the vocabulary show up, offer both ways out: add them to
`config.json → extra_types` (if the type is legitimate for that vault) or fix the notes (if it
was a typo).

## Automatic refresh

```bash
vault-memory modules --on schedule
vault-memory schedule --at 08:30 --days weekdays             # dry run
vault-memory schedule --at 08:30 --days weekdays --install   # installs (launchd or cron)
vault-memory schedule --status                               # what is there + last runs
vault-memory schedule --off
```

**Always show the dry run before installing**: it prints the exact file and line it will write
on the user's system. Install only with confirmation.

It schedules `vault-memory refresh`, which uses no network and no model: zero tokens per run.
If the user asks to "bring information from Slack/Jira at that time", explain that is a
different thing — `schedule` refreshes what is already written; capturing from external
sources needs a separate agent task.

## Modules that are off

`doctor` lists at the end which modules are off. If the user asks about something missing from
the report (orphans, patterns, panel), first check whether the module is off instead of
assuming the vault is clean:

```bash
vault-memory modules                        # what is active
vault-memory modules --on graph patterns    # turn on what is missing
```

After changing modules run `vault-memory refresh`. A command whose module is off says so and
does nothing — it is not an error.

## Limits

- `fix --apply`, `retype --apply` and `migrate --apply` are the only operations that write into
  notes. Explicit confirmation, always.
- Do not propose moving or renaming files: the engine does not need it and the user did not ask.
- If `doctor` reports duplicate note names, flag it as a priority: wikilinks to that name
  resolve to either one, and that silently corrupts the graph.
