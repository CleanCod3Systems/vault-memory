#!/usr/bin/env python3
"""vault-memory — turns an Obsidian vault into memory any agent can query.

  vault-memory init [--vault PATH] [--scaffold]   initialize and propose the config
  vault-memory index                              rescan (index + graph + contract)
  vault-memory recall "query"                     search the vault
  vault-memory doctor                             vault health, read only
  vault-memory fix [--apply]                      normalize frontmatter (additions only)
  vault-memory retype [--apply]                   fix the type of moved notes
  vault-memory patterns [--note PATH]             recurrence synthesis
  vault-memory panel [--out FILE]                 self-contained HTML page
  vault-memory refresh                            index + patterns + related + panel
  vault-memory schedule [--at HH:MM] [--install]  automatic refresh at a fixed time
  vault-memory modules [--on X] [--off Y]         what is active; turn on and off
  vault-memory migrate [--apply]                  rewrite a legacy (Spanish) vault to the current names
  vault-memory install-cli                        put `vault-memory` on the PATH

No network, no model, no tokens. Python 3.9+, no dependencies.
"""
import argparse
import datetime as dt
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
ROOT = HERE.parent            # plugin root: standard/ lives next to scripts/

import contract as CT  # noqa: E402
import core as C  # noqa: E402
import evaluate as EV  # noqa: E402
import freshness as FR  # noqa: E402
import indexer as IX  # noqa: E402
import legacy as LG  # noqa: E402
import mcp_server as MCP  # noqa: E402
import note as NT  # noqa: E402
import panel as PN  # noqa: E402
import patterns as PT  # noqa: E402
import recall as RC  # noqa: E402
import schedule as SC  # noqa: E402
import telemetry as TL  # noqa: E402

# ──────────────────────────────────────────────────────────────────── init
LAYOUT = ["00 Index", "10 Log/Daily", "10 Log/Periods",
          "20 Records/Incidents", "20 Records/Meetings", "20 Records/Decisions",
          "30 Knowledge", "40 Runbooks", "50 Projects", "90 Templates",
          "_attachments", "_archive"]
TEMPLATES_FOLDERS = ("90 Templates", "Templates", "Plantillas")
STANDARD_TEMPLATES = {"Incident.md", "Runbook.md", "Meeting.md", "Decision.md", "Project.md"}
STANDARD_BASES = {"Map.base", "Timeline.base", "Records.base", "Runbooks.base"}

# Hints to guess the type of existing folders. Compared by WHOLE word, not substring: "sop"
# inside "Support" would type a folder of cases as runbook. Order matters: the first hit wins.
TYPE_HINTS = [
    ({"incident", "incidents", "incidente", "incidentes", "case", "cases", "caso", "casos",
      "postmortem", "postmortems", "bug", "bugs", "incidencia", "incidencias"}, "incident"),
    ({"runbook", "runbooks", "playbook", "playbooks", "procedure", "procedures",
      "procedimiento", "procedimientos", "sop", "sops"}, "runbook"),
    ({"meeting", "meetings", "reunion", "reuniones", "minutes", "minuta", "minutas", "1on1",
      "1a1"}, "meeting"),
    ({"decision", "decisions", "decisiones", "adr", "adrs", "rfc", "rfcs"}, "decision"),
    ({"daily", "dailies", "diario", "diarios", "journal", "log", "bitacora"}, "daily"),
    ({"week", "weeks", "semana", "semanas", "month", "months", "mes", "meses", "period",
      "periods", "periodo", "periodos", "sprint", "sprints"}, "period"),
    ({"project", "projects", "proyecto", "proyectos", "repo", "repos"}, "project"),
    ({"template", "templates", "plantilla", "plantillas"}, "template"),
    ({"glossary", "glosario", "glosarios", "schema", "schemas", "esquema", "esquemas",
      "reference", "references", "referencia", "referencias", "taxonomy", "taxonomia"}, "reference"),
    ({"documentation", "documentacion", "docs", "doc", "knowledge", "conocimiento", "guides",
      "guide", "guias", "guia"}, "doc"),
    ({"index", "indices", "indice", "home", "portada", "moc"}, "home"),
]


def _suggest_type(name):
    words = set(re.split(r"[^a-z0-9]+", C.norm(name))) - {""}
    for keys, note_type in TYPE_HINTS:
        if words & keys:
            return note_type
    return None


def _slug(text):
    """'20 Records' -> 'records'. The numeric prefix orders the folder in the explorer; as an
    area name it adds nothing."""
    clean = re.sub(r"^\s*\d+[\s._-]*", "", C.norm(text)).strip()
    return re.sub(r"[^a-z0-9]+", "-", clean).strip("-") or "general"


