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
  vault-memory move PLAN.tsv [--apply]            move/rename by a plan; links, config and state follow
  vault-memory ingest --file F [--source S]       learn from a thread, issue or PR an agent collected
  vault-memory capture FILE|- [--title T]         keep raw material as it arrived (ingest --capture PATH)
  vault-memory review [PATH --verdict V]          what to check against its source; record a check
  vault-memory review --budget | --calibration    today's check count; does confidence hold up?
  vault-memory review PATH --flag "reason"        disputed in a conversation: check it first
  vault-memory inbox [--accept ID | --reject ID]  learned candidates waiting: list, accept, reject
  vault-memory answer --question Q --cites A,B    keep an answer as a note (unverified, reviewed later)
  vault-memory bench [--tune [--apply]]           does recall find what gets used? (own history)
  vault-memory bench --compare                    plain BM25 against the semantic rerank, paired
  vault-memory embed [--full]                     vectors for the semantic rerank (config `embeddings`)
  vault-memory bases [--install] [--folder F]     standard Obsidian views (.base) missing in the vault
  vault-memory links [--n 20]                     notes that talk about the same thing and are not linked
  vault-memory install-cli                        put `vault-memory` on the PATH

No network, no model, no tokens (unless you turn on `distill` or `embeddings`). Python 3.9+, no dependencies.
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
import distill as DS  # noqa: E402
import embeddings as EM  # noqa: E402
import bench as BN  # noqa: E402
import catalog as CA  # noqa: E402
import evaluate as EV  # noqa: E402
import freshness as FR  # noqa: E402
import indexer as IX  # noqa: E402
import legacy as LG  # noqa: E402
import links as LK  # noqa: E402
import mcp_server as MCP  # noqa: E402
import note as NT  # noqa: E402
import oplog as OL  # noqa: E402
import panel as PN  # noqa: E402
import patterns as PT  # noqa: E402
import recall as RC  # noqa: E402
import review as RV  # noqa: E402
import calibration as CB  # noqa: E402
import schedule as SC  # noqa: E402
import telemetry as TL  # noqa: E402
import today as TD  # noqa: E402

# ──────────────────────────────────────────────────────────────────── init
LAYOUT = ["00 Index", "10 Log/Daily", "10 Log/Periods",
          "20 Records/Incidents", "20 Records/Meetings", "20 Records/Decisions",
          "30 Knowledge", "40 Runbooks", "50 Projects", "90 Templates",
          "_attachments", "_archive"]
