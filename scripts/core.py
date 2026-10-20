#!/usr/bin/env python3
"""Shared core: find the vault, read its config, tokenize, parse frontmatter.

Everything vault-specific lives in <vault>/.vault-memory/config.json. This module knows
nothing about any particular vault: the defaults are the ones in STANDARD.md.
"""
import datetime as dt
import hashlib
import json
import os
import re
import unicodedata
from pathlib import Path

import legacy as LG

STATE = ".vault-memory"
LEGACY_STATE = ".cerebro"

# ─────────────────────────────────────────────────────── standard vocabulary
FAMILY = {
    "incident": "record", "meeting": "record", "decision": "record",
    "daily": "record", "period": "record",
    "doc": "knowledge", "runbook": "knowledge",
    "reference": "knowledge", "project": "knowledge",
    "home": "structure", "template": "structure", "note": "structure",
}

BASE = ["type", "area", "tags"]
SCHEMA = {
    "incident": BASE + ["date", "status", "severity", "system"],
    "meeting":  BASE + ["date"],
    "decision": BASE + ["date"],
    "daily":    BASE + ["date"],
    "period":   BASE + ["period"],
    "runbook":  BASE + ["status", "system"],
    "project":  BASE + ["project"],
}
# Placeholders that `fix` writes empty: a visible gap is worth more than a missing one.
EMPTY_FIELDS = {"incident": ["status", "severity", "system"], "runbook": ["status", "system"]}

EXCLUDE = [".obsidian", STATE, LEGACY_STATE, "_attachments", "_adjuntos", "node_modules", ".git",
           "__pycache__", ".trash", ".credentials"]
ARCHIVE = ["_archive", "Archive", "_archivo", "Archivo"]

# ─────────────────────────────────────────────────────────────────── modules
# Each module turns on and off on its own. `recall` is deliberately not in the list:
# it is the tool, not an option.
MODULES = {
    "contract":  ("Frontmatter contract", "validates and normalizes type/area/fields per type"),
    "graph":     ("Link diagnostics", "backlinks, orphans, broken wikilinks, duplicate names"),
    "patterns":  ("Pattern synthesis", "what repeats, what got stuck"),
    "panel":     ("HTML panel", "self-contained page that searches the vault from the browser"),
    "bases":     ("Bases views", "installs the standard .base files inside the vault"),
    "templates": ("Note templates", "installs the standard templates inside the vault"),
    "tickets":   ("Ticket references", "extracts ABC-123 keys and flags what has no ticket"),
    "chat":      ("Conversation references", "extracts team chat permalinks"),
    "ids":       ("ID references", "extracts order/case/record numbers for exact search"),
    "schedule":  ("Scheduled refresh", "runs `vault-memory refresh` at a fixed time (launchd/cron)"),
}

PROFILES = {
    "minimal":  {"contract": True, "graph": False, "patterns": False, "panel": False,
                 "bases": False, "templates": False,
                 "tickets": False, "chat": False, "ids": True, "schedule": False},
    "standard": {"contract": True, "graph": True, "patterns": True, "panel": True,
                 "bases": True, "templates": True,
                 "tickets": False, "chat": False, "ids": True, "schedule": False},
    "team":     {**{k: True for k in MODULES}, "schedule": False},
}
PROFILE_DESC = {
    "minimal":  "Memory only: index, recall and the standard contract. Nothing is written to the vault except missing frontmatter.",
    "standard": "Memory + link diagnostics, synthesis, HTML panel, Bases and templates. No external integrations.",
    "team":     "Everything, including references to tickets and chat conversations.",
}