def _combine(base, own):
    """Recursive merge: a user value wins, unless it is empty."""
    out = dict(base)
    for key, value in (own or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _combine(out[key], value)
        elif value not in ("", [], {}, None):
            out[key] = value
    return out


def _ask_profile():
    """Minimal stdin wizard. The good version of this question is asked by the setup skill;
    this is for whoever runs the CLI by hand."""
    print("\nWhat do you want to enable?\n")
    choices = list(C.PROFILES)
    for i, key in enumerate(choices, 1):
        print(f"  {i}) {key:<9} {C.PROFILE_DESC[key]}")
    print()
    while True:
        try:
            answer = input("Pick 1-3 [2]: ").strip() or "2"
        except EOFError:
            return "standard"
        if answer.isdigit() and 1 <= int(answer) <= len(choices):
            return choices[int(answer) - 1]
        print("  A number between 1 and 3.")


def _vault_notes(vault, excluded, limit=2000):
    for path in sorted(vault.rglob("*.md"))[:limit]:
        if set(path.relative_to(vault).parts[:-1]) & excluded:
            continue
        try:
            yield path.read_text(encoding="utf-8")
        except Exception:
            continue


def _detect_tickets(vault, excluded, minimum=2):
    """ABC-123 prefixes that really appear in the vault. The ones seen only once are usually
    noise (an acronym inside a sentence)."""
    pattern = re.compile(r"\b([A-Z][A-Z0-9]{1,5})-\d{1,6}\b")
    counts = Counter()
    for text in _vault_notes(vault, excluded):
        counts.update(pattern.findall(text))
    # Ties broken by name: most_common depended on filesystem order.
    return [k for k, n in sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:6] if n >= minimum]


def init(args):
    vault = Path(args.vault).expanduser().resolve() if args.vault else (C.find_vault() or Path.cwd())
    vault.mkdir(parents=True, exist_ok=True)
    empty = not any(vault.rglob("*.md"))
    print(f"vault: {vault}")
    if not (vault / ".obsidian").exists():
        print("  · no .obsidian/ yet — open it once in Obsidian so it exists")

    # 1. Previous config: re-running init does not erase what the user adjusted by hand.
    previous = C.read_config_file(vault)
    if previous:
        print("  · already initialized: hand-made settings are kept")

    # 2. Profile: it defines which modules run, so it is resolved BEFORE everything else.
    if args.wizard:
        profile = _ask_profile()
    elif args.profile:
        profile = args.profile
    else:
        profile = None if previous.get("modules") else "standard"
    mods = dict(C.PROFILES[profile]) if profile else C.modules(previous)
    if profile:
        print(f"  · profile {profile}: {C.PROFILE_DESC[profile]}")
    if args.no_standard:                    # shortcut: turns off the two modules that write
        mods["bases"] = mods["templates"] = False

    if args.scaffold:
        for folder in LAYOUT:
            (vault / folder).mkdir(parents=True, exist_ok=True)
        print(f"  · reference structure created ({len(LAYOUT)} folders)")

    # 3. Folder → area and type mapping.
    cfg = json.loads(json.dumps(C.CONFIG_DEF))
    cfg["vault"], cfg["modules"] = vault.name, mods
    excluded = set(C.EXCLUDE) | set(C.ARCHIVE)
    for folder in sorted(x for x in vault.iterdir()
                         if x.is_dir() and x.name not in excluded and not x.name.startswith(".")):
        cfg["areas"][folder.name] = _slug(folder.name)
        hint = _suggest_type(folder.name)
        if hint:
            cfg["types_by_folder"][folder.name] = hint
        for sub in sorted(x for x in folder.iterdir() if x.is_dir() and not x.name.startswith(".")):
            sub_hint = _suggest_type(sub.name)
            if sub_hint:
                cfg["types_by_folder"][f"{folder.name}/{sub.name}"] = sub_hint

    # 4. Search language, detected from the notes.
    if not (previous.get("language") or {}).get("stemmer"):
        language = C.detect_language(_vault_notes(vault, excluded, limit=300))
        cfg["language"]["stemmer"] = language
        print(f"  · search language: {language}")

    # 5. Ticket prefixes: read from the vault instead of asked. Only if relevant.
    if mods["tickets"]:
        prefixes = _detect_tickets(vault, C.excluded_folders(cfg))
        if prefixes:
            cfg["references"]["tickets"]["prefixes"] = prefixes
            print(f"  · ticket prefixes detected: {', '.join(prefixes)}"
                  "   (fill in the URL in config.json to get clickable links)")
        else:
            print("  · tickets module active but no ABC-123 keys found in the vault")

    cfg = _combine(cfg, previous)           # what the user set by hand wins
    cfg["modules"] = mods                   # …except modules, defined by the profile
    C.save_config(vault, cfg)
    print(f"  · config written · {len(cfg['areas'])} areas, "
          f"{len(cfg['types_by_folder'])} typed folders · "
          f"{sum(1 for x in mods.values() if x)}/{len(mods)} modules active")

    # 6. Standard files: only what the modules allow to write inside the vault.
    if mods["bases"] or mods["templates"]:
        templates_folder = next((c for c in TEMPLATES_FOLDERS if (vault / c).is_dir()), TEMPLATES_FOLDERS[0])
        pairs = []
        if mods["bases"]:
            pairs.append((ROOT / "standard" / "bases",
                          vault / "00 Index" if (vault / "00 Index").is_dir() else vault, "Bases"))
        if mods["templates"]:
            pairs.append((ROOT / "standard" / "templates", vault / templates_folder, "templates"))
        for source, target, label in pairs:
            if not source.is_dir():
                continue
            target.mkdir(parents=True, exist_ok=True)
            copied = 0
            for path in sorted(source.iterdir()):
                destination = target / path.name
                if not destination.exists():
                    shutil.copy2(path, destination)
                    copied += 1
            if copied:
                print(f"  · {copied} {label} in {target.relative_to(vault)}/")
    else:
        print("  · nothing is written inside the vault (Bases and templates off)")

    (C.state_dir(vault) / ".gitignore").write_text(
        "# derived state: regenerated with `vault-memory refresh`\n*\n!.gitignore\n!config.json\n",
        encoding="utf-8")

    print()
    cfg = C.load_config(vault)
    _, _, coverage = IX.build(cfg)
    print()
    if empty:
        print("Empty vault: create your first note and run `vault-memory index` again.")
    elif C.enabled(cfg, "contract"):
        print(f"Contract coverage: {coverage['coverage']}%.")
        print("Next step:  vault-memory doctor    (says what is missing, read only)")
        print("Then:       vault-memory fix --apply")
    else:
        print("Done. Try:  vault-memory recall \"...\"")
    print(f"\nModules:  vault-memory modules   ·  config: {vault.name}/{C.state_dir(vault).name}/config.json")