TEMPLATES_FOLDERS = ("90 Templates", "Templates", "Plantillas")
STANDARD_TEMPLATES = {"Incident.md", "Runbook.md", "Meeting.md", "Decision.md", "Project.md"}
STANDARD_BASES = {"Map.base", "Timeline.base", "Records.base", "Runbooks.base", "Memory.base"}

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
    properties = CT.property_issues(cfg)
    import capture as CP
    captures = CP.report(cfg, docs)
    long_notes = _long_notes(cfg, docs)
    if args.json:
        print(json.dumps({"health": result["health"], "gaps": link_graph["gaps"], "freshness": report,
                          "properties": properties, "long_notes": long_notes,
                          "captures": captures,
                          "distill": dict(zip(("provider", "ready", "detail"), DS.readiness(cfg))),
                          "embeddings": EM.status(cfg, docs)},
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
    if long_notes["threshold"] and long_notes["count"]:
        shown = long_notes["notes"][:10]
        print(f"\n{long_notes['count']} notes longer than {long_notes['threshold']} words "
              f"(long notes dilute recall; one screen is enough):")
        for item in shown:
            print(f"  {item['words']:>5}  {item['p']}")
        if long_notes["count"] > len(shown):
            print(f"  … {long_notes['count'] - len(shown)} more")
    if DS.options(cfg)["enabled"]:
        name, ok, detail = DS.readiness(cfg)
        print(f"\ndistill provider: {name} · {'ready' if ok else 'NOT ready'} · {detail}")
    if EM.enabled(cfg):
        emb = EM.status(cfg, docs)
        print(f"\nembeddings: {emb['provider']} · {emb['model'] or 'no model'} · embedded {emb['embedded']} of "
              f"{emb['eligible']} · stale {emb['stale']} · {'ready' if emb['ready'] else 'NOT ready'} · {emb['detail']}")
    if properties:
        kinds = Counter(i["issue"] for i in properties)
        print(f"\nObsidian properties ({len(properties)}): " + ", ".join(f"{k} {v}" for k, v in sorted(kinds.items())))
        for item in properties[:5]:
            print(f"  {item['issue']:<32} {item['key']:<14} {item['p']}")
    _captures(captures)


def _captures(report):
    """Captures are evidence: report the ones edited after capture and the notes citing a
    capture that is gone. Report only."""
    if not (report["n"] or report["dangling"]):
        return
    print(f"\nCaptures ({report['folder']}/): {report['n']}")
    for path in report["edited"][:8]:
        print(f"  ✗ edited after capture  {path}   (body no longer matches its sha256)")
    for item in report["dangling"][:8]:
        print(f"  ⚠ missing capture       {item['p']}  →  [[{item['capture']}]]")


def _long_notes(cfg, docs):
    """Notes whose body (no frontmatter, no fenced code) is above doctor.long_words. Report only."""
    threshold = int(cfg["doctor"].get("long_words", 0) or 0)
    out = {"threshold": threshold, "count": 0, "notes": []}
    if threshold <= 0:
        return out
    skip = set(cfg["doctor"].get("long_skip_types", []))
    found = []
    for d in docs:
        # d["words"] counts code too, so it is an upper bound: only those can be long.
        if d["archived"] or d["type"] in skip or d["words"] <= threshold:
            continue
        try:
            text = (cfg["_vault"] / d["p"]).read_text(encoding="utf-8")
        except Exception:
            continue
        body = re.sub(r"^(```|~~~).*?^\1[^\n]*$", "", C.frontmatter(text)[1], flags=re.S | re.M)
        words = sum(1 for _ in C.TOK.finditer(C.norm(body)))
        if words > threshold:
            found.append({"p": d["p"], "words": words})
    found.sort(key=lambda x: (-x["words"], x["p"]))
    out.update(count=len(found), notes=found)
    return out


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
    for path in report.get("outdated", [])[:8]:
        print(f"  ✗ outdated        {path}   (a review found the source says otherwise)")
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
        hook = os.environ.get("VAULT_MEMORY_ORIGIN") == "hook"
        # The semantic rerank (embeddings on and `embed` run) reorders BM25's best candidates;
        # the prompt hook uses it only with `recall.rerank.hook`.
        rerank = not args.no_rerank and EM.enabled(cfg) and (not hook or EM.rerank_options(cfg)["hook"])
        wanted = max(args.n, int(EM.rerank_options(cfg)["candidates"])) if rerank else args.n
        results = RC.search(idx, args.query, note_type=args.type, area=args.area, status=args.status,
                            system=args.system, since=args.since, family=args.family,
                            include_archive=args.archive, n=wanted, include_expired=args.include_expired)
        reranked = False
        if rerank:
            results, reranked = EM.rerank(cfg, args.query, results, origin="hook" if hook else "cli")
        results = results[:args.n]
        query_terms = list(C.tokens(args.query, keep_stopwords=True))
        if not hook:                                            # the hook records its own
            TL.record(cfg["_vault"], {"ev": "recall", "origin": "cli", "q": args.query[:200],
                                      "n": len(results), "top": round(results[0][0], 1) if results else 0,
                                      "ps": [d["p"] for _, d in results[:3]],
                                      **({"rerank": True} if reranked else {})}, cfg)
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
                           "verified": d.get("verified", ""), "confidence": d.get("confidence", ""),
                           "checked": d.get("checked", ""), "volatility": d.get("volatility", ""),
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


def cmd_answer(args, cfg):
    """Keep an answer from a conversation as a note: unverified, expiring, queued for review."""
    import answer as AN
    body = args.body or ""
    if args.body_file:
        body = Path(args.body_file).read_text(encoding="utf-8")
    elif args.stdin:
        body = sys.stdin.read()
    cites = [c for c in (args.cites or "").split(",") if c.strip()]
    try:
        result = AN.save(cfg, args.question, body, cites, title=args.title, dry_run=args.dry_run)
    except NT.NoteError as e:
        if args.json:
            print(json.dumps({"error": e.message, **e.details}, ensure_ascii=False, indent=1))
        else:
            print(f"vault-memory answer: {e.message}", file=sys.stderr)
            for dup in e.details.get("duplicates", []):
                print(f"  {dup['score']:.2f}  {dup['p']}   ({dup['reason']})", file=sys.stderr)
        sys.exit(e.code)
    if args.json:
        print(json.dumps({k: v for k, v in result.items() if k != "text" or args.dry_run},
                         ensure_ascii=False, indent=1))
    elif result.get("dry_run"):
        print(result["text"])
    else:
        print(f"  saved  {result['p']}  (from {len(result['cites'])} notes, unverified, queued for review)")


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


def _print_distill(result):
    reasons = ", ".join(f"{k} {v}" for k, v in sorted(result["discarded_reasons"].items()))
    days = ", ".join(result["days"]) or "nothing new"
    print(f"  distill {days}  sessions {result['sessions']} · proposed {result['proposed']} · "
          f"kept {result['kept']} · updated {result['updated']} · replaced {result['replaced']} · "
          + (f"update proposals {result['update_proposals']} · " if result["update_proposals"] else "")
          + f"discarded {result['discarded']}" + (f" ({reasons})" if reasons else "")
          + f" · renewed {result['renewed']} · archived {result['archived']}"
          + (f" · errors {result['errors']}" if result["errors"] else "")
          + (f" · already done: {', '.join(result['skipped'])}" if result["skipped"] else ""))
    if result.get("write"):
        print(f"    learned notes → {result['write']['to']} ({result['write']['reason']})")


def cmd_distill(args, cfg):
    """Learn from yesterday's agent sessions (config `distill`)."""
    if not DS.options(cfg)["enabled"]:
        print("  distill is off: set `distill.enabled` in config.json")
        return
    if args.sources:
        cfg["distill"] = {**(cfg.get("distill") or {}), "sources": [s.strip() for s in args.sources.split(",") if s.strip()]}
    try:
        result = DS.run(cfg, day=args.day, dry_run=args.dry_run, force=args.force)
    except DS.DistillError as e:
        sys.exit(f"  distill: {e}")
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        _print_distill(result)


def cmd_ingest(args, cfg):
    """Learn from material an agent collected (a thread, an issue, a PR): JSON or Markdown."""
    import capture as CP
    import ingest as IG
    import sources as SR
    try:
        if args.capture:
            # a capture: its body, its url and date by default, and a `capture:` link on every candidate
            session = CP.session(cfg, args.capture, source=args.source, url=args.url, ref_id=args.id,
                                 date=args.date)
        else:
            raw = sys.stdin.read() if args.file == "-" else Path(args.file).expanduser().read_text(encoding="utf-8")
            session = SR.material(raw, source=args.source, url=args.url, ref_id=args.id, date=args.date,
                                  today=C.today())
    except CP.CaptureError as e:
        print(f"  ingest: {e.message}", file=sys.stderr)
        sys.exit(e.code)
    except (OSError, ValueError) as e:
        print(f"  ingest: {e}", file=sys.stderr)
        sys.exit(2)
    try:
        result = IG.run(cfg, session, dry_run=args.dry_run)
    except DS.DistillError as e:
        sys.exit(f"  ingest: {e}")
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return
    reasons = ", ".join(f"{k} {v}" for k, v in sorted(result["discarded_reasons"].items()))
    print(f"  ingest {result['source']} ({result['day']})  proposed {result['proposed']} · kept {result['kept']} · "
          f"updated {result['updated']} · replaced {result['replaced']} · "
          + (f"update proposals {result['update_proposals']} · " if result["update_proposals"] else "")
          + f"discarded {result['discarded']}"
          + (f" ({reasons})" if reasons else "") + ("  [dry run]" if args.dry_run else ""))


def _embed_line(result):
    return (f"embeddings     {result['model']} · {result['eligible']} eligible · embedded {result['embedded']} · "
            f"kept {result['kept']} · dropped {result['dropped']}"
            + (" · rebuilt" if result["rebuilt"] else "")
            + (f" · {result['pending']} pending" if result.get("pending") else "")
            + f" · {result['seconds']:.1f}s")


def cmd_embed(args, cfg):
    """Vectors for the semantic rerank: new or changed notes only (`--full`: all of them)."""
    if not EM.enabled(cfg):
        print("  embeddings are off: set `embeddings.provider` and `embeddings.model` in config.json")
        return
    try:
        result = EM.build(cfg, full=args.full)
    except EM.EmbedError as e:
        partial = getattr(e, "result", None)
        if args.json:
            print(json.dumps({"error": str(e), **(partial or {})}, ensure_ascii=False, indent=1))
        else:
            if partial and partial["embedded"]:
                print("  " + _embed_line(partial))
            print(f"  embed: {e}", file=sys.stderr)
        sys.exit(1)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print("  " + _embed_line(result))


def cmd_capture(args, cfg):
    """Keep raw material exactly as it arrived (redacted first), for notes to cite."""
    import capture as CP
    try:
        text = sys.stdin.read() if args.file == "-" else Path(args.file).expanduser().read_text(encoding="utf-8")
        result = CP.write(cfg, text, title=args.title, url=args.url, date=args.date)
    except CP.CaptureError as e:
        print(f"  capture: {e.message}", file=sys.stderr)
        sys.exit(e.code)
    except (OSError, ValueError) as e:
        print(f"  capture: {e}", file=sys.stderr)
        sys.exit(2)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print(result["p"])


def cmd_refresh(args, cfg):
    started, mods = dt.datetime.now(), C.modules(cfg)
    if DS.options(cfg)["enabled"] and not args.no_distill:
        # before indexing, so what it learned enters the index in this same run
        try:
            _print_distill(DS.run(cfg))
        except DS.DistillError as e:
            print(f"  distill: {e}")
    import answer as AN
    renewed, archived = AN.upkeep(cfg)          # saved answers expire like learned notes
    if renewed or archived:
        print(f"  answers        renewed {renewed} · archived {archived}")
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
    embed_ok = True
    if EM.enabled(cfg):
        # after indexing, so new notes get their vector today; a provider failure is one line
        try:
            print("  " + _embed_line(EM.build(cfg, docs=idx["docs"])))
        except Exception as e:
            embed_ok = False
            print(f"  embed: {e} (recall keeps working with plain BM25)")
    try:
        measured = BN.run(cfg, rerank=None if embed_ok else False)
        print(f"  bench.json     {measured['cases']} cases · MRR {measured['mrr']:.3f} · "
              f"suggestions used this week {measured['week']['pct_read']}%")
    except Exception as e:                     # measuring must never break the refresh
        print(f"  bench: {e}")
    target = None
    if mods["panel"]:
        target, count = PN.build(cfg, docs, link_graph, coverage, result)
        print(f"  panel.html     {count} notes · {target.stat().st_size / 1024:.0f} KB")
    try:
        written = CA.write(cfg, docs)
        if written:
            print(f"  catalog        {written}")
    except CA.CatalogError as e:
        print(f"  catalog: {e}")
    try:
        written = OL.write(cfg)
        if written:
            print(f"  {written[0].name:<14} {written[1]} operations" + ("" if written[2] else " (unchanged)"))
    except ValueError as e:
        print(f"  log: {e}")
    off = [k for k, on in mods.items() if not on]
    if off:
        print(f"  (off: {', '.join(off)})")
    print(f"\n  done in {(dt.datetime.now() - started).total_seconds():.1f}s"
          + (f"  ·  {target}" if target else ""))


def cmd_review(args, cfg):
    """Without PATH: the queue of notes to check against their source. With PATH and
    --verdict: record the check (frontmatter only, with a backup)."""
    if args.path and args.flag:
        try:
            result = RV.flag(cfg, args.path, args.flag)
        except NT.NoteError as e:
            print(f"vault-memory review: {e.message}", file=sys.stderr)
            sys.exit(e.code)
        print(json.dumps(result, ensure_ascii=False, indent=1) if args.json
              else f"  flagged  {result['p']}  (first in the queue until it is checked at the source)")
        return
    if args.path:
        if not args.verdict:
            sys.exit("vault-memory review: recording a check needs --verdict confirmed|outdated|unsure")
        try:
            result = RV.record(cfg, args.path, args.verdict, args.evidence, args.confidence, args.volatility)
        except NT.NoteError as e:
            if args.json:
                print(json.dumps({"error": e.message}, ensure_ascii=False))
            else:
                print(f"vault-memory review: {e.message}", file=sys.stderr)
            sys.exit(e.code)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=1))
        else:
            print(f"  {result['verdict']}  {result['p']}"
                  + (f"  ({result['confidence']}/10)" if result.get("confidence") else ""))
        return
    if args.calibration:
        _print_calibration(CB.report(cfg), args.json)
        return
    result = RV.queue(cfg, args.n, cap=args.budget or None, use_budget=args.budget is not None)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return
    cov, plan = result["coverage"], result["budget"]
    print(f"\n  reviewable notes {cov['notes']} · verified and current {cov['verified_current']} · "
          f"outdated {cov['outdated']} · unsure {cov['unsure']} · due {cov['due']}")
    print(f"  budget today {plan['n']} (base {plan['base']} + {plan['high']} high priority, "
          f"max {plan['max']}) · models: verifier {result['models']['verifier']}, "
          f"skeptic {result['models']['skeptic']}\n")
    if not result["queue"]:
        print("  nothing due for review")
        return
    for i, row in enumerate(result["queue"], 1):
        print(f"  {i:>2}. [{row['score']:>4}] {row['p']}")
        print(f"      {'; '.join(row['reasons'])}")