CONFIG_DEF = {
    "modules": dict(PROFILES["standard"]),
    "vault": "",
    "areas": {},              # "Folder" -> "area"   (path prefix)
    "default_area": "general",
    "types_by_folder": {},    # "Folder" -> "type"
    "extra_types": {},        # "my-type" -> "record" | "knowledge" | "structure"
    "extra_schema": {},       # "type" -> ["field", ...]  (added to the standard one)
    "exclude": [],            # own folders to exclude, on top of the reserved ones
    "references": {
        "tickets": {"prefixes": [], "url": ""},
        "chat": {"domain": ""},
        "ids": {"words": ["id", "order", "ticket", "case", "record"]},
    },
    "taxonomy": "",           # relative path to a note with a table `id | ... | solution | ...`
    # Which note types enter the recurrence synthesis. Only types that have a notion of class:
    # meetings and dailies never have one and would show up as "unclassified".
    "patterns": {"types": ["incident"], "stale_days": 30},
    # Tokenizer language: "en" or "es" apply a light stemmer; "none" leaves words as they are.
    # `init` detects it from the vault. `irregular` adds domain-specific word forms.
    "language": {"stemmer": "en", "extra_stopwords": [], "irregular": {}},
    # One single definition of which status closes a note and which makes it obsolete.
    # Both languages by default: a vault can mix them.
    "statuses": {"closed": ["resolved", "closed", "done", "fixed", "resuelto", "cerrado", "hecho"],
                 "obsolete": ["obsolete", "deprecated", "superseded", "replaced",
                              "obsoleta", "obsoleto", "reemplazada", "reemplazado"]},
    "severities": {"high": ["p1", "critical", "high", "critico", "alta"]},
    # `skip_paths` is applied by the hook and `eval` (not by `recall`: a skill may ask for a
    # daily on purpose). `similar_skip_types`: notes that hide the story when used as neighbors.
    "recall": {"skip_paths": [], "similar_skip_types": ["daily", "period", "home", "index"],
               "max_terms": 800,
               # Weight in tf of what describes the note without being in its text: tags,
               # section titles and the folders of its path.
               "tag_weight": 3, "h2_weight": 2, "path_weight": 1,
               # Bonus for the fraction of the query that appears in the note's name/title.
               "title_bonus": 8},
    "hook": {},               # thresholds and regexes of the UserPromptSubmit hook (see hookfilter.py)
    # Freshness (docs/freshness.md): which types are reviewed, after how many days an
    # unverified note is reported, and how much a replaced/expired note weighs in recall.
    # `unverified` at 1.0 = only reported: penalizing every old note would move the ranking
    # without new information.
    "freshness": {"types": ["doc", "decision", "reference", "note", "runbook", "project"],
                  "review_days": 180, "near_duplicate": 0.85,
                  # How often each type is reviewed (a runbook ages faster than a decision).
                  "review_days_by_type": {"runbook": 120, "doc": 180, "decision": 365, "reference": 365},
                  "penalty": {"replaced": 0.4, "expired": 0.5, "unverified": 1.0}},
}

# ───────────────────────────────────────────────────────────── locate the vault
ENV_VAULT = ("VAULT_MEMORY_VAULT", "CEREBRO_VAULT")


def state_dir(vault):
    """`.vault-memory/`, or the legacy `.cerebro/` while a vault has not been migrated."""
    vault = Path(vault)
    if not (vault / STATE).is_dir() and (vault / LEGACY_STATE).is_dir():
        return vault / LEGACY_STATE
    return vault / STATE


def find_vault(start=None):
    """Walk up from `start` until a folder with .obsidian/ or a state folder appears."""
    here = Path(start or Path.cwd()).resolve()
    for candidate in [here, *here.parents]:
        if any((candidate / d).is_dir() for d in (".obsidian", STATE, LEGACY_STATE)):
            return candidate
    return None


VAULT_ORIGIN = ""   # where the resolved vault came from: shown by `vault-memory info`


def profile_vault():
    """Vault declared in the `env` of the active Claude Code profile settings.json
    ($CLAUDE_CONFIG_DIR). It beats the environment variable: a project settings.json also
    exports env and can override it. None outside Claude Code."""
    profile = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()
    try:
        env = json.loads((profile / "settings.json").read_text(encoding="utf-8"))["env"]
    except Exception:
        return None
    return next((env[k] for k in ENV_VAULT if env.get(k)), None)


def _vault_sources():
    yield "profile", profile_vault()
    for key in ENV_VAULT:
        yield key, os.environ.get(key)
    yield "CLAUDE_PLUGIN_OPTION_VAULT", os.environ.get("CLAUDE_PLUGIN_OPTION_VAULT")
    # `claude plugin eval` sandbox: only EVAL_* variables get through
    yield "EVAL_VAULT_MEMORY_VAULT", os.environ.get("EVAL_VAULT_MEMORY_VAULT")