# ───────────────────────────────────────────────────────────────── modules
# Files each module leaves inside the vault. Turning it off offers to clean them:
# a module that is off but keeps leaving files is not off.
LEFTOVERS = {
    "panel":     lambda v, s: [s / "panel.html"],
    "patterns":  lambda v, s: [s / "patterns.json"],
    "contract":  lambda v, s: [s / "contract.json"],
    "bases":     lambda v, s: [x for x in v.rglob("*.base") if x.name in STANDARD_BASES],
    "schedule":  lambda v, s: [SC.plist_path(v)] if SC.system() == "mac" else [],
    "templates": lambda v, s: [x for x in v.rglob("*.md")
                               if x.parent.name in TEMPLATES_FOLDERS and x.name in STANDARD_TEMPLATES],
}


def modules(args, cfg):
    vault, state = cfg["_vault"], cfg["_state"]
    current = C.modules(cfg)

    if args.profile:
        current = dict(C.PROFILES[args.profile])
        print(f"profile {args.profile} — {C.PROFILE_DESC[args.profile]}")

    changes = []
    for name in (args.on or []):
        if name not in C.MODULES:
            raise SystemExit(f"Unknown module: {name}")
        if not current[name]:
            changes.append((name, True))
        current[name] = True
    for name in (args.off or []):
        if name not in C.MODULES:
            raise SystemExit(f"Unknown module: {name}")
        if current[name]:
            changes.append((name, False))
        current[name] = False

    if args.profile or changes:
        cfg["modules"] = current
        C.save_config(vault, cfg)

    width = max(len(k) for k in C.MODULES)
    print()
    print(f"  {'':2} {'module'.ljust(width)}  {'':9} what it does")
    for key, (_title, description) in C.MODULES.items():
        on = current[key]
        print(f"  {'●' if on else '○'}  {key.ljust(width)}  {'on' if on else 'off':<9} {description}")
    print(f"\n  ●  {'recall'.ljust(width)}  {'always':<9} index and search: it is the tool, not an option")

    # cleanup of what a module just turned off left behind
    if any(n == "schedule" and not on for n, on in changes):
        ok, detail = SC.remove(vault)
        print(f"\n  scheduled refresh: {'removed' if ok else detail}")
    turned_off = [n for n, on in changes if not on and n in LEFTOVERS]
    if turned_off:
        leftover = [f for n in turned_off for f in LEFTOVERS[n](vault, state) if f.exists()]
        if leftover:
            print(f"\n  {len(leftover)} file(s) no longer needed:")
            for path in leftover[:12]:
                try:
                    print(f"    {path.relative_to(vault)}")
                except ValueError:
                    print(f"    {path}")
            if args.clean:
                for path in leftover:
                    try:
                        path.unlink()
                    except Exception as e:
                        print(f"    could not delete {path.name}: {e}")
                print(f"  {len(leftover)} deleted.")
            else:
                print("  To delete them:  vault-memory modules --clean")
    if args.profile or changes:
        print("\n  Run `vault-memory refresh` to regenerate the state with the new config.")


def _require(cfg, module, command):
    if not C.enabled(cfg, module):
        raise SystemExit(
            f"The '{module}' module is off, so `{command}` does nothing.\n"
            f"  To turn it on:  vault-memory modules --on {module}")