def cmd_inbox(args, cfg):
    """Learned candidates waiting in the inbox: list them, accept one (note + confirmed review)
    or reject one (logged as an outdated learned claim). Both count as audits."""
    import inbox as IB
    try:
        if args.accept:
            result = IB.accept(cfg, args.accept, args.evidence, args.confidence, args.volatility)
        elif args.reject:
            result = IB.reject(cfg, args.reject, args.reason)
        else:
            result = IB.listing(cfg)
    except NT.NoteError as e:
        if args.json:
            print(json.dumps({"error": e.message, **e.details}, ensure_ascii=False, indent=1))
        else:
            print(f"vault-memory inbox: {e.message}", file=sys.stderr)
            for dup in e.details.get("duplicates", []):
                print(f"  {dup['score']:.2f}  {dup['p']}   ({dup['reason']})", file=sys.stderr)
        sys.exit(e.code)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    elif args.accept and result.get("kind") == "update":
        print(f"  accepted  {result['id']}  →  updated {result['p']}  (backup in {result['backup']}; "
              f"confirmed {result['confidence']}/10)")
    elif args.accept:
        print(f"  accepted  {result['id']}  →  {result['p']}  (confirmed {result['confidence']}/10)")
    elif args.reject:
        print(f"  rejected  {result['id']}  (logged as an outdated learned claim)")
    elif not result["candidates"]:
        print("  the inbox is empty: nothing learned is waiting for review")
    else:
        print(f"\n  inbox · {result['count']} learned candidates waiting\n")
        for row in result["candidates"]:
            kind = f"update → {row['target']}" if row.get("kind") == "update" else row["type"]
            print(f"  {row['id']}  {row['day']}  {row['origin'] or row['source']}  [{kind}]  {row['title']}")
            if row["quote"]:
                print(f"            > {row['quote']}")
            for line in (row.get("diff") or "").splitlines()[2:]:
                print(f"            {line}")
            if row.get("stale"):
                print("            (the note changed since this was proposed: accepting it fails; reject it)")
        print("\n  accept:  vault-memory inbox --accept ID --evidence \"<source>: <what it shows>\" [--confidence 8-10]"
              "\n  reject:  vault-memory inbox --reject ID --reason \"<why it does not hold>\"")