def hook_vault():
    """Vault for the hooks: same order as resolve_vault without --vault or cwd search.
    None if there is none (the hook does nothing)."""
    for _, value in _vault_sources():
        if value and Path(value).expanduser().is_dir():
            return str(Path(value).expanduser())
    return None


def resolve_vault(explicit=None, require_config=True):
    # Order: --vault > profile settings.json > environment > plugin option > walk up from cwd.
    global VAULT_ORIGIN
    for origin, value in (("--vault", explicit), *_vault_sources()):
        if value:
            vault, VAULT_ORIGIN = Path(value).expanduser().resolve(), origin
            break
    else:
        vault, VAULT_ORIGIN = find_vault(), "cwd"
    if vault is None:
        raise SystemExit(
            "Can't find an Obsidian vault.\n"
            "  · Run from inside the vault, or pass  --vault /path/to/vault\n"
            "  · For a new vault that has no .obsidian/ yet, run:  vault-memory init --vault /path")
    if not vault.is_dir():
        raise SystemExit(f"Does not exist: {vault}")
    if require_config and not (state_dir(vault) / "config.json").exists():
        raise SystemExit(f"'{vault.name}' is not initialized yet. Run:  vault-memory init --vault \"{vault}\"")
    return vault


def merge(base, own):
    """Deep merge: an old config without the new keys inherits the defaults."""
    for key, value in own.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            merge(base[key], value)
        else:
            base[key] = value
    return base


def read_config_file(vault):
    """The vault's own config.json, translated to the current keys (legacy configs too)."""
    config_file = state_dir(vault) / "config.json"
    if not config_file.exists():
        return {}
    try:
        own = json.loads(config_file.read_text(encoding="utf-8"))
    except Exception as e:
        raise SystemExit(f"invalid config.json: {e}")
    return LG.config(own)


def load_config(vault):
    cfg = json.loads(json.dumps(CONFIG_DEF))
    merge(cfg, read_config_file(vault))
    cfg["_vault"] = vault
    cfg["_state"] = state_dir(vault)
    configure(cfg)
    return cfg


# What depends on the config but is used by functions without cfg (tokens(), search()):
# fixed once when it is loaded. One process works on one vault.
CLOSED, OBSOLETE, HIGH_SEVERITY, SIMILAR_SKIP = set(), set(), set(), set()
FRESHNESS = dict(CONFIG_DEF["freshness"])
TITLE_BONUS = CONFIG_DEF["recall"]["title_bonus"]
_STEMMER, _IRREGULAR_EXTRA, _STOP_EXTRA = "en", {}, set()


def configure(cfg):
    global CLOSED, OBSOLETE, HIGH_SEVERITY, SIMILAR_SKIP, _STEMMER, _IRREGULAR_EXTRA, _STOP_EXTRA
    global FRESHNESS, TITLE_BONUS
    FRESHNESS = cfg["freshness"]
    TITLE_BONUS = float(cfg["recall"].get("title_bonus", 8))
    CLOSED = {norm(x) for x in cfg["statuses"]["closed"]}
    OBSOLETE = {norm(x) for x in cfg["statuses"]["obsolete"]}
    HIGH_SEVERITY = {norm(x) for x in cfg["severities"]["high"]}
    SIMILAR_SKIP = set(cfg["recall"]["similar_skip_types"])
    language = cfg["language"]
    _STEMMER = language.get("stemmer", "en")
    _IRREGULAR_EXTRA = {norm(k): norm(v) for k, v in language.get("irregular", {}).items()}
    _STOP_EXTRA = {norm(x) for x in language.get("extra_stopwords", [])}