# ────────────────────────────────────────────────────────────────── doctor
def doctor(args, cfg):
    # Read-only diagnosis: computes everything in memory and leaves the state untouched.
    docs, link_graph, coverage = IX.build(cfg, quiet=True, write=False)
    result = PT.build(cfg, docs, link_graph, coverage, write=False)
    report = FR.analyze({"docs": docs}, cfg)
    if args.json:
        print(json.dumps({"health": result["health"], "gaps": link_graph["gaps"], "freshness": report},
                         ensure_ascii=False, indent=1))
        return
    health = result["health"]
    print(f"\n🧠 {cfg['_vault'].name}\n")
    mods = C.modules(cfg)
    print(f"  indexed notes        {health['notes']}")
    print(f"  links between notes  {health['edges']}")
    if mods["contract"]:
        print(f"  contract             {health['contract_coverage']}%  ({health['conforming']}/{coverage['notes']})")
    if mods["graph"]:
        print(f"  orphan notes         {health['orphans']}")
        print(f"  broken wikilinks     {health['broken_links']}")
        if health.get("duplicates"):
            print(f"  duplicate names      {health['duplicates']}   (wikilinks will fail)")
    if mods["patterns"]:
        print(f"  open records         {health['open_records']} of {health['records']}")
    for key, value in link_graph["gaps"].items():
        print(f"  {key:<26} {value['n']}")
    off = [k for k, on in mods.items() if not on]
    if off:
        print(f"\n  modules off: {', '.join(off)}   (vault-memory modules)")
    if LG.is_legacy_config(json.loads((cfg["_state"] / "config.json").read_text(encoding="utf-8"))) \
            or cfg["_state"].name == C.LEGACY_STATE:
        print("\n  legacy names in use: run `vault-memory migrate` to rewrite them")
    print()
    if mods["contract"]:
        CT.report(coverage, CT.plan(docs, cfg))
    if result["stale"]:
        print(f"\n{len(result['stale'])} open for more than {PT.stale_threshold(cfg)} days:")
        for entry in result["stale"][:8]:
            print(f"  {entry['days']:>4} d · {entry['status']:<18} {entry['t'][:60]}")
    repeated = [r for r in result["recurrence"] if r["n"] >= 2]
    if repeated:
        print("\nRepeats:")
        for row in repeated[:8]:
            print(f"  ×{row['n']:<3} {row['class']:<28} last {row['last'] or '—'}")
    _freshness(report, cfg)


def _freshness(report, cfg):
    """What in memory can no longer be trusted. First what is a sure error (two current notes
    with the same key), then what expired, and at the end the suspicions."""
    if not any(report.values()):
        return
    print("\nFreshness:")
    for item in report["contradictions"]:
        print(f"  ⚠ contradiction · key '{item['key']}': {' | '.join(item['notes'])}")
    for path in report["expired"][:8]:
        print(f"  ⌛ expired         {path}")
    for item in report["replaced"][:8]:
        print(f"  ↪ replaced        {item['p']}  →  {item['by']}")
    for item in report["near_duplicates"][:8]:
        if item["score"] >= 0.98:
            # Same content: not "similar", the same note twice.
            print(f"  ⧉ duplicates ({item['score']:.2f})  {item['a']}  |  {item['b']}\n"
                  f"      → vault-memory note --update \"{item['a']}\" --replaces \"{Path(item['b']).stem}\"  (and archive the other)")
        else:
            print(f"  ≈ near duplicates ({item['score']:.2f})  {item['a']}  |  {item['b']}")
    for item in report["possible_contradictions"][:5]:
        print(f"  ? review ({item['reason']})  {item['a']}  |  {item['b']}")
    count = len(report["unverified"])
    if count:
        oldest = report["unverified"][0]
        print(f"  {count} unverified notes (more than {cfg['freshness']['review_days']} days, or their type's limit) "
              f"(oldest: {oldest['p']}, {oldest['days']} d)")


# ───────────────────────────────────────────────────────────────── actions
def cmd_index(args, cfg):
    IX.build(cfg)


def cmd_recall(args, cfg):
    idx = RC.load(cfg, auto=not args.no_auto)
    if args.related:
        print(f"  related.json  {RC.write_related(cfg, idx)} notes with neighbors")
        return
    if args.like:
        results, query_terms = RC.similar(idx, args.like, args.n), []
    else:
        if not args.query:
            raise SystemExit("The query is missing.  vault-memory recall \"...\"")
        results = RC.search(idx, args.query, note_type=args.type, area=args.area, status=args.status,
                            system=args.system, since=args.since, family=args.family,
                            include_archive=args.archive, n=args.n, include_expired=args.include_expired)
        query_terms = list(C.tokens(args.query, keep_stopwords=True))
        if os.environ.get("VAULT_MEMORY_ORIGIN") != "hook":   # the hook records its own
            TL.record(cfg["_vault"], {"ev": "recall", "origin": "cli", "q": args.query[:200],
                                      "n": len(results), "top": round(results[0][0], 1) if results else 0}, cfg)
    if args.brief:
        # One line per note with the short passage: often enough to answer without reading
        # the whole note (the biggest token saving after not searching elsewhere).
        for score, d in results:
            print(f"{score:5.1f}  {d['p']}  [{FR.state(d)}]  |  {RC.passage(cfg, d, query_terms, args.passage_width)}")
        return
    if args.json:
        print(json.dumps([{"score": s, **{k: d[k] for k in
                          ("p", "n", "t", "type", "area", "date", "status", "sev", "system", "ids", "refs")},
                           "state": FR.state(d), "replaced_by": d.get("replaced_by", ""),
                           "verified": d.get("verified", ""),
                           "passage": RC.passage(cfg, d, query_terms)} for s, d in results],
                         ensure_ascii=False, indent=1))
    else:
        RC.print_results(cfg, results, query_terms)


def cmd_fix(args, cfg):
    _require(cfg, "contract", "fix")
    CT.fix(cfg, args.apply)


def cmd_retype(args, cfg):
    _require(cfg, "contract", "retype")
    CT.retype(cfg, args.apply)