def _print_calibration(data, as_json):
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=1))
        return
    pct = lambda x: "—" if x is None else f"{x:.0%}"          # noqa: E731
    print(f"\n  calibration · {data['reviews']} reviews logged · a rate counts from "
          f"{data['min_samples']} rechecks on a later day\n")
    print("  confidence  rechecked  held  failed  unsure next  hold rate  95% interval   calibrated")
    for row in data["buckets"]:
        interval = f"[{pct(row['low'])}, {pct(row['high'])}]" if row["n"] else "—"
        calibrated = f"{row['calibrated']:.2f}" if row["enough"] else "too few"
        print(f"  {row['bucket']:<10}  {row['n']:<9}  {row['held']:<4}  {row['failed']:<6}  "
              f"{row['unsure_next']:<11}  {pct(row['rate']):<9}  {interval:<13}  {calibrated}")
    if data["same_day"]:
        print(f"  ({data['same_day']} same-day re-checks left out: corrections, not tests of time)")
    lr = data["learned"]
    print(f"\n  learned notes  audited {lr['audited']} · confirmed {lr['confirmed']} · outdated "
          f"{lr['outdated']} · unsettled {lr['unsettled']} · precision {pct(lr['precision'])}"
          + (f" [{pct(lr['low'])}, {pct(lr['high'])}]" if lr["audited"] else ""))
    auto = data["autonomy"]
    print(f"  learned notes → {'notes' if auto['auto'] else 'inbox'} ({auto['reason']})")


