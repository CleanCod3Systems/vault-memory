#!/usr/bin/env python3
"""Single scan of the vault → three derived files in the state folder:

    index.json     recall: frontmatter + terms + references of each note
    graph.json     links, backlinks, orphans, broken wikilinks, gaps
    contract.json  frontmatter contract coverage per type

Deterministic: no network, no model. ~0.5 s over 200 notes.
It only reads notes and only writes state files. What writes into notes is `vault-memory fix`.
"""
import datetime as dt
import json
import re
from collections import defaultdict

import core as C
import freshness as FR

RE_WIKI = re.compile(r"\[\[([^\]|#\n]+)")
RE_CODE = re.compile(r"```.*?```|`[^`\n]*`", re.S)
RE_HEADING = re.compile(r"^(#{1,4})\s+(.+?)\s*$", re.M)
SENSITIVE = re.compile(r"(^|/)(\.env|\.credentials|.*\.pem|.*\.key)($|/)", re.I)


def _ticket_regex(cfg):
    prefixes = [p for p in cfg["references"]["tickets"].get("prefixes", []) if p]
    return re.compile(r"\b(" + "|".join(map(re.escape, prefixes)) + r")-\d{1,6}\b") if prefixes else None


def _chat_regex(cfg):
    domain = cfg["references"]["chat"].get("domain", "").strip()
    return re.compile(r"https://" + re.escape(domain) + r"/\S+") if domain else None


def _id_regex(cfg):
    words = [w for w in cfg["references"]["ids"].get("words", []) if w]
    if not words:
        return None
    return re.compile(r"\b(?:" + "|".join(map(re.escape, words))
                      + r")s?\s*(?:n[°ºo]\.?\s*|#\s*)?(\d{3,10})", re.I)


def title_of(body, name):
    match = re.search(r"^#\s+(.+?)\s*$", body, re.M)
    return (match.group(1) if match else name).strip()


def snippet(body, width=260):
    for line in body.splitlines():
        text = line.strip()
        if not text or text.startswith(("#", ">", "---", "|", "- [", "```", "![")):
            continue
        text = re.sub(r"\[\[([^\]|]+)\|?[^\]]*\]\]", r"\1", text)
        text = re.sub(r"[*`_]", "", text)
        if len(text) > 40:
            return text[:width]
    return ""


# ───────────────────────────────────────────────────────────────────── scan
def fingerprint(path):
    """(mtime, size): if neither changed, the note is not read again."""
    st = path.stat()
    return round(st.st_mtime, 3), st.st_size