def cmd_patterns(args, cfg):
    _require(cfg, "patterns", "patterns")
    docs, link_graph, coverage = IX.build(cfg, quiet=True)
    result = PT.build(cfg, docs, link_graph, coverage)
    PT.summary(result)
    if args.note:
        print("  " + PT.write_note(cfg, result, args.note))


def cmd_panel(args, cfg):
    _require(cfg, "panel", "panel")
    docs, link_graph, coverage = IX.build(cfg, quiet=True)
    result = PT.build(cfg, docs, link_graph, coverage)
    out, count = PN.build(cfg, docs, link_graph, coverage, result, args.out)
    print(f"  {out}  ·  {count} notes  ·  {out.stat().st_size / 1024:.0f} KB")
    if args.open:
        opener = "open" if platform.system() == "Darwin" else "xdg-open"
        subprocess.run([opener, str(out)], capture_output=True)


def cmd_schedule(args, cfg):
    if not (args.off or args.status):
        _require(cfg, "schedule", "schedule")
    SC.run(args, cfg)


def _version():
    manifest = ROOT / ".claude-plugin" / "plugin.json"
    return json.loads(manifest.read_text(encoding="utf-8")).get("version", "?") if manifest.exists() else "?"


def cmd_info(args, cfg):
    """Which vault, which code and which version: the first thing to check if something does
    not add up between profiles."""
    index_file = cfg["_state"] / "index.json"
    stored = json.loads(index_file.read_text(encoding="utf-8")) if index_file.exists() else {}
    print(f"  vault      {cfg['_vault']}   (origin: {C.VAULT_ORIGIN})")
    sources = {k: os.environ.get(k) for k in (*C.ENV_VAULT, "CLAUDE_PLUGIN_OPTION_VAULT") if os.environ.get(k)}
    if C.profile_vault():
        sources["profile"] = C.profile_vault()
    if len({str(Path(v).expanduser().resolve()) for v in sources.values()}) > 1:
        print("  ⚠ different vault sources " + str(sorted(sources)) + ": the profile settings.json "
              "wins; if the environment differs, a project settings.json is overriding it")
    print(f"  version    {_version()}")
    print(f"  code       {HERE}")
    print(f"  python     {platform.python_version()}  ({sys.executable})")
    print(f"  state      {cfg['_state']}")
    print(f"  language   {cfg['language'].get('stemmer')}")
    print(f"  index      {stored.get('notes', 0)} notes · tok {stored.get('tok', '-')} · "
          f"{stored.get('generated', 'not built')}")


def cmd_config(args, cfg):
    """Which config keys are missing (and use their default). --write writes them once, so
    config.json says explicitly what the engine is using."""
    own = C.read_config_file(cfg["_vault"])

    def missing(base, mine, prefix=""):
        for key, value in base.items():
            if key not in mine:
                yield prefix + key
            elif isinstance(value, dict) and isinstance(mine[key], dict):
                yield from missing(value, mine[key], prefix + key + ".")
    keys = list(missing(C.CONFIG_DEF, own))
    if not keys:
        print("  config.json complete: no key is missing")
        return
    print("  keys using their default value (not in config.json):")
    for key in keys:
        print(f"    {key}")
    if args.write:
        C.save_config(cfg["_vault"], cfg)
        print(f"\n  written to {cfg['_state'] / 'config.json'}")
    else:
        print("\n  To write them:  vault-memory config --write")


def cmd_note(args, cfg):
    body = args.body or ""
    if args.body_file:
        body = Path(args.body_file).read_text(encoding="utf-8")
    elif args.stdin:
        body = sys.stdin.read()
    fields = {}
    for pair in args.field or []:
        key, sep, value = pair.partition("=")
        if not key or not sep:
            raise SystemExit(f"--field expects key=value, not {pair!r}")
        fields[key.strip()] = value.strip()
    try:
        result = NT.write(cfg, title=args.title, note_type=args.type, folder=args.folder, body=body,
                          area=args.area, tags=[t.strip() for t in args.tags.split(",")] if args.tags else None,
                          source=args.source, verified=args.verified, replaces=args.replaces,
                          key=args.key, expires=args.expires, fields=fields, update=args.update,
                          append=args.append, force=args.force, dry_run=args.dry_run)
    except NT.NoteError as e:
        if args.json:
            print(json.dumps({"error": e.message, **e.details}, ensure_ascii=False, indent=1))
        else:
            print(f"vault-memory note: {e.message}", file=sys.stderr)
            for dup in e.details.get("duplicates", []):
                print(f"  {dup['score']:.2f}  {dup['p']}   ({dup['reason']})", file=sys.stderr)
        sys.exit(e.code)
    if not result.get("dry_run"):
        TL.record(cfg["_vault"], {"ev": "note", "p": result["p"], "mode": result["mode"]}, cfg)
    if args.json:
        print(json.dumps({k: v for k, v in result.items() if k != "text" or args.dry_run},
                         ensure_ascii=False, indent=1))
    elif result.get("dry_run"):
        print(result["text"])
    else:
        print(f"  {result['mode']}  {result['p']}")


def cmd_mcp(args, cfg):
    MCP.serve(cfg, _version())


