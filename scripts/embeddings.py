"""vault-memory embeddings — optional semantic rerank of recall's BM25 candidates.

BM25 finds the candidates; a vector model only reorders the best `recall.rerank.candidates` of
them by `weight × bm25/max + (1 - weight) × cosine(query, note)`. It never replaces BM25 and
never adds a note BM25 did not find. Off until `embeddings.provider` and `embeddings.model` are
set, and every failure (server down, timeout, missing model, wrong dimension) falls back to plain
BM25 without the user noticing, logging a `rerank_error` telemetry event with a short reason.

Store, in the state folder (never inside notes): `embeddings/index.json` (model, dimension and,
per note path, its row and a hash of the embedded text) and `embeddings/vectors-<gen>.f32`
(unit-length float32 rows). A recall reads the small index and seeks only the rows of its
candidates. A new generation file is written on every change and the index is replaced last,
so a reader never sees a half-written store.

Providers, with distill's conventions: `ollama` (POST {base_url}/api/embed, base_url default
http://localhost:11434) and `openai-compat` (POST {base_url}/embeddings, base_url required, key
from the env var named by `api_key_env`). Text that goes to `openai-compat`, or to an Ollama
that is not on this machine, passes through distill's redaction first.
"""
import array
import hashlib
import json
import math
import os
import re
import sys
import time
import urllib.parse

import core as C
# urllib.request (and http.client) cost ~40 ms to import: loaded only when a call is made, so
# the gate and a recall without embeddings do not pay for it.

PROVIDERS = ("off", "ollama", "openai-compat")
OLLAMA_URL = "http://localhost:11434"
FOLDER = "embeddings"
VERSION = 1
RE_TICKET = re.compile(r"^[A-Za-z]{2,6}-\d{1,6}$")
_REDACT = None


class EmbedError(Exception):
    """A short reason, safe to log: it never carries note or query text."""


def options(cfg):
    return {**C.CONFIG_DEF["embeddings"], **(cfg.get("embeddings") or {})}


def rerank_options(cfg):
    return {**C.CONFIG_DEF["recall"]["rerank"], **((cfg.get("recall") or {}).get("rerank") or {})}


def enabled(cfg):
    return str(options(cfg).get("provider") or "off") != "off"


def _base_url(opts):
    if opts["provider"] == "ollama":
        return (opts.get("base_url") or OLLAMA_URL).rstrip("/")
    return (opts.get("base_url") or "").rstrip("/")


def _key_env(opts):
    return opts.get("api_key_env") or "OPENAI_API_KEY"


def _local(url):
    return urllib.parse.urlsplit(url).hostname in ("localhost", "127.0.0.1", "::1")


def leaves_machine(opts):
    """True when text goes to someone else's server: openai-compat always (it is meant for
    hosted APIs; a local one gets redacted text too, which costs nothing), Ollama off localhost."""
    return opts["provider"] == "openai-compat" or not _local(_base_url(opts))


def _redact(text):
    global _REDACT
    if _REDACT is None:
        import distill as DS           # imported only when needed: distill pulls in a lot
        _REDACT = DS.redact
    return _REDACT(text)


def problem(opts):
    """What keeps the configured provider from working, or "" (no network call)."""
    name = opts["provider"]
    if name not in PROVIDERS:
        return f"unknown embeddings provider {name!r} (use {', '.join(PROVIDERS)})"
    if name == "off":
        return "off"
    if not str(opts.get("model") or "").strip():
        return f"set `embeddings.model` to an embedding model your {name} server serves"
    if name == "openai-compat" and not opts.get("base_url"):
        return "set `embeddings.base_url` (e.g. https://api.openai.com/v1)"
    return ""


def readiness(cfg):
    """(provider, ok, detail): are the prerequisites there? No network calls, like distill."""
    opts = options(cfg)
    name, issue = opts["provider"], problem(opts)
    if issue:
        return name, False, issue
    url = _base_url(opts)
    if name == "openai-compat":
        env = _key_env(opts)
        has = bool(os.environ.get(env))
        note = "is set" if has else "is not set" + (" (optional for a local server)" if _local(url) else "")
        return name, has or _local(url), f"{url} · {env} {note}"
    return name, True, f"{url} (no key; server not contacted)"


