"""Legacy (Spanish) names → current names, in one place.

Vaults created by earlier versions (obsidian-cerebro) use Spanish frontmatter keys, type
values and config keys. They keep working: every load goes through these tables. The same
tables drive `vault-memory migrate`, which rewrites the vault so the translation is no
longer needed.
"""
import re

# Frontmatter keys. `tipo` was the old fallback for `clase` (the class of an incident).
FM_KEYS = {
    "tipo_nota": "type", "fecha": "date", "estado": "status", "severidad": "severity",
    "sistema": "system", "periodo": "period", "proyecto": "project", "clase": "class",
    "tipo": "class", "verificado": "verified", "vence": "expires", "clave": "key",
    "reemplaza": "replaces", "fuente": "source", "desde": "since", "plantilla": "template",
    "generado": "generated",
}
# Keys whose legacy value wins over an existing current key. A note with both `tipo_nota`
# and `type` usually carries `type: note` left by another tool; `tipo_nota` is the real one.
FM_LEGACY_WINS = {"tipo_nota"}

TYPE_VALUES = {
    "incidente": "incident", "reunion": "meeting", "diario": "daily", "periodo": "period",
    "referencia": "reference", "proyecto": "project", "plantilla": "template", "nota": "note",
    "indice": "index",
}
FAMILY_VALUES = {"registro": "record", "conocimiento": "knowledge", "estructura": "structure"}

MODULE_NAMES = {"contrato": "contract", "grafo": "graph", "patrones": "patterns",
                "plantillas": "templates", "agenda": "schedule"}
PROFILE_NAMES = {"minimo": "minimal", "estandar": "standard", "equipo": "team"}

# Config keys, by section ("" = top level).
CONFIG_KEYS = {
    "": {"modulos": "modules", "area_defecto": "default_area", "tipos_por_carpeta": "types_by_folder",
         "tipos_extra": "extra_types", "esquema_extra": "extra_schema", "excluir": "exclude",
         "referencias": "references", "taxonomia": "taxonomy", "patrones": "patterns",
         "idioma": "language", "estados": "statuses", "severidades": "severities",
         "vigencia": "freshness", "telemetria": "telemetry"},
    "references.tickets": {"prefijos": "prefixes"},
    "references.chat": {"dominio": "domain"},
    "references.ids": {"palabras": "words"},
    "patterns": {"tipos": "types", "estancado_dias": "stale_days"},
    "language": {"stop_extra": "extra_stopwords", "irregulares": "irregular"},
    "statuses": {"cerrados": "closed", "obsoletos": "obsolete"},
    "severities": {"altas": "high"},
    "recall": {"saltar_rutas": "skip_paths", "similares_saltar_tipos": "similar_skip_types",
               "tope_terminos": "max_terms", "peso_tags": "tag_weight", "peso_h2": "h2_weight",
               "peso_ruta": "path_weight", "bonus_titulo": "title_bonus"},
    "hook": {"dominio": "domain", "infra": "memory_only", "solo_memoria_prefijo": "memory_prefix",
             "piso": "floor", "saltar_prefijos": "skip_prefixes", "relativo": "relative",
             "min_largo": "min_length", "formato": "format", "repetir_max": "repeat_max",
             "brief_piso": "brief_floor", "brief_relativo": "brief_relative",
             "brief_ancho": "brief_width", "cierre": "stop_prompt"},
    "freshness": {"tipos": "types", "revisar_dias": "review_days", "casi_duplicado": "near_duplicate",
                  "revisar_dias_por_tipo": "review_days_by_type", "penalizacion": "penalty"},
    "freshness.penalty": {"reemplazada": "replaced", "vencida": "expired", "sin_verificar": "unverified"},
    "telemetry": {"activa": "enabled", "guardar_prompt": "store_prompt", "mudos": "log_silent"},
}
HOOK_FORMATS = {"compacto": "compact", "clasico": "classic"}


def type_value(value):
    return TYPE_VALUES.get(value, value)