def cmd_usage(args, cfg):
    """Does memory help? Metrics from this vault's local telemetry."""
    if args.tokens:
        folder = args.transcripts or str(Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects")
        cost = TL.tokens(folder, cfg["_vault"], args.days)
        if args.json:
            print(json.dumps(cost, ensure_ascii=False, indent=1))
            return
        print(f"\n  vault-memory cost in the context · last {args.days} days · {folder}\n")
        print(f"  sessions                 {cost['sessions']}  (with memory: {cost['sessions_with_memory']})")
        print(f"  hook pointers            {cost['pointers']}  · {cost['hook_bytes'] / 1024:.1f} KB · "
              f"median {cost['hook_ms_median']} ms")
        print(f"  CLI via Bash/Skill       {cost['recalls']}  · {cost['recall_bytes'] / 1024:.1f} KB")
        print(f"  Reads of vault notes     {cost['reads']}  · {cost['read_bytes'] / 1024:.1f} KB")
        print(f"  total                    {cost['total_bytes'] / 1024:.1f} KB ≈ {cost['estimated_tokens']:,} tokens (bytes/3.6)")
        print(f"  session context          cache_creation {cost['cache_creation']:,} · cache_read {cost['cache_read']:,}")
        return
    stats = TL.metrics(TL.read(cfg["_vault"], args.days))
    if args.suggest_eval:
        # Notes read without the hook suggesting them, with the request that led to reading
        # them: candidates for a new eval.tsv case (review them before pasting).
        for item in stats["missed_recall"]:
            if item["q"]:
                print(f"{item['q'].replace(chr(9), ' ').replace(chr(10), ' ')}\t{Path(item['p']).stem}")
        return
    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=1))
        return
    print(f"\n  usage · last {args.days} days · {cfg['_vault'].name}\n")
    print(f"  sessions                 {stats['sessions']}")
    print(f"  sessions with a pointer  {stats['sessions_with_pointer']}")
    print(f"  pointers emitted         {stats['pointers']}")
    print(f"  pointers used            {stats['pointers_read']}  ({stats['pct_read']}%)")
    print(f"  notes written            {stats['notes_written']}")
    if stats["silent_reasons"]:
        print("  hook stayed silent       " + " · ".join(f"{k} {v}" for k, v in stats["silent_reasons"].items()))
    if stats["never_read"]:
        print("\n  suggested and never read (noise? → recall.skip_paths or improve the note):")
        for path in stats["never_read"]:
            print(f"    {path}")
    if stats["missed_recall"]:
        print("\n  read without a prior pointer (missed recall → vault-memory usage --suggest-eval):")
        for item in stats["missed_recall"][:10]:
            print(f"    {item['p']}   ← {item['q'][:60]!r}")
    if stats["gaps"]:
        print("\n  searches without results (a missing note? → vault-memory note):")
        for query in stats["gaps"]:
            print(f"    {query[:80]}")


def cmd_eval(args, cfg):
    EV.run(cfg, args.tsv, args.minimum, hook=args.hook)


def cmd_refresh(args, cfg):
    started, mods = dt.datetime.now(), C.modules(cfg)
    docs, link_graph, coverage = IX.build(cfg)
    result = PT.build(cfg, docs, link_graph, coverage)
    if mods["patterns"]:
        PT.summary(result)
        if args.note:
            # the derived note is one more note: rescan so it enters the index
            print("  " + PT.write_note(cfg, result, args.note))
            docs, link_graph, coverage = IX.build(cfg, quiet=True)
            result = PT.build(cfg, docs, link_graph, coverage)
    idx = json.loads((cfg["_state"] / "index.json").read_text(encoding="utf-8"))
    print(f"  related.json   {RC.write_related(cfg, idx)} notes with neighbors")
    target = None
    if mods["panel"]:
        target, count = PN.build(cfg, docs, link_graph, coverage, result)
        print(f"  panel.html     {count} notes · {target.stat().st_size / 1024:.0f} KB")
    off = [k for k, on in mods.items() if not on]
    if off:
        print(f"  (off: {', '.join(off)})")
    print(f"\n  done in {(dt.datetime.now() - started).total_seconds():.1f}s"
          + (f"  ·  {target}" if target else ""))