def cfg_hash(cfg):
    """Fingerprint of what changes which notes enter the index and how they are tokenized:
    if it changes, the incremental index is useless and gets rebuilt."""
    keys = ("language", "exclude", "types_by_folder", "extra_types", "areas", "default_area",
            "references", "modules", "recall")
    payload = json.dumps({k: cfg.get(k) for k in keys}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(f"{TOK_VER}|{IDX_VER}|{payload}".encode("utf-8")).hexdigest()[:12]


def today():
    """The date of "today" for stale and expiry checks. Pinned with VAULT_MEMORY_TODAY (or
    `--today`) so two runs over the same vault give the same output in tests."""
    pinned = (os.environ.get("VAULT_MEMORY_TODAY") or os.environ.get("CEREBRO_HOY") or "").strip()
    return dt.date.fromisoformat(pinned) if pinned else dt.date.today()


def walk(cfg, md_only=True, prune_excluded=True):
    """The single traversal of the vault. Prunes excluded and hidden folders in place
    (.git, .obsidian, .trash…). Stable order so the index is reproducible."""
    vault = cfg["_vault"]
    # Without pruning excluded folders (to find attachments: `_attachments/` has no notes but
    # does have the files notes embed) the folders that never matter are still skipped.
    excluded = excluded_folders(cfg) if prune_excluded else {"node_modules", "__pycache__"}
    for root_dir, dirs, files in os.walk(vault):
        dirs[:] = sorted(d for d in dirs if d not in excluded and not d.startswith("."))
        for name in sorted(files):
            if md_only and not name.endswith(".md"):
                continue
            path = Path(root_dir) / name
            yield path, path.relative_to(vault).as_posix()


def save_config(vault, cfg):
    own = {k: v for k, v in cfg.items() if not k.startswith("_")}
    target = state_dir(vault) / "config.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(own, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def families(cfg):
    return {**FAMILY, **cfg.get("extra_types", {})}


def required_fields(note_type, cfg):
    fields = list(SCHEMA.get(note_type, BASE))
    for extra in cfg.get("extra_schema", {}).get(note_type, []):
        if extra not in fields:
            fields.append(extra)
    return fields


def record_types(cfg):
    """Types for which recurrence, stale notes and linking gaps are computed."""
    types = (cfg.get("patterns") or {}).get("types")
    return [x for x in types if x] if types else ["incident"]


def modules(cfg):
    """Effective state of each module. What is not declared uses the standard profile
    default, so an old config keeps working as before."""
    state = dict(PROFILES["standard"])
    state.update({k: bool(v) for k, v in (cfg.get("modules") or {}).items() if k in MODULES})
    return state


def enabled(cfg, name):
    return modules(cfg).get(name, False)


def excluded_folders(cfg):
    return set(EXCLUDE) | set(cfg.get("exclude", []))


# ─────────────────────────────────────────────────────────────── tokenization
STOP_ES = set("""
a al algo algun alguna algunas alguno algunos ante antes aqui asi aun aunque cada casi
como con contra cual cuales cuando cuanto de del desde donde dos el ella ellas ello ellos
en entre era eran eres es esa esas ese eso esos esta estan estas este esto estos fue
fueron ha habia hace hacen hacer hacia han hasta hay la las le les lo los mas me menos mi
mientras mis mucho muy nada ni no nos nosotros o otra otras otro otros para pero poco por
porque pues que quien quienes se sea segun ser si sin sobre solo son su sus tambien tan
tanto te tiene tienen todo todos tras un una uno unos vez y ya
""".split())
STOP_EN = set("""
a about after again all also am an and any are as at be been before being both but by can
could did do does doing down during each few for from had has have having he her here hers
him his how if in into is it its itself just me more most my no nor not now of off on once
only or other our out over own same she should so some such than that the their them then
there these they this those through to too under until up very was we were what when where
which while who whom why will with would you your yours
""".split())
STOP = STOP_ES | STOP_EN

TOK = re.compile(r"[a-z0-9][a-z0-9_\-\.]{1,31}")

# Light stemmers shared by index and query (tokens()). Precision first: minimum root of 4
# letters, one suffix after the plural, and tokens with digits, dashes, dots or _ are never
# touched (IDs, ATL-123, api-service, logs-back-*).
TOK_VER = 2   # bump when tokenization changes: invalidates the index
IDX_VER = 4   # bump when the fields of each indexed doc change
ALPHA = re.compile(r"[a-z]+")

IRREGULAR_ES = {"cayo": "cai", "cayeron": "cai", "caido": "cai", "caida": "cai", "caidos": "cai",
                "caidas": "cai", "cae": "cai", "caen": "cai", "caer": "cai", "cayendo": "cai"}
SUFFIXES_ES = ("aciones", "iciones", "amientos", "imientos", "amiento", "imiento", "acion", "icion",
               "ados", "adas", "idos", "idas", "ado", "ada", "ido", "ida", "ando", "iendo",
               "aron", "ieron", "aban", "aba", "ar", "er", "ir", "o", "a", "e")

IRREGULAR_EN = {"ran": "run", "running": "run", "went": "go", "gone": "go", "broke": "break",
                "broken": "break", "paid": "pay", "sent": "send", "failure": "fail", "failures": "fail"}
SUFFIXES_EN = ("ations", "ation", "ments", "ment", "ings", "ing", "edly", "ed", "ers", "er",
               "ness", "ly")


def stem_es(word):
    if word in IRREGULAR_ES:
        return IRREGULAR_ES[word]
    if len(word) < 5 or not ALPHA.fullmatch(word):
        return word
    if word.endswith("ces") and len(word) - 3 >= 4:
        word = word[:-3] + "z"
    elif word.endswith("es") and word[-3] in "nlrdj" and len(word) - 2 >= 4:
        word = word[:-2]
    elif word.endswith("s") and word[-2] in "aeo" and len(word) - 1 >= 4:
        word = word[:-1]
    for suffix in SUFFIXES_ES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[:-len(suffix)]
    return word


def stem_en(word):
    if word in IRREGULAR_EN:
        return IRREGULAR_EN[word]
    if len(word) < 5 or not ALPHA.fullmatch(word):
        return word
    if word.endswith("ies") and len(word) - 3 >= 3:
        word = word[:-3] + "y"
    elif word.endswith(("sses", "shes", "ches", "xes")) and len(word) - 2 >= 4:
        word = word[:-2]
    elif word.endswith("s") and not word.endswith(("ss", "us", "is")) and len(word) - 1 >= 4:
        word = word[:-1]
    for suffix in SUFFIXES_EN:
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            word = word[:-len(suffix)]
            # deployed → deploy, stopped → stop
            if len(word) >= 5 and word[-1] == word[-2] and word[-1] not in "lsz":
                word = word[:-1]
            return word
    return word


def stem(word):
    if word in _IRREGULAR_EXTRA:
        return _IRREGULAR_EXTRA[word]
    if _STEMMER == "es":
        return stem_es(word)
    if _STEMMER == "en":
        return stem_en(word)
    return word


def norm(text):
    text = unicodedata.normalize("NFD", str(text).lower())
    return "".join(c for c in text if unicodedata.category(c) != "Mn")


def tokens(text, keep_stopwords=False):
    for match in TOK.finditer(norm(text)):
        token = match.group(0).strip("-._")
        if len(token) >= 3 and not token.isdigit() and (
                keep_stopwords or (token not in STOP and token not in _STOP_EXTRA)):
            yield token if token in STOP else stem(token)


def detect_language(texts):
    """'en', 'es' or 'none' from stopword frequency in a sample of notes (used by init)."""
    counts = {"en": 0, "es": 0}
    for text in texts:
        for word in re.findall(r"[a-z]+", norm(text)):
            if word in STOP_EN and word not in STOP_ES:
                counts["en"] += 1
            elif word in STOP_ES and word not in STOP_EN:
                counts["es"] += 1
    if not any(counts.values()):
        return "none"
    return "es" if counts["es"] > counts["en"] else "en"


# ────────────────────────────────────────────────────────────── frontmatter
RE_FM_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):[ \t]*(.*)$")