def frontmatter(fm):
    """Translate legacy keys and type values of one parsed frontmatter (dict of strings)."""
    if not any(k in FM_KEYS for k in fm):
        if fm.get("type") in TYPE_VALUES:
            return {**fm, "type": type_value(fm["type"])}
        return fm
    out = {k: v for k, v in fm.items() if k not in FM_KEYS}
    for old, new in FM_KEYS.items():
        if old not in fm:
            continue
        if old == "tipo" and ("clase" in fm or "class" in fm):
            continue
        if new not in out or old in FM_LEGACY_WINS:
            out[new] = fm[old]
    if out.get("type"):
        out["type"] = type_value(out["type"])
    return out


def _rename(section, mapping):
    return {mapping.get(k, k): v for k, v in section.items()} if isinstance(section, dict) else section


def is_legacy_config(own):
    return any(k in CONFIG_KEYS[""] for k in own)


def config(own):
    """Translate a legacy config.json (any mix of old and new keys) to the current keys."""
    legacy = is_legacy_config(own)
    out = _rename(own, CONFIG_KEYS[""])
    for path, mapping in CONFIG_KEYS.items():
        if not path:
            continue
        node, keys = out, path.split(".")
        for key in keys[:-1]:
            node = node.get(key) if isinstance(node, dict) else None
        if isinstance(node, dict) and isinstance(node.get(keys[-1]), dict):
            node[keys[-1]] = _rename(node[keys[-1]], mapping)
    # Values that name modules, profiles, types or families.
    if isinstance(out.get("modules"), dict):
        out["modules"] = {MODULE_NAMES.get(k, k): v for k, v in out["modules"].items()}
    if isinstance(out.get("types_by_folder"), dict):
        out["types_by_folder"] = {k: type_value(v) for k, v in out["types_by_folder"].items()}
    if isinstance(out.get("extra_types"), dict):
        out["extra_types"] = {type_value(k): FAMILY_VALUES.get(v, v) for k, v in out["extra_types"].items()}
    if isinstance(out.get("extra_schema"), dict):
        out["extra_schema"] = {type_value(k): [FM_KEYS.get(f, f) for f in v]
                               for k, v in out["extra_schema"].items()}
    for section, key in (("patterns", "types"), ("freshness", "types"), ("recall", "similar_skip_types")):
        values = (out.get(section) or {}).get(key)
        if isinstance(values, list):
            out[section][key] = [type_value(v) for v in values]
    by_type = (out.get("freshness") or {}).get("review_days_by_type")
    if isinstance(by_type, dict):
        out["freshness"]["review_days_by_type"] = {type_value(k): v for k, v in by_type.items()}
    hook = out.get("hook")
    if isinstance(hook, dict) and hook.get("format") in HOOK_FORMATS:
        hook["format"] = HOOK_FORMATS[hook["format"]]
    # Before `language` existed, the stemmer was Spanish by default.
    if legacy and "language" not in out:
        out["language"] = {"stemmer": "es"}
    return out


# ──────────────────────────────────────────────────────────────── migration
RE_KEY_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):(.*)$")


def migrate_note(text):
    """Rewrite the frontmatter keys (and the `type` value) of one note. Body untouched.
    Returns the new text, or None if nothing changes."""
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end < 0:
        return None
    lines = text[4:end].split("\n") if text[3:4] == "\n" else text[3:end].split("\n")
    present = {m.group(1) for m in (RE_KEY_LINE.match(x) for x in lines) if m}
    if not present & set(FM_KEYS) and not any(
            (m := RE_KEY_LINE.match(x)) and m.group(1) == "type" and m.group(2).strip() in TYPE_VALUES
            for x in lines):
        return None
    out, skip_block = [], False
    for line in lines:
        match = RE_KEY_LINE.match(line)
        if match:
            skip_block = False
            key, value = match.group(1), match.group(2)
            if key == "type" and "tipo_nota" in present:
                skip_block = True                 # the legacy `tipo_nota` replaces it
                continue
            if key == "tipo" and ("clase" in present or "class" in present):
                skip_block = True
                continue
            new = FM_KEYS.get(key, key)
            if new != key and new in present and key not in FM_LEGACY_WINS:
                skip_block = True                 # the current key already exists
                continue
            if new == "type":
                value = " " + type_value(value.strip()) if value.strip() else value
            out.append(f"{new}:{value}")
            continue
        if skip_block and (line[:1] in " \t" or line.strip().startswith("- ")):
            continue
        out.append(line)
    new_text = "---\n" + "\n".join(out) + text[end:]
    return new_text if new_text != text else None
