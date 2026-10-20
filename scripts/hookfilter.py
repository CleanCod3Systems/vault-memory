"""Which recall results deserve to be a hook pointer. Shared by hooks/recall.py and
`vault-memory eval --hook`, so what is measured is exactly what is injected.

Everything vault-dependent comes from config.json (`hook.*`, `recall.skip_paths`).
"""
import re
import unicodedata

DEFAULTS = {"domain": "", "memory_only": "", "memory_prefix": "", "floor": 8,
            # Prompts nobody typed (background agent notices, bots): they carry IDs and
            # domain vocabulary, so without this they pass the filter and are noise.
            "skip_prefixes": ["<task-notification"],
            "relative": 0.45, "max": 3, "min_length": 15, "format": "compact", "repeat_max": 2,
            # Brief mode: with a clear answer (high top and the second far behind) the top-1
            # passage is added, to answer without opening the note. 0 turns it off.
            "brief_floor": 20, "brief_relative": 0.6, "brief_width": 240}
SKIP_TYPES = {"home", "index"}
ID = re.compile(r"\b(\d{4,}|[A-Z]{2,}-\d+|GHSA-[\w-]+)\b")

# Request and chat words that do not say what the prompt is about, in English and Spanish.
# They do not count for the title overlap, and a prompt made only of them is conversational.
# Matched as whole words (never by prefix: "check" must not hide "checkout").
FILLER = set("""
about after again also always another anything around back before being better could does
doing done else even every from give going good great have help here into just know last
later like look looking make maybe more much need next nice none nothing okay other please
really right same should show some something still sure tell than thank thanks that their
them then there these thing things think this those through want well what when where which
while will with without would yeah your continue keep proceed finish finished commit wrap
ready summary summarize recap remind earlier above below asked said told talked ahead
anyway cool fine perfect awesome sounds tomorrow today yesterday soon
como cual cuales donde cuando porque para sobre desde hasta entre esto esta este eso
esos esas otra otro otras otros todo todos nada algo tiene tienen hace hacer hacen
puede pueden sigue segui seguir dale listo gracias favor revisa revise revisar mira
mirar dime decime quiero necesito paso pasa pasar tema cosa cosas bien mejor ahora
despues antes tambien sola solo cada vez veces hola chau mismo misma forma
avisa avisame avisar termina terminar termines terminaste commitea commitear
pedi pediste dije dijiste hablamos resumen resumi resumir anterior recien
perfecto genial buenisimo vamos sigamos continua continuar
""".split())


def options(cfg):
    return {**DEFAULTS, **(cfg.get("hook") or {})}


def _words(text, minimum):
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.findall(r"[a-z0-9_]{%d,}" % minimum, text)


def stems(text, minimum=4):
    # 5-letter prefix without accents: enough to match deploy/deployed or refunds/refund
    # when comparing a request against a title.
    return {w[:5] for w in _words(text, minimum) if w not in FILLER}


def request_terms(prompt):
    # 3-letter acronyms (CRM, API, 3DS) are among the most specific things a request carries,
    # but they do not pass the length filter: they are added only when written in capitals.
    return stems(prompt) | {w.lower() for w in re.findall(r"\b[A-Z0-9]{3}\b", prompt)}


def is_conversational(prompt):
    """A prompt with no content word: "ok, go ahead and commit", "dale, seguí", "thanks"."""
    return not ID.search(prompt) and not request_terms(prompt)


def decide(prompt, cfg):
    """(search, memory_only, reason_if_not). A short request with an ID does search."""
    hook = options(cfg)
    if (len(prompt) < hook["min_length"] and not ID.search(prompt)) or prompt[:1] in ("/", "!"):
        return False, False, "short"
    if prompt.lstrip().startswith(tuple(hook["skip_prefixes"])):
        return False, False, "automatic"
    if is_conversational(prompt):
        return False, False, "conversational"
    in_domain = not hook["domain"] or bool(re.search(hook["domain"], prompt, re.I) or ID.search(prompt))
    memory_only = not in_domain and bool(hook["memory_only"] and re.search(hook["memory_only"], prompt, re.I))
    if not (in_domain or memory_only):
        return False, False, "domain"
    return True, memory_only, ""


def select(rows, prompt, cfg, memory_only=False, exclude=()):
    """(hits, reason_if_not). `rows`: output of `recall --json` (or equivalent)."""
    hook = options(cfg)
    skip = tuple(cfg.get("recall", {}).get("skip_paths", []))
    request = request_terms(prompt)
    asked_ids = {x.upper() for x in ID.findall(prompt)}

    def mentions_asked_id(row):
        known = set(row.get("ids", [])) | set((row.get("refs") or {}).get("tickets", []))
        return bool(asked_ids & {x.upper() for x in known})

    def useful(row):
        path = row.get("p", "")
        if "/" not in path or (skip and path.startswith(skip)) or row.get("type") in SKIP_TYPES:
            return False
        # A replaced or expired note is never a good pointer: if that is the topic, the
        # current one matches too.
        if row.get("state") in ("replaced", "expired") or path in exclude:
            return False
        if memory_only and hook["memory_prefix"] and not path.startswith(hook["memory_prefix"]):
            return False
        # An ID or ticket the note mentions is as strong a match as a shared title word.
        return mentions_asked_id(row) or bool(
            request & stems(" ".join((row.get("n", ""), row.get("t", ""), path)), minimum=3))

    candidates = [r for r in rows if useful(r)]
    if not candidates or candidates[0]["score"] < hook["floor"]:
        return [], "floor"
    cut = candidates[0]["score"] * hook["relative"]
    return [r for r in candidates if r["score"] >= cut][:hook["max"]], ""


def brief_passage(hits, hook):
    """Short passage of the top-1 only if confidence is high and the note is current and
    verified or without freshness data (never unverified/expired/replaced)."""
    if not hook.get("brief_floor") or not hits:
        return ""
    top = hits[0]
    second = hits[1]["score"] if len(hits) > 1 else 0
    if top["score"] < hook["brief_floor"] or second > hook["brief_relative"] * top["score"]:
        return ""
    if top.get("state", "ok") != "ok":
        return ""
    text = (top.get("passage") or "").strip()
    width = int(hook.get("brief_width", 240))
    return text[:width] + ("…" if len(text) > width else "")


def pointer(hits, fmt="compact"):
    """The text injected into the model. English on purpose: the model replies in the
    user's language whatever the language of this hint."""
    if fmt == "classic":
        return ("[vault] Possibly relevant notes: {}. If they apply to the request, read them "
                "before answering; otherwise ignore them.".format(
                    "; ".join('"{}" ({})'.format(r["n"], r["p"]) for r in hits)))
    parts = []
    for row in hits:
        meta = row.get("type", "")
        if row.get("verified"):
            meta += f", verified {row['verified'][:7]}"
        parts.append(f"{row['p']} ({meta})")
    return "[vault] " + " · ".join(parts) + " — if relevant, read only what you need."
