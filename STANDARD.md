# The vault-memory standard — a predictable structure for an Obsidian vault

A vault becomes queryable when its notes are **predictable**. This standard defines the
minimum needed for that. It is deliberately short: every extra rule is a rule someone will
break.

The central rule: **what makes recall work is the frontmatter, not the folders.** That is why
the contract is mandatory and the folder layout is only a recommendation.

---

## 1. Frontmatter contract (mandatory)

Every note carries, at least:

```yaml
---
type: doc          # WHAT the note is   (closed vocabulary, see §2)
area: backend      # WHAT it is about   (open vocabulary, each vault's own)
tags:
  - doc
  - backend
---
```

Some types add fields:

| Type | Added fields |
|---|---|
| `incident` | `date`, `status`, `severity`, `system` |
| `meeting`, `decision` | `date` |
| `daily` | `date` |
| `period` | `period` (e.g. `2026-W35`, `2026-08`) |
| `runbook` | `status`, `system` |
| `project` | `project` |

Optional fields the engine understands when present: `ticket`, `related`, `status`, `source`,
and the freshness fields (`verified`, `replaces`, `expires`, `key` — see `docs/freshness.md`).

**An empty field is a visible gap; a missing field is an invisible one.** That is why
`vault-memory fix` writes the key empty instead of leaving it out: it shows up in the Bases and
in the health report.

---

## 2. `type` vocabulary (closed)

Twelve values in three families. The engine derives the family; it is not written.

**`record` family** — something happened, it has a date:

| Type | What it is |
|---|---|
| `incident` | Something broke. Symptom, diagnosis, solution, prevention |
| `meeting` | Notes of a meeting |
| `decision` | A decision taken, with its context and alternatives |
| `daily` | The day's log |
| `period` | Closing of a week or a month |

**`knowledge` family** — it is like this, until further notice:

| Type | What it is |
|---|---|
| `doc` | How something works. The bulk of the vault |
| `runbook` | Step-by-step procedure for a known situation |
| `reference` | Schema, glossary, data table, taxonomy |
| `project` | A project or repository card |

**`structure` family** — holds the rest together:

| Type | What it is |
|---|---|
| `home` | Front page or index of a folder |
| `template` | Template for new notes |
| `note` | Catch-all. If you have many, a type is missing |

Your own types go in `config.json → extra_types`. Do not invent loose values in the notes:
what is not in the vocabulary cannot be grouped.

### `class` — only on the notes that need it

Types that repeat (`incident` by default) also carry `class`: the identifier of *what kind of
thing* it was, which is what makes recurrence countable. It is crossed with the taxonomy if one
is declared. Which types count is set in `config.json → patterns.types`.

## 3. `area` (open vocabulary)

What the note is about. Each vault defines its own — `backend`, `finance`, `customers`,
`personal`. It is mapped from folders in `config.json → areas`, so in practice it fills itself.
A small vault can live entirely with `area: general`.

---

## 4. File names

- **Dated notes** (`record` family): `YYYY-MM-DD - Title.md`. The date in the name is
  redundant with the frontmatter **on purpose**: it sorts the folder and allows recovering
  `date` if the frontmatter is lost.
- **Everything else**: a natural-language title. No cryptic prefixes, no `snake_case`.
- **Wikilinks resolve by file name**, so two notes with the same name in different folders are
  a broken link waiting to happen. The engine reports them.

---

## 5. Folders

**Recommended, not mandatory.** `vault-memory init --scaffold` creates them in a new vault; in
an existing vault the engine maps the folders you already have and moves nothing.

```
00 Index/             front pages
10 Log/               Daily/  ·  Periods/
20 Records/           Incidents/  ·  Meetings/  ·  Decisions/
30 Knowledge/         docs, grouped as you like
40 Runbooks/
50 Projects/
90 Templates/
_attachments/         images and files
_archive/             what no longer applies but is not thrown away
.vault-memory/        derived state of the engine
```

**Reserved** (treated differently, do not use them for notes):

| Folder | Treatment |
|---|---|
| `.vault-memory/` | Derived state. Not indexed. Can be deleted entirely and regenerated |
| `_archive/` | Indexed, but excluded from coverage and from default searches |
| `_attachments/` | Not indexed |
| `.obsidian/` | Not indexed |

---

## 6. What never goes into the vault

Credentials, tokens, `.env`, private keys, database dumps with personal data. The engine
excludes `.credentials/`, `.env*`, `*.pem` and `*.key` from the index, but that is a safety
net, not a permission.

---

## 7. Adopting it in an existing vault

Nobody reorganizes 200 notes by hand. The order is:

1. `vault-memory init` — finds the vault, proposes the folder→area mapping, writes the config.
2. `vault-memory doctor` — says how much is missing, touching nothing.
3. `vault-memory fix` — adds **only the missing keys**, never overwrites a value, keeps a backup.
4. Only then, if you want, move folders. The engine does not need it.

100 % coverage is reachable without moving a single file.

Vaults created by obsidian-cerebro (Spanish names: `tipo_nota`, `fecha`, `.cerebro/`) work as
they are; `vault-memory migrate` rewrites them to these names when you choose to.