def scan(cfg, previous=None):
    """Walk the vault and build one doc per note. With `previous` (docs of an earlier index
    with the same tokenization and config) unchanged notes are reused: the recall of each
    prompt pays one stat per note, not one read."""
    archive = set(C.ARCHIVE)
    mods = C.modules(cfg)
    re_tickets = _ticket_regex(cfg) if mods["tickets"] else None
    re_chat = _chat_regex(cfg) if mods["chat"] else None
    re_ids = _id_regex(cfg) if mods["ids"] else None
    max_terms = int(cfg["recall"].get("max_terms", 800))
    old_docs = {d["p"]: d for d in (previous or []) if "mte" in d}
    docs, name_to_paths = [], defaultdict(list)
    known_families = C.families(cfg)
    weights = cfg["recall"]

    for path, rel in C.walk(cfg):
        if SENSITIVE.search(rel):
            continue
        try:
            mtime, size = fingerprint(path)
        except OSError:
            continue
        old = old_docs.get(rel)
        if old and old["mte"] == mtime and old["size"] == size:
            docs.append(old)
            name_to_paths[old["n"]].append(rel)
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        fm, body = C.frontmatter(text)
        tags, name = C.tag_list(fm), path.stem
        note_type = C.type_of(rel, name, tags, fm, cfg)
        struct_type = C.structural_type(rel, name, tags, cfg)
        # A template declares the type it GENERATES (`type: incident`), so it cannot be
        # detected by `type`: it is detected by where it lives. It is flagged so it does not
        # compete in recall with real notes — its body is {{title}} and sample words.
        is_template = (struct_type == "template"
                       or str(fm.get("template", "")).lower() in ("true", "yes", "si", "sí"))

        term_freq = defaultdict(int)
        # name and title weigh ×3: it is what one remembers of a note
        for term in C.tokens(name + " " + title_of(body, name)):
            term_freq[term] += 3
        for term in C.tokens(body):
            term_freq[term] += 1
        # What describes the note even if it is not in its text: tags, section titles and the
        # folders of its path ("01-strategy", "Incidents").
        for term in C.tokens(" ".join(tags).replace("-", " ")):
            term_freq[term] += int(weights.get("tag_weight", 3))
        for _, heading in RE_HEADING.findall(body):
            for term in C.tokens(heading):
                term_freq[term] += int(weights.get("h2_weight", 2))
        for term in C.tokens(" ".join(rel.split("/")[:-1]).replace("-", " ")):
            term_freq[term] += int(weights.get("path_weight", 1))
        # Total order (frequency, term) so the cut is the same on every run.
        term_freq = dict(sorted(term_freq.items(), key=lambda x: (-x[1], x[0]))[:max_terms])

        raw_ids = set(re_ids.findall(text)) if re_ids else set()
        raw_ids |= set(re.findall(r"\d{3,10}", fm.get("ids", "")))
        ids = sorted(x for x in raw_ids if not re.fullmatch(r"(19|20)\d{2}", x))  # years are not IDs

        date = (fm.get("date", "") or fm.get("since", "")).strip()
        if not date:
            match = C.RE_DATE.search(name)
            date = match.group(1) if match else (C.date_from_path(rel, name) or "")

        replaces = fm.get("replaces", "")
        docs.append({
            "p": rel, "n": name, "t": title_of(body, name),
            "type": note_type, "struct_type": struct_type,
            "family": known_families.get(note_type, "structure"),
            "area": (fm.get("area", "") or C.area_of(rel, cfg)).strip(),
            "tags": tags, "date": date,
            "status": fm.get("status", "").strip(),
            "sev": fm.get("severity", "").strip(),
            "system": fm.get("system", "").strip(),
            "class": fm.get("class", "").strip(),
            "archived": bool(set(rel.split("/")[:-1]) & archive),
            "template": is_template,
            "mt": dt.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d"), "mte": mtime, "size": size,
            "words": sum(1 for _ in C.TOK.finditer(C.norm(body))),
            "h2": [h for level, h in RE_HEADING.findall(body) if len(level) == 2][:14],
            "refs": {
                # rstrip("\\"): inside a table the alias pipe is escaped ([[Note\\|Alias]]),
                # and the regex cuts at "|" but not at "\\|". Code is removed: `[[note name]]`
                # in a note that explains Obsidian is a syntax example, not a link.
                "wiki": sorted({w.strip().rstrip("\\") for w in RE_WIKI.findall(RE_CODE.sub("", text))}),
                "tickets": sorted({m.group(0) for m in re_tickets.finditer(text)}) if re_tickets else [],
                "chat": sorted(set(re_chat.findall(text)))[:5] if re_chat else [],
                "rel": [x.strip() for x in fm.get("related", "").split(",") if x.strip()],
            },
            "ids": ids[:20], "s": snippet(body), "fm": sorted(fm.keys()), "tf": term_freq,
            # Freshness: all optional. `replaces` is resolved to paths in freshness.annotate().
            "verified": fm.get("verified", "").strip()[:10], "source": fm.get("source", "").strip()[:200],
            "expires": fm.get("expires", "").strip()[:10], "key": fm.get("key", "").strip(),
            "replaces": [w.strip() for w in RE_WIKI.findall(replaces)]
                        or [x.strip() for x in replaces.split(",") if x.strip()],
        })
        name_to_paths[name].append(rel)
    return docs, name_to_paths