def _print_compare(comp):
    if comp is None:
        return
    if not comp["available"]:
        print(f"\n  compare    cannot compare: {comp['reason']}")
        return
    bm25, rerank = comp["bm25"], comp["rerank"]
    print(f"\n  compare    {comp['cases']} cases, the same ones with each mode")
    print(f"    plain BM25        MRR {bm25['mrr']:.3f}  hit@1 {bm25['hit1']:.0%}  hit@3 {bm25['hit3']:.0%}")
    print(f"    semantic rerank   MRR {rerank['mrr']:.3f}  hit@1 {rerank['hit1']:.0%}  hit@3 {rerank['hit3']:.0%}")
    print(f"    paired gain       {comp['gain']:+.3f}  95% interval [{comp['gain_ci'][0]:+.3f}, {comp['gain_ci'][1]:+.3f}]"
          + ("  → clearly better" if comp["gain_ci"][0] > 0 else
             "  → clearly worse" if comp["gain_ci"][1] < 0 else "  → not clear"))
    for kind, row in comp["by_kind"].items():
        print(f"    {kind:<17} n={row['cases']:<4} MRR {row['bm25']:.3f} → {row['rerank']:.3f}  "
              f"gain {row['gain']:+.3f} [{row['gain_ci'][0]:+.3f}, {row['gain_ci'][1]:+.3f}]")


def cmd_bench(args, cfg):
    """Recall measured on this vault's own history; --tune tries parameters, --apply keeps the
    best only when the gain is statistically clear."""
    result = BN.run(cfg, days=args.days, do_tune=args.tune or args.apply,
                    rerank=False if args.no_rerank else None, do_compare=args.compare)
    tuned = result.get("tune")
    if args.apply and tuned and tuned["clear"]:
        result["applied"] = str(BN.apply(cfg, tuned["best"]))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return
    low, high = result["mrr_ci"]
    print(f"\n  recall on your own history · last {result['days']} days\n")
    print(f"  cases      {result['cases']}  (mined from use {result['mined']} · written by hand {result['hand']})")
    print(f"  hit@1      {result['hit1']:.0%}    hit@3 {result['hit3']:.0%}")
    print(f"  MRR@10     {result['mrr']:.3f}   95% interval [{low:.3f}, {high:.3f}]   "
          f"({'BM25 + semantic rerank' if result['rerank'] else 'plain BM25'})")
    kinds = [f"{kind} n={m['cases']} MRR {m['mrr']:.2f} [{m['mrr_ci'][0]:.2f}, {m['mrr_ci'][1]:.2f}] "
             f"hit@3 {m['hit3']:.0%}" for kind, m in result["by_kind"].items()]
    if kinds:
        print("  by kind    " + " · ".join(kinds))
    _print_compare(result.get("compare"))
    week, cov = result["week"], result["verified"]
    print(f"  this week  {week['pointers_read']} of {week['pointers']} suggestions used ({week['pct_read']}%)")
    print(f"  verified   {cov['verified_current']} of {cov['notes']} reviewable notes current · {cov['due']} due")
    if result["misses"]:
        print("\n  not found in the top 10:")
        for query in result["misses"][:6]:
            print(f"    {' '.join(query.split())}")
    if tuned:
        best, cur = tuned["best"], tuned["current"]
        print(f"\n  tune ({tuned['tried']} variants): current MRR {cur['mrr']:.3f} → best {best['mrr']:.3f} "
              f"with k1 {best['k1']} · b {best['b']} · title_bonus {best['title_bonus']} · prior_weight {best['prior_weight']}")
        print(f"  gain on held-out cases {tuned['held_out_gain']:+.3f}, 95% interval "
              f"[{tuned['gain_ci'][0]:+.3f}, {tuned['gain_ci'][1]:+.3f}] → "
              + ("clear: worth applying" if tuned["clear"] else
                 f"not clear (needs a gain above 0 and {BN.MIN_CASES}+ cases): keep the current values"))
        if result.get("applied"):
            print(f"  applied to {result['applied']}")