# ───────────────────────────────────────────────────────────── provider calls
def _post(url, body, headers, timeout):
    import socket
    import urllib.error
    import urllib.request
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers={"content-type": "application/json", **headers})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise EmbedError(f"http {e.code}") from None
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise EmbedError("timeout") from None
        raise EmbedError("connection") from None
    except (socket.timeout, TimeoutError):
        raise EmbedError("timeout") from None
    except (OSError, ValueError):
        raise EmbedError("bad response") from None


def client(cfg, timeout=None):
    """callable(texts) -> [vector] for the configured provider. Raises EmbedError."""
    opts = options(cfg)
    issue = problem(opts)
    if issue:
        raise EmbedError(issue)
    url, model = _base_url(opts), str(opts["model"]).strip()
    timeout = float(timeout if timeout is not None else opts["timeout"])

    def call(texts):
        texts = list(texts)
        if not texts:
            return []
        if opts["provider"] == "ollama":
            data = _post(url + "/api/embed", {"model": model, "input": texts}, {}, timeout)
            vectors = data.get("embeddings") if isinstance(data, dict) else None
        else:
            key = os.environ.get(_key_env(opts))
            headers = {"authorization": f"Bearer {key}"} if key else {}
            data = _post(url + "/embeddings", {"model": model, "input": texts}, headers, timeout)
            rows = data.get("data") if isinstance(data, dict) else None
            try:
                vectors = [r["embedding"] for r in sorted(rows, key=lambda r: r.get("index", 0))]
            except (TypeError, KeyError, AttributeError):
                vectors = None
        if not isinstance(vectors, list) or len(vectors) != len(texts) or not all(
                isinstance(v, list) and v and all(isinstance(x, (int, float)) for x in v) for v in vectors):
            raise EmbedError("bad response")
        return vectors
    return call


def _unit(vector):
    norm = math.sqrt(sum(x * x for x in vector))
    if not norm or math.isnan(norm):
        raise EmbedError("empty vector")
    return [x / norm for x in vector]


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


# ───────────────────────────────────────────────────────────── text of a note
def eligible(cfg, docs):
    """Notes worth a vector: not archived, not templates, not captures (those are not indexed)."""
    captures = C.captures_folder(cfg) + "/"
    return [d for d in docs if not d.get("archived") and not d.get("template")
            and not d["p"].startswith(captures)]


def note_text(cfg, d, opts=None):
    """Title and body (no frontmatter), whitespace collapsed, cut at `max_chars`, redacted when
    it leaves the machine. "" when the note cannot be read."""
    opts = opts or options(cfg)
    try:
        text = (cfg["_vault"] / d["p"]).read_text(encoding="utf-8")
    except Exception:
        return ""
    body = C.frontmatter(text)[1]
    text = " ".join(f"{d.get('t') or d.get('n') or ''}\n\n{body}".split())[:int(opts["max_chars"])]
    return _redact(text) if leaves_machine(opts) else text