def raw_frontmatter(text):
    """(dict, body) with the keys as written. Deliberately tolerant parser: a real vault has
    hand-written frontmatter and breaking on imperfect YAML would be useless."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    head = text[3:end]
    # Line by line: `key: value` at column 0; an indented line that follows continues the
    # value (folded YAML); `- item` builds a comma-separated list for any key.
    fm, key = {}, None
    for line in head.splitlines():
        match = RE_FM_KEY.match(line)
        if match:
            key, fm[match.group(1)] = match.group(1), match.group(2).strip()
            continue
        stripped = line.strip()
        if key is None or not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- ") or stripped == "-":
            item = stripped[1:].strip().strip('"').strip("'")
            fm[key] = f"{fm[key]},{item}" if fm[key] else item
        elif line[:1] in " \t":
            fm[key] = f"{fm[key]} {stripped}".strip()
    fm = {k: v.strip().strip('"').strip("'") for k, v in fm.items()}
    if fm.get("tags", "").startswith("["):
        fm["tags"] = fm["tags"].strip("[]")
    return fm, text[end + 4:]


def frontmatter(text):
    """(dict, body) with legacy (Spanish) keys and type values translated to the current ones."""
    fm, body = raw_frontmatter(text)
    return LG.frontmatter(fm), body


def tag_list(fm):
    return [t.strip().strip('"') for t in fm.get("tags", "").split(",") if t.strip()]


# ─────────────────────────────────────────────────── derive type and area
RE_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")
MONTHS_EN = {m: i for i, m in enumerate(
    "January February March April May June July August September October November December".split(), 1)}
MONTHS_ES = {m: i for i, m in enumerate(
    "enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre".split(), 1)}


def by_prefix(mapping, rel):
    """Match by path prefix, the most specific wins."""
    best, length = None, -1
    for prefix, value in (mapping or {}).items():
        clean = prefix.strip("/")
        if clean and (rel == clean or rel.startswith(clean + "/")) and len(clean) > length:
            best, length = value, len(clean)
    return best


def area_of(rel, cfg):
    return by_prefix(cfg.get("areas"), rel) or cfg.get("default_area", "general")


def structural_type(rel, name, tags, cfg):
    """Without looking at `type`: what this note looks like from where it lives and its name.

    Template is decided FIRST: a template declares in its tags the type it generates
    (`incident`), so looking at the tags first would pass it as a real note."""
    by_folder = by_prefix(cfg.get("types_by_folder"), rel)
    # The whole path is checked: a template is usually called "Incident.md" and the only
    # thing that gives it away is the folder that contains it.
    if by_folder == "template" or re.search(r"(?:^|/)[^/]*\b(template|plantilla)s?\b", rel, re.I):
        return "template"
    known = families(cfg)
    for tag in tags:
        tag_type = LG.type_value(tag.lower())
        if tag_type in known:
            return tag_type
    if re.search(r"\b(index|indice|home|readme|portada|moc)\b", name, re.I):
        return "home"
    if by_folder:
        return by_folder
    return "note" if "/" not in rel else "doc"


def type_of(rel, name, tags, fm, cfg):
    """A written `type` wins; otherwise it is derived from the structure."""
    return (fm.get("type") or "").strip() or structural_type(rel, name, tags, cfg)


def date_from_path(rel, name):
    """.../2026/January/20 - Title.md  ->  2026-01-20"""
    parts = rel.split("/")
    year = next((x for x in parts if re.fullmatch(r"(19|20)\d{2}", x)), None)
    month = next((MONTHS_EN.get(x) or MONTHS_ES.get(x.lower()) for x in parts
                  if x in MONTHS_EN or x.lower() in MONTHS_ES), None)
    day = re.match(r"(\d{1,2})\s*[-–]", name)
    if year and month and day:
        return f"{year}-{month:02d}-{int(day.group(1)):02d}"
    return None


if __name__ == "__main__":
    # Stemmer self-check: families that must merge and tokens that must stay untouched.
    for family in (("cayo", "caido", "caida", "cae"), ("devoluciones", "devolucion"),
                   ("pagos", "pago"), ("cancelar", "cancelacion", "cancelado"),
                   ("rechazo", "rechazado", "rechazaron")):
        assert len({stem_es(w) for w in family}) == 1, (family, [stem_es(w) for w in family])
    for family in (("payments", "payment"), ("deploy", "deployed", "deploying", "deployment", "deploys"),
                   ("refund", "refunds", "refunded", "refunding"), ("crash", "crashes", "crashed"),
                   ("stopped", "stopping")):
        assert len({stem_en(w) for w in family}) == 1, (family, [stem_en(w) for w in family])
    for untouched in ("api-service", "48213", "atl-123", "logs-back-", "ghsa-x"):
        assert stem_es(untouched) == untouched and stem_en(untouched) == untouched, untouched
    print("stemmer ok")