# ───────────────────────────────────────────────────────────────── migrate
def cmd_migrate(args, cfg):
    """Rewrite a vault made by an earlier version: Spanish frontmatter keys and type values,
    Spanish config keys, and the `.cerebro/` state folder. Dry run unless --apply."""
    vault = cfg["_vault"]
    legacy_state = cfg["_state"].name == C.LEGACY_STATE
    config_file = cfg["_state"] / "config.json"
    raw_config = json.loads(config_file.read_text(encoding="utf-8"))
    legacy_config = LG.is_legacy_config(raw_config)
    changed, bases = [], []
    for path, rel in C.walk(cfg, md_only=False):
        if IX.SENSITIVE.search(rel) or not rel.endswith((".md", ".base")):
            continue
        migrate = LG.migrate_note if rel.endswith(".md") else LG.migrate_base
        try:
            new_text = migrate(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if new_text is not None:
            (changed if rel.endswith(".md") else bases).append((rel, new_text))

    print(f"\n  migrate · {vault.name}\n")
    print(f"  state folder   {'.cerebro/ → .vault-memory/' if legacy_state else 'ok'}")
    print(f"  config.json    {'legacy keys → current keys' if legacy_config else 'ok'}")
    print(f"  notes          {len(changed)} with legacy frontmatter")
    for rel, new_text in changed[:5]:
        print(f"    {rel}")
        new_head = C.raw_frontmatter(new_text)[0]
        print("      → " + ", ".join(f"{k}: {v}" for k, v in list(new_head.items())[:5]))
    if len(changed) > 5:
        print(f"    … and {len(changed) - 5} more")
    print(f"  bases          {len(bases)} .base views with legacy keys")
    for rel, _ in bases[:5]:
        print(f"    {rel}")
    if not (legacy_state or legacy_config or changed or bases):
        print("\n  Nothing to migrate.")
        return
    if not args.apply:
        print("\n  Dry run. To apply (every note is backed up first):  vault-memory migrate --apply")
        return

    if legacy_state:
        (vault / C.LEGACY_STATE).rename(vault / C.STATE)
        cfg["_state"] = C.state_dir(vault)
    if legacy_config:
        C.save_config(vault, {**C.read_config_file(vault)})
    target = CT.backup_dir(cfg)
    for rel, new_text in changed + bases:
        CT.backup(cfg, rel, target)
        NT._write_atomic(vault / rel, new_text)
    cfg = C.load_config(vault)
    IX.build(cfg, quiet=True)
    print(f"\n  migrated · {len(changed)} notes · {len(bases)} bases · backup in {C.STATE}/backups/{target.name}/")
    print("  If an environment variable still says CEREBRO_VAULT, rename it to VAULT_MEMORY_VAULT.")


# ───────────────────────────────────────────────────────────── install-cli
SHIM = """#!/usr/bin/env bash
# vault-memory CLI shim (written by `vault-memory install-cli`).
# Runs the installed plugin copy. Plugin updates move that copy to a new folder, so it is
# looked up on every call: VAULT_MEMORY_HOME, then the active Claude Code profile, then the
# copy this shim was written from.
root="${VAULT_MEMORY_HOME:-}"
if [ -z "$root" ]; then
  root=$(python3 - "${CLAUDE_CONFIG_DIR:-$HOME/.claude}" <<'PY'
import json, sys, pathlib
profile = pathlib.Path(sys.argv[1]).expanduser()
try:
    data = json.loads((profile / "plugins/installed_plugins.json").read_text())
    print(data["plugins"]["vault-memory@vault-memory"][0]["installPath"])
except Exception:
    found = sorted(profile.glob("plugins/cache/*/vault-memory/*/scripts/cli.py"))
    print(found[-1].parent.parent if found else "")
PY
)
fi
[ -f "$root/scripts/cli.py" ] || root="__ROOT__"
exec python3 "$root/scripts/cli.py" "$@"
"""


def cmd_install_cli(args):
    target = Path(args.dir).expanduser() / "vault-memory"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(SHIM.replace("__ROOT__", str(ROOT)), encoding="utf-8")
    target.chmod(0o755)
    print(f"  written {target}")
    if str(target.parent) not in os.environ.get("PATH", "").split(os.pathsep):
        print(f"  {target.parent} is not on PATH: add it to your shell profile")


# ──────────────────────────────────────────────────────────────────── main
def main():
    parser = argparse.ArgumentParser(prog="vault-memory", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vault", help="path to the vault (default: VAULT_MEMORY_VAULT or search from the current folder)")
    parser.add_argument("--today", metavar="YYYY-MM-DD",
                        help="pin today's date (stale notes, expiry): reproducible output")
    # --vault works before or after the subcommand. SUPPRESS keeps the subparser from
    # overwriting with None the value the top level already brought.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--vault", default=argparse.SUPPRESS, help="path to the vault")
    sub = parser.add_subparsers(dest="cmd", required=True, parser_class=lambda **kw:
                                argparse.ArgumentParser(parents=[common], **kw))

    p = sub.add_parser("init")
    p.add_argument("--scaffold", action="store_true", help="create the reference folder structure")
    p.add_argument("--no-standard", dest="no_standard", action="store_true",
                   help="do not install Bases or templates in the vault")
    p.add_argument("--profile", choices=list(C.PROFILES), help="which modules to enable (default: standard)")
    p.add_argument("--wizard", action="store_true", help="ask for the profile interactively")
    p.set_defaults(fn=init, no_cfg=True)

    p = sub.add_parser("modules")
    p.add_argument("--on", nargs="+", metavar="MOD", help="turn modules on")
    p.add_argument("--off", nargs="+", metavar="MOD", help="turn modules off")
    p.add_argument("--profile", choices=list(C.PROFILES), help="apply a whole profile")
    p.add_argument("--clean", action="store_true", help="delete the files left by modules just turned off")
    p.set_defaults(fn=modules)
    p = sub.add_parser("index")
    p.set_defaults(fn=cmd_index)
    p = sub.add_parser("doctor")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=doctor)

    p = sub.add_parser("recall")
    p.add_argument("query", nargs="?", default="")
    p.add_argument("--type")
    p.add_argument("--area")
    p.add_argument("--family")
    p.add_argument("--status")
    p.add_argument("--system")
    p.add_argument("--since")
    p.add_argument("--n", type=int, default=8)
    p.add_argument("--archive", action="store_true", help="include archived notes")
    p.add_argument("--like", help="notes similar to this one")
    p.add_argument("--related", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-auto", dest="no_auto", action="store_true")
    p.add_argument("--brief", action="store_true", help="one line per note with a short passage")
    p.add_argument("--passage-width", dest="passage_width", type=int, default=160)
    p.add_argument("--include-expired", dest="include_expired", action="store_true",
                   help="do not lower the score of replaced or expired notes")
    p.set_defaults(fn=cmd_recall)

    p = sub.add_parser("fix")
    p.add_argument("--apply", action="store_true")
    p.set_defaults(fn=cmd_fix)
    p = sub.add_parser("retype")
    p.add_argument("--apply", action="store_true")
    p.set_defaults(fn=cmd_retype)
    p = sub.add_parser("patterns")
    p.add_argument("--note")
    p.set_defaults(fn=cmd_patterns)
    p = sub.add_parser("panel")
    p.add_argument("--out")
    p.add_argument("--open", action="store_true")
    p.set_defaults(fn=cmd_panel)
    p = sub.add_parser("refresh")
    p.add_argument("--note")
    p.set_defaults(fn=cmd_refresh)
    p = sub.add_parser("info")
    p.set_defaults(fn=cmd_info)
    p = sub.add_parser("config")
    p.add_argument("--write", action="store_true")
    p.set_defaults(fn=cmd_config)

    p = sub.add_parser("note", help="create or update a note following the contract, without duplicates")
    p.add_argument("--title")
    p.add_argument("--type")
    p.add_argument("--folder")
    p.add_argument("--area")
    p.add_argument("--tags", help="a,b,c")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--body", help="note text")
    group.add_argument("--body-file", dest="body_file")
    group.add_argument("--stdin", action="store_true", help="read the body from stdin")
    p.add_argument("--source")
    p.add_argument("--verified", help="YYYY-MM-DD or today")
    p.add_argument("--replaces", help="note this one makes obsolete")
    p.add_argument("--key")
    p.add_argument("--expires", help="YYYY-MM-DD")
    p.add_argument("--field", action="append", metavar="K=V", help="extra frontmatter field (repeatable)")
    p.add_argument("--update", metavar="PATH", help="update this note instead of creating one")
    p.add_argument("--append", action="store_true", help="with --update: add the body as a dated section")
    p.add_argument("--force", action="store_true", help="create even if there are duplicates")
    p.add_argument("--dry-run", dest="dry_run", action="store_true", help="show what would be written")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_note)

    p = sub.add_parser("usage", help="memory usage metrics (local telemetry)")
    p.add_argument("--days", type=int, default=14)
    p.add_argument("--json", action="store_true")
    p.add_argument("--suggest-eval", dest="suggest_eval", action="store_true")
    p.add_argument("--tokens", action="store_true", help="bytes/tokens vault-memory adds to the context (from transcripts)")
    p.add_argument("--transcripts", help="transcripts folder (default $CLAUDE_CONFIG_DIR/projects)")
    p.set_defaults(fn=cmd_usage)
    p = sub.add_parser("mcp", help="MCP server over stdio (desktop chat, Codex, Cursor, Gemini, Copilot)")
    p.set_defaults(fn=cmd_mcp)
    p = sub.add_parser("eval")
    p.add_argument("--tsv", help="case set (default: the vault's eval.tsv)")
    p.add_argument("--minimum", type=int, help="exit 1 if top-1 is below (with --hook: hit@3, and noise > 0)")
    p.add_argument("--hook", action="store_true", help="measure what the hook would emit (hit@1, hit@3, noise)")
    p.set_defaults(fn=cmd_eval)

    p = sub.add_parser("schedule")
    p.add_argument("--at", metavar="HH:MM", help="time of the daily refresh, 24 h")
    p.add_argument("--days", default="daily", choices=list(SC.DAYS), help="daily | weekdays | weekend")
    p.add_argument("--install", action="store_true", help="really install (otherwise dry run)")
    p.add_argument("--off", action="store_true", help="remove the scheduled refresh")
    p.add_argument("--status", action="store_true", help="see what is scheduled")
    p.set_defaults(fn=cmd_schedule)

    p = sub.add_parser("migrate", help="rewrite legacy (Spanish) names to the current ones")
    p.add_argument("--apply", action="store_true")
    p.set_defaults(fn=cmd_migrate)
    p = sub.add_parser("install-cli", help="write the `vault-memory` command to a folder on PATH")
    p.add_argument("--dir", default=str(Path.home() / ".local" / "bin"))
    p.set_defaults(fn=cmd_install_cli, no_cfg=True)

    args = parser.parse_args()
    if getattr(args, "today", None):
        os.environ["VAULT_MEMORY_TODAY"] = args.today
    if not getattr(args, "vault", None):
        args.vault = None
    if getattr(args, "no_cfg", False):
        args.fn(args)
        return
    vault = C.resolve_vault(args.vault)
    args.fn(args, C.load_config(vault))


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        # `vault-memory recall ... | head` closes the pipe; not a program error.
        try:
            sys.stdout.close()
        except Exception:
            pass
        os._exit(0)
    except KeyboardInterrupt:
        sys.exit(130)