def cmd_bases(args, cfg):
    """Standard .base views: which ones the vault lacks; --install copies them (never overwrites)."""
    vault = cfg["_vault"]
    present = {p.name for p in vault.rglob("*.base") if ".obsidian" not in p.parts and "_skill" not in p.parts}
    source = ROOT / "standard" / "bases"
    missing = [p for p in sorted(source.glob("*.base")) if p.name not in present]
    if args.names:
        wanted = {n if n.endswith(".base") else n + ".base" for n in args.names}
        missing = [p for p in missing if p.name in wanted]
    if args.folder:
        target = vault / args.folder.strip("/")
    else:
        target = vault / "00 Index" if (vault / "00 Index").is_dir() else vault
    if not missing:
        print("  every standard view is already in the vault")
        return
    for path in missing:
        first = path.read_text(encoding="utf-8").splitlines()[0].lstrip("# ")
        print(f"  {'+' if args.install else '-'} {path.name:<15} {first}")
    if not args.install:
        print(f"\n  To add them:  vault-memory bases [NAME…] --install [--folder F]   (target: {target.relative_to(vault) or '.'})\n"
              "  A view you already have under another name (a translated copy) does not need it again.")
        return
    if target.resolve() != vault.resolve() and vault.resolve() not in target.resolve().parents:
        sys.exit("vault-memory bases: --folder must be inside the vault")
    target.mkdir(parents=True, exist_ok=True)
    for path in missing:
        shutil.copy2(path, target / path.name)
    print(f"\n  installed {len(missing)} in {target.relative_to(vault) or '.'}: open them in Obsidian")


def cmd_links(args, cfg):
    """Read only: pairs of notes that talk about the same thing and neither links to the other."""
    pairs = LK.suggest(cfg, args.n)
    if args.json:
        print(json.dumps(pairs, ensure_ascii=False, indent=1))
        return
    if not pairs:
        print("  no missing links among similar notes")
        return
    for pair in pairs:
        print(f"  {pair['score']:5.1f}  [[{pair['a_n']}]]  ↔  [[{pair['b_n']}]]")
    print("\n  Add the link where it helps the reader; nothing was changed.")


def cmd_today(args, cfg):
    """The daily summary: next, waiting, due soon (config `today`)."""
    if not TD.enabled(cfg):
        print("  today is off: set `today.type` (and its fields) in config.json")
        return
    if args.json:
        print(json.dumps(TD.collect(cfg), ensure_ascii=False, indent=1, default=str))
        return
    print(TD.render(cfg, all_items=args.all))


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


# ──────────────────────────────────────────────────────────────────── move
def cmd_move(args, cfg):
    """Move and rename files and folders by a plan (old<TAB>new), rewriting links, config and
    the state saved by path. Dry run unless --apply."""
    import move as MV
    try:
        text = sys.stdin.read() if args.plan == "-" else Path(args.plan).expanduser().read_text(encoding="utf-8")
        result = MV.run(cfg, text, apply=args.apply)
    except MV.MoveError as e:
        if args.json:
            print(json.dumps({"error": e.message, "errors": e.errors}, ensure_ascii=False, indent=1))
        else:
            print(f"vault-memory move: {e.message}", file=sys.stderr)
            for error in e.errors:
                print(f"  {error}", file=sys.stderr)
        sys.exit(e.code)
    except (OSError, UnicodeDecodeError) as e:
        print(f"vault-memory move: {e}", file=sys.stderr)
        sys.exit(2)
    if result["applied"]:
        # what derives from paths is rebuilt, not rewritten: index, graph, related, patterns, panel
        cfg = C.load_config(cfg["_vault"])
        docs, link_graph, coverage = IX.build(cfg, quiet=True)
        patterns = PT.build(cfg, docs, link_graph, coverage)
        RC.write_related(cfg, json.loads((cfg["_state"] / "index.json").read_text(encoding="utf-8")))
        if C.enabled(cfg, "panel"):
            PN.build(cfg, docs, link_graph, coverage, patterns)
        for name, regenerate in (("catalog", lambda: CA.write(cfg, docs)), ("log", lambda: OL.write(cfg))):
            try:                                   # generated pages name the new paths too
                regenerate()
            except (CA.CatalogError, ValueError) as e:
                print(f"  {name}: {e}", file=sys.stderr)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return
    counts = result["counts"]
    print(f"\n  move · {cfg['_vault'].name} · {'applied' if result['applied'] else 'dry run'}\n")
    print(f"  files     {counts['files']} moved ({counts['notes']} notes, {counts['other']} other files)")
    for row in result["files"]:
        print(f"    {row['from']}  →  {row['to']}")
    print(f"  links     {counts['links']} rewritten in {counts['notes_rewritten']} notes")
    for row in result["links"]:
        print(f"    {row['p']}: {row['from']} → {row['to']}")
    print(f"  config    {counts['config']} keys")
    for row in result["config"]:
        print(f"    {row['key']}: {row['from']} → {row['to']}")
    print(f"  state     {counts['state']} lines"
          + (" (" + " · ".join(f"{s['file']} {s['lines']}" for s in result["state"]) + ")" if result["state"] else ""))
    if result["warnings"]:
        print(f"  warnings  {len(result['warnings'])}")
        for warning in result["warnings"]:
            print(f"    {warning}")
    if result["applied"]:
        print(f"\n  moved · backup in {C.STATE}/backups/{Path(result['backup']).name}/ · "
              "index, graph, related notes, patterns and panel rebuilt")
    else:
        print(f"\n  Dry run. To apply (everything it touches is backed up first):  vault-memory move {args.plan} --apply")


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