# ──────────────────────────────────────────────────────────────────── graph
def common_prefix(a, b):
    """How many path components two folders share, from the root."""
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def resolve_link(link, name_to_paths, origin):
    """Paths a wikilink points to, resolved like Obsidian: by name, an explicit path breaks
    ties first, then proximity (shortest unique path)."""
    target = link.strip().strip("[]")
    candidates = name_to_paths.get(target) or name_to_paths.get(target.split("/")[-1]) or []
    if len(candidates) > 1 and "/" in target:
        suffix = "/" + target.lstrip("./").removesuffix(".md") + ".md"
        candidates = [x for x in candidates if ("/" + x).endswith(suffix)] or candidates
    if len(candidates) > 1:
        folder = origin.split("/")[:-1]
        candidates = [max(candidates, key=lambda x: common_prefix(folder, x.split("/")[:-1]))]
    return candidates


def graph(docs, name_to_paths, cfg):
    # An attachment embed is not a broken link: ![[Pasted image X.png]], [[Incidents.base]]
    # and [[Diagram.html]] point to files that exist, they are just not .md notes.
    other_files = set()
    for path, _ in C.walk(cfg, md_only=False, prune_excluded=False):
        if path.suffix != ".md":
            other_files.add(path.name)
            other_files.add(path.stem)

    outgoing, incoming, broken = defaultdict(set), defaultdict(set), []
    for d in docs:
        # A template links {{date}} and "Note name": they are placeholders, not targets.
        if d.get("template"):
            continue
        for link in d["refs"]["wiki"] + d["refs"]["rel"]:
            link = link.strip().strip("[]")
            targets = resolve_link(link, name_to_paths, d["p"])
            if not targets:
                if link not in other_files and link.split("/")[-1] not in other_files:
                    broken.append({"from": d["p"], "link": link})
                continue
            for target in targets:
                if target != d["p"]:
                    outgoing[d["p"]].add(target)
                    incoming[target].add(d["p"])

    diagnostics = C.enabled(cfg, "graph")
    # A repeated name is only a problem if someone links it by name: six README.md in six
    # folders is normal structure, not a defect to report.
    referenced = {w.strip().strip("[]") for d in docs if not d.get("template")
                  for w in d["refs"]["wiki"] + d["refs"]["rel"]} if diagnostics else set()
    duplicates = {n: p for n, p in name_to_paths.items() if len(p) > 1 and n in referenced}
    types = C.record_types(cfg)
    records = [d for d in docs if d["type"] in types and not d["template"]]
    gaps = {
        "record_without_ticket": sorted(d["p"] for d in records if not d["refs"]["tickets"]),
        "record_without_source": sorted(d["p"] for d in records if not d["refs"]["chat"]),
        "runbooks_without_backlink": sorted(d["p"] for d in docs if d["type"] == "runbook"
                                            and not d["template"] and not incoming[d["p"]]),
    }
    if not diagnostics or not C.enabled(cfg, "tickets") or not cfg["references"]["tickets"].get("prefixes"):
        gaps.pop("record_without_ticket", None)
    if not diagnostics or not C.enabled(cfg, "chat") or not cfg["references"]["chat"].get("domain"):
        gaps.pop("record_without_source", None)
    if not diagnostics:
        gaps.pop("runbooks_without_backlink", None)

    return {
        "generated": dt.datetime.now().isoformat(timespec="seconds"),
        "notes": len(docs),
        "edges": sum(len(v) for v in outgoing.values()),
        "diagnostics": diagnostics,
        "orphans": sorted(d["p"] for d in docs
                          if not outgoing[d["p"]] and not incoming[d["p"]]
                          and not d["archived"] and not d["template"]) if diagnostics else [],
        "broken": broken if diagnostics else [],
        "duplicates": {n: p for n, p in sorted(duplicates.items())},
        "hubs": ([{"note": p, "backlinks": len(v)}
                  for p, v in sorted(incoming.items(), key=lambda x: -len(x[1]))[:12]]
                 if diagnostics else []),
        "gaps": {k: {"n": len(v), "items": v[:40]} for k, v in gaps.items()},
        "backlinks": {p: sorted(v) for p, v in incoming.items() if v},
        "outgoing": {p: sorted(v) for p, v in outgoing.items() if v},
    }