def text_hash(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


# ───────────────────────────────────────────────────────────── store
def _dir(cfg):
    return cfg["_state"] / FOLDER


def load_store(cfg):
    """The store's index ({"model", "dim", "file", "notes": {path: [row, hash]}}) or None."""
    try:
        meta = json.loads((_dir(cfg) / "index.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict) or meta.get("v") != VERSION or not isinstance(meta.get("notes"), dict):
        return None
    return meta


def read_vectors(cfg, meta, paths):
    """{path: unit vector} for the paths that have one, reading only their rows."""
    dim, out = int(meta["dim"]), {}
    rows = sorted((meta["notes"][p][0], p) for p in paths if p in meta["notes"])
    if not rows:
        return out
    size = dim * 4
    with open(_dir(cfg) / meta["file"], "rb") as handle:
        for row, path in rows:
            handle.seek(row * size)
            values = array.array("f")
            values.frombytes(handle.read(size))
            if len(values) != dim:
                raise EmbedError("store")
            if meta.get("byteorder", sys.byteorder) != sys.byteorder:
                values.byteswap()
            out[path] = values
    return out


def _save(cfg, model, dim, entries):
    """entries: [(path, hash, unit vector)]. Writes a new vectors file, then the index."""
    folder = _dir(cfg)
    folder.mkdir(parents=True, exist_ok=True)
    name = f"vectors-{time.time_ns():x}.f32"
    flat = array.array("f")
    notes = {}
    for row, (path, digest, vector) in enumerate(entries):
        flat.extend(vector)
        notes[path] = [row, digest]
    with open(folder / name, "wb") as handle:
        flat.tofile(handle)
    meta = {"v": VERSION, "model": model, "dim": dim, "byteorder": sys.byteorder, "file": name,
            "count": len(entries), "notes": notes}
    tmp = folder / "index.json.tmp"
    tmp.write_text(json.dumps(meta, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(folder / "index.json")
    for old in folder.glob("vectors-*.f32"):
        if old.name != name:
            try:
                old.unlink()
            except OSError:
                pass                      # in use by a reader (Windows): removed next time


def _progress(done, total):
    if sys.stderr.isatty():
        print(f"\r  embedding {done}/{total}", end="" if done < total else "\n", file=sys.stderr, flush=True)


def build(cfg, docs=None, full=False, embed=None):
    """Embed new or changed notes, drop deleted ones, rebuild everything when the model
    changed (or with `full`). What was embedded before a failure is kept. Returns counts;
    raises EmbedError with the counts so far in `.result` when the provider fails."""
    started = time.time()
    opts = options(cfg)
    issue = problem(opts)
    if issue:
        raise EmbedError("embeddings are off: set `embeddings.provider` and `embeddings.model` in config.json"
                         if issue == "off" else issue)
    model = str(opts["model"]).strip()
    if docs is None:
        import recall as RC
        docs = RC.load(cfg)["docs"]
    embed = embed or client(cfg, timeout=opts["batch_timeout"])
    meta = load_store(cfg)
    rebuilt = bool(meta) and meta.get("model") != model
    keep_old = bool(meta) and not rebuilt and not full
    try:
        old = read_vectors(cfg, meta, meta["notes"]) if keep_old else {}
    except (OSError, EmbedError):
        keep_old, old = False, {}          # a damaged store is rebuilt, never trusted
    dim = int(meta["dim"]) if keep_old and meta.get("dim") else 0
    entries, todo = [], []
    targets = eligible(cfg, docs)
    for d in targets:
        text = note_text(cfg, d, opts)
        if not text:
            continue
        digest = text_hash(text)
        if keep_old and d["p"] in old and meta["notes"][d["p"]][1] == digest:
            entries.append((d["p"], digest, old[d["p"]]))
        else:
            todo.append((d["p"], digest, text))
    kept = len(entries)
    dropped = len(set(meta["notes"]) - {d["p"] for d in targets}) if meta and not rebuilt else 0
    size = max(1, int(opts["batch"]))
    error = None
    try:
        for start in range(0, len(todo), size):
            chunk = todo[start:start + size]
            vectors = embed([text for _, _, text in chunk])
            if len(vectors) != len(chunk):
                raise EmbedError("bad response")
            for (path, digest, _), vector in zip(chunk, vectors):
                if dim and len(vector) != dim:
                    raise EmbedError("dimension")
                dim = dim or len(vector)
                entries.append((path, digest, _unit(vector)))
            _progress(len(entries) - kept, len(todo))
    except EmbedError as e:
        error = e
    finally:
        changed = len(entries) - kept
        if changed or dropped or rebuilt or full or meta is None:
            if entries or meta is not None:
                _save(cfg, model, dim, entries)
    result = {"model": model, "eligible": len(targets), "embedded": changed, "kept": kept,
              "dropped": dropped, "pending": len(todo) - changed, "rebuilt": rebuilt or full,
              "total": len(entries), "dim": dim, "seconds": round(time.time() - started, 1)}
    if error:
        error.result = result
        raise error
    return result


def status(cfg, docs):
    """For doctor: provider, model, notes embedded vs eligible, stale, ready. No network."""
    opts = options(cfg)
    name, ok, detail = readiness(cfg)
    out = {"provider": name, "model": str(opts.get("model") or ""), "eligible": 0, "embedded": 0,
           "stale": 0, "missing": 0, "ready": False, "detail": detail}
    if name == "off":
        return out
    targets = eligible(cfg, docs)
    meta = load_store(cfg) or {"notes": {}, "model": ""}
    same_model = meta.get("model") == out["model"]
    notes = meta["notes"] if same_model else {}
    stale = embedded = 0
    for d in targets:
        if d["p"] not in notes:
            continue
        embedded += 1
        if notes[d["p"]][1] != text_hash(note_text(cfg, d, opts)):
            stale += 1
    out.update(eligible=len(targets), embedded=embedded, stale=stale, missing=len(targets) - embedded)
    if ok and not embedded:
        out["detail"] = detail + (" · store built with another model" if meta["notes"] and not same_model
                                  else "") + " · run `vault-memory embed`"
    out["ready"] = ok and embedded > 0
    return out


# ───────────────────────────────────────────────────────────── rerank
def available(cfg):
    """Embeddings on, with a store for the configured model: recall can rerank."""
    if not enabled(cfg) or problem(options(cfg)):
        return False
    meta = load_store(cfg)
    return bool(meta and meta["notes"] and meta.get("model") == str(options(cfg)["model"]).strip())


def exact_query(query):
    query = query.strip()
    return bool(re.fullmatch(r"\d{3,10}", query) or RE_TICKET.match(query))


def embed_queries(cfg, queries, timeout=None):
    """Unit vectors for several queries at once (bench). Raises EmbedError."""
    opts = options(cfg)
    call = client(cfg, timeout=opts["batch_timeout"] if timeout is None else timeout)
    texts = [_redact(q) if leaves_machine(opts) else q for q in queries]
    out, size = [], max(1, int(opts["batch"]))
    for start in range(0, len(texts), size):
        out += [_unit(v) for v in call(texts[start:start + size])]
    return out


def _log_error(cfg, origin, reason):
    import telemetry as TL
    TL.record(cfg["_vault"], {"ev": "rerank_error", "origin": origin, "reason": str(reason)[:60]}, cfg)


def rerank(cfg, query, results, origin="cli", query_vector=None, embed=None, log=True):
    """(results, reranked). `results`: [(score, doc)] from RC.search, best first. The first
    `candidates` are reordered; the rest keep their place after them. Scores are rescaled so the
    new first keeps the BM25 top score (thresholds keep their meaning) and the order is the
    combined one. Any failure → the same results, False, and a `rerank_error` event."""
    if len(results) < 2 or exact_query(query) or not enabled(cfg) or problem(options(cfg)):
        return results, False
    meta = load_store(cfg)
    if not (meta and meta["notes"] and meta.get("model") == str(options(cfg)["model"]).strip()):
        return results, False             # `embed` has not run for this model: plain BM25
    params = rerank_options(cfg)
    weight = min(1.0, max(0.0, float(params["weight"])))
    count = max(2, int(params["candidates"]))
    head, tail = results[:count], results[count:]
    try:
        try:
            vectors = read_vectors(cfg, meta, [d["p"] for _, d in head])
        except OSError:
            raise EmbedError("store") from None
        if not vectors:
            return results, False        # none of them embedded yet: nothing to compare
        if query_vector is None:
            opts = options(cfg)
            text = _redact(query) if leaves_machine(opts) else query
            query_vector = _unit((embed or client(cfg))([text])[0])
        if len(query_vector) != int(meta["dim"]):
            raise EmbedError("dimension")
    except Exception as e:                # whatever it is, recall still answers with BM25
        if log:
            _log_error(cfg, origin, e if isinstance(e, EmbedError) else f"error {type(e).__name__}")
        return results, False
    cosine = {p: _dot(query_vector, v) for p, v in vectors.items()}
    values = sorted(cosine.values())
    half = len(values) // 2
    middle = values[half] if len(values) % 2 else (values[half - 1] + values[half]) / 2
    top = max(score for score, _ in head) or 1.0
    combined = [(weight * score / top + (1 - weight) * cosine.get(d["p"], middle), d) for score, d in head]
    combined.sort(key=lambda x: (-x[0], x[1]["p"]))
    best = combined[0][0]
    if best <= 0:                         # only with weight < 0.5 and opposite vectors
        return [(top * (len(combined) - i) / len(combined), d) for i, (_, d) in enumerate(combined)] + tail, True
    return [(max(value, 0.0) * top / best, d) for value, d in combined] + tail, True