SHIM_CMD = """@echo off
rem vault-memory CLI shim (written by `vault-memory install-cli`).
rem Runs the installed plugin copy. Plugin updates move that copy to a new folder, so it is
rem looked up on every call: VAULT_MEMORY_HOME, then the active Claude Code profile, then the
rem copy this shim was written from.
setlocal
set "VM_ROOT=%VAULT_MEMORY_HOME%"
if not defined VM_ROOT for /f "usebackq delims=" %%i in (`"__PY__" "%~dp0vault-memory-locate.py"`) do set "VM_ROOT=%%i"
if not exist "%VM_ROOT%\\scripts\\cli.py" set "VM_ROOT=__ROOT__"
"__PY__" "%VM_ROOT%\\scripts\\cli.py" %*
exit /b %ERRORLEVEL%
"""

SHIM_LOCATE = """import json, os, pathlib
profile = pathlib.Path(os.environ.get("CLAUDE_CONFIG_DIR") or (pathlib.Path.home() / ".claude"))
try:
    data = json.loads((profile / "plugins" / "installed_plugins.json").read_text(encoding="utf-8"))
    print(data["plugins"]["vault-memory@vault-memory"][0]["installPath"])
except Exception:
    found = sorted(profile.glob("plugins/cache/*/vault-memory/*/scripts/cli.py"))
    print(found[-1].parent.parent if found else "")
"""


def _default_cli_dir():
    return str(SC.shim_path(SC.system()).parent)