# ───────────────────────────────────────────────────────────────── contract
def contract(docs, cfg):
    if not C.enabled(cfg, "contract"):
        return {"generated": dt.datetime.now().isoformat(timespec="seconds"),
                "disabled": True, "notes": 0, "conforming": 0, "coverage": None,
                "by_type": {}, "unknown_types": {}, "missing_fields": []}
    by_type = defaultdict(lambda: {"n": 0, "ok": 0, "missing": defaultdict(int)})
    missing_fields, unknown = [], defaultdict(list)
    known_families = C.families(cfg)
    for d in docs:
        if d["archived"] or d["template"]:
            continue
        if d["type"] not in known_families:
            unknown[d["type"]].append(d["p"])
        missing = [k for k in C.required_fields(d["type"], cfg) if k not in set(d["fm"])]
        entry = by_type[d["type"]]
        entry["n"] += 1
        if not missing:
            entry["ok"] += 1
        else:
            for k in missing:
                entry["missing"][k] += 1
            missing_fields.append({"p": d["p"], "type": d["type"], "missing": missing})
    total = sum(v["n"] for v in by_type.values())
    ok = sum(v["ok"] for v in by_type.values())
    return {
        "generated": dt.datetime.now().isoformat(timespec="seconds"),
        "notes": total, "conforming": ok,
        "coverage": round(ok / total * 100, 1) if total else 0.0,
        "by_type": {k: {"n": v["n"], "ok": v["ok"], "missing": dict(v["missing"])}
                    for k, v in sorted(by_type.items())},
        "unknown_types": {k: v[:10] for k, v in unknown.items()},
        "missing_fields": missing_fields,
    }


def reusable_previous(cfg):
    """Docs of the existing index if it can be built upon (same tokenization and config);
    otherwise None and everything is rebuilt."""
    try:
        stored = json.loads((cfg["_state"] / "index.json").read_text(encoding="utf-8"))
    except Exception:
        return None
    if stored.get("tok") != C.TOK_VER or stored.get("cfg") != C.cfg_hash(cfg):
        return None
    return stored.get("docs")


def build(cfg, quiet=False, write=True, incremental=False):
    """Scan and derive index, graph and contract. `write=False` is the read-only variant
    (doctor, fix without --apply, eval): computes everything and leaves the state untouched."""
    docs, name_to_paths = scan(cfg, reusable_previous(cfg) if incremental else None)
    FR.annotate(docs, name_to_paths)
    link_graph = graph(docs, name_to_paths, cfg)
    coverage = contract(docs, cfg)
    if not write:
        return docs, link_graph, coverage
    state = cfg["_state"]
    state.mkdir(parents=True, exist_ok=True)
    (state / "index.json").write_text(json.dumps(
        {"generated": dt.datetime.now().isoformat(timespec="seconds"),
         "vault": cfg["_vault"].name, "tok": C.TOK_VER, "cfg": C.cfg_hash(cfg),
         "notes": len(docs), "docs": docs},
        ensure_ascii=False), encoding="utf-8")
    (state / "graph.json").write_text(json.dumps(link_graph, ensure_ascii=False, indent=1), encoding="utf-8")
    (state / "contract.json").write_text(json.dumps(coverage, ensure_ascii=False, indent=1), encoding="utf-8")
    if not quiet:
        size_kb = (state / "index.json").stat().st_size / 1024
        print(f"  index.json     {len(docs)} notes · {size_kb:.0f} KB")
        if link_graph.get("diagnostics"):
            print(f"  graph.json     {link_graph['edges']} links · {len(link_graph['orphans'])} orphans · "
                  f"{len(link_graph['broken'])} broken links")
        if not coverage.get("disabled"):
            print(f"  contract.json  coverage {coverage['coverage']}% ({coverage['conforming']}/{coverage['notes']})")
    return docs, link_graph, coverage