def cmd_install_cli(args):
    folder = Path(args.dir).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        target = folder / "vault-memory.cmd"
        with open(target, "w", encoding="utf-8", newline="\r\n") as handle:  # Path.write_text has no newline= before 3.10
            handle.write(SHIM_CMD.replace("__PY__", sys.executable).replace("__ROOT__", str(ROOT)))
        (folder / "vault-memory-locate.py").write_text(SHIM_LOCATE, encoding="utf-8")
    else:
        target = folder / "vault-memory"
        target.write_text(SHIM.replace("__ROOT__", str(ROOT)), encoding="utf-8")
        target.chmod(0o755)
    print(f"  written {target}")
    if str(folder) not in os.environ.get("PATH", "").split(os.pathsep):
        if os.name == "nt":
            print(f"  {folder} is not on PATH. To add it for your user (then open a new terminal):")
            print('    [Environment]::SetEnvironmentVariable("Path", '
                  f'[Environment]::GetEnvironmentVariable("Path", "User") + ";{folder}", "User")')
            print("  (PowerShell; or Settings > System > About > Advanced system settings > Environment Variables)")
        else:
            print(f"  {folder} is not on PATH: add it to your shell profile")


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
    p.add_argument("--no-rerank", dest="no_rerank", action="store_true",
                   help="plain BM25, even when the semantic rerank is on")
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
    p.add_argument("--no-distill", dest="no_distill", action="store_true", help="skip learning from sessions")
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
    p = sub.add_parser("answer", help="keep an answer that cites several notes as a new note (unverified, reviewed later)")
    p.add_argument("--question", required=True, help="what was asked (default title)")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--body", help="the answer")
    group.add_argument("--body-file", dest="body_file")
    group.add_argument("--stdin", action="store_true", help="read the answer from stdin")
    p.add_argument("--cites", required=True, help="paths of the cited notes, comma-separated (field p of recall)")
    p.add_argument("--title", help="note title (default: the question)")
    p.add_argument("--dry-run", dest="dry_run", action="store_true", help="show what would be written")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_answer)

    p = sub.add_parser("usage", help="memory usage metrics (local telemetry)")
    p.add_argument("--days", type=int, default=14)
    p.add_argument("--json", action="store_true")
    p.add_argument("--suggest-eval", dest="suggest_eval", action="store_true")
    p.add_argument("--tokens", action="store_true", help="bytes/tokens vault-memory adds to the context (from transcripts)")
    p.add_argument("--transcripts", help="transcripts folder (default $CLAUDE_CONFIG_DIR/projects)")
    p.set_defaults(fn=cmd_usage)
    p = sub.add_parser("distill", help="learn from yesterday's agent sessions (config `distill`)")
    p.add_argument("--day", help="YYYY-MM-DD (default: yesterday plus any missed days)")
    p.add_argument("--force", action="store_true", help="learn again from a day already processed")
    p.add_argument("--dry-run", dest="dry_run", action="store_true", help="call the model, write nothing")
    p.add_argument("--sources", help="only these sources this run, e.g. codex,gemini (default `distill.sources`)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_distill)
    p = sub.add_parser("ingest", help="learn from material an agent collected (thread, issue, PR): JSON or Markdown")
    p.add_argument("--source", help="where it comes from, lowercase (e.g. chat, issues, prs); required for Markdown "
                        "(with --capture: default capture)")
    material = p.add_mutually_exclusive_group(required=True)
    material.add_argument("--file", help="path to a .json or .md file, or - for stdin")
    material.add_argument("--capture", metavar="PATH",
                          help="a capture in the captures folder (vault-memory capture): its candidates cite it")
    p.add_argument("--url", help="link to the original (stored with the candidate)")
    p.add_argument("--id", help="id in the original system (default: a hash of the content)")
    p.add_argument("--date", help="YYYY-MM-DD the material is from (default: the JSON date or today)")
    p.add_argument("--dry-run", dest="dry_run", action="store_true", help="call the model, write nothing")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_ingest)
    p = sub.add_parser("capture", help="keep raw material exactly as it arrived (redacted), for notes to cite")
    p.add_argument("file", metavar="FILE", help="path to the material, or - for stdin")
    p.add_argument("--title", help="default: the first heading or line of the text")
    p.add_argument("--url", help="link to the original")
    p.add_argument("--date", help="YYYY-MM-DD it was captured (default: today)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_capture)
    p = sub.add_parser("today", help="daily summary: what is next, waiting and due soon")
    p.add_argument("--all", action="store_true", help="every open item, not only the top ones")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_today)
    p = sub.add_parser("bases", help="standard Obsidian views (.base) missing in the vault")
    p.add_argument("names", nargs="*", help="only these views (e.g. Memory)")
    p.add_argument("--install", action="store_true", help="copy the missing ones (never overwrites)")
    p.add_argument("--folder", help="folder inside the vault (default: 00 Index or the root)")
    p.set_defaults(fn=cmd_bases)
    p = sub.add_parser("links", help="similar notes that do not link to each other (read only)")
    p.add_argument("--n", type=int, default=20)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_links)
    p = sub.add_parser("bench", help="does recall find what gets used? measured on your own history")
    p.add_argument("--days", type=int, default=BN.DAYS, help="history window (default 120)")
    p.add_argument("--tune", action="store_true", help="try recall parameters against the cases")
    p.add_argument("--apply", action="store_true", help="tune and write the best only if the gain is clear")
    p.add_argument("--compare", action="store_true",
                   help="the same cases with plain BM25 and with the semantic rerank, paired difference")
    p.add_argument("--no-rerank", dest="no_rerank", action="store_true",
                   help="main numbers with plain BM25 even when the rerank is available")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_bench)
    p = sub.add_parser("embed", help="vectors for the semantic rerank of recall (config `embeddings`)")
    p.add_argument("--full", action="store_true", help="embed every note again")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_embed)
    p = sub.add_parser("review", help="notes to check against their source, and record a check")
    p.add_argument("path", nargs="?", help="note to record a check for (relative to the vault)")
    p.add_argument("--verdict", choices=RV.VERDICTS)
    p.add_argument("--confidence", type=int, help="1-10: how much primary evidence backs it (confirmed needs 8+)")
    p.add_argument("--evidence", help="what was checked and where (required when recording)")
    p.add_argument("--volatility", choices=["live", "stable"], help="how fast the fact changes")
    p.add_argument("--flag", metavar="REASON",
                   help="mark PATH as disputed in a conversation (the note is not edited); "
                        "it goes first in the queue until a review settles it")
    p.add_argument("--n", type=int, help="queue length (default `review.n`, 20)")
    p.add_argument("--budget", type=int, nargs="?", const=0, metavar="MAX",
                   help="only today's budget: review.daily.base plus one per high-priority note, "
                        "up to MAX (default review.daily.max)")
    p.add_argument("--calibration", action="store_true",
                   help="does confidence hold up at the next review? and learned-note precision")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_review)
    p = sub.add_parser("inbox", help="learned candidates waiting for review: list, accept or reject (each counts as an audit)")
    decision = p.add_mutually_exclusive_group()
    decision.add_argument("--accept", metavar="ID", help="write the candidate as a note and record it as confirmed")
    decision.add_argument("--reject", metavar="ID", help="write no note; log the learned claim as outdated")
    p.add_argument("--evidence", help="with --accept: the source checked and what it shows (required)")
    p.add_argument("--confidence", type=int, help="with --accept: 8-10 (default `review.min_confirm`, 8)")
    p.add_argument("--volatility", choices=["live", "stable"], help="with --accept: how fast the fact changes")
    p.add_argument("--reason", help="with --reject: why the learned claim does not hold (required)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_inbox)
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
    p = sub.add_parser("move", help="move or rename files and folders by a plan, rewriting links, config and state")
    p.add_argument("plan", metavar="PLAN.tsv",
                   help="one line per file or folder: old<TAB>new, relative to the vault (- for stdin)")
    p.add_argument("--apply", action="store_true", help="move (otherwise dry run); everything touched is backed up")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_move)
    p = sub.add_parser("install-cli", help="write the `vault-memory` command to a folder on PATH")
    p.add_argument("--dir", default=_default_cli_dir())
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
