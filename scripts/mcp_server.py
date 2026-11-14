"""vault-memory mcp — the same engine as an MCP server (stdio, JSON-RPC 2.0, stdlib only).

For clients without a shell or hooks (Claude desktop chat, Codex, Cursor, Gemini, Copilot):
memory is queried and written with the same recall, the same contract and the same duplicate
detection as the CLI. In Claude Code it is not needed: the hook and the CLI cover the same
without adding tool definitions to every session's context.

stdout carries only protocol messages; every notice goes to stderr.
"""
import json
import re
import sys

import core as C
import embeddings as EM
import freshness as FR
import indexer as IX
import note as NT
import patterns as PT
import recall as RC
import telemetry as TL

VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

TOOLS = [
    {"name": "recall",
     "description": "Search the vault memory before searching anywhere else. Returns the notes that "
                    "answer, with their freshness state and the passage that matches. Read the whole note "
                    "only if the passage is not enough.",
     "inputSchema": {"type": "object", "required": ["q"], "properties": {
         "q": {"type": "string", "description": "natural-language query (any language), an ID or a ticket"},
         "n": {"type": "integer", "default": 5},
         "type": {"type": "string"}, "area": {"type": "string"}, "status": {"type": "string"},
         "since": {"type": "string", "description": "YYYY-MM-DD"},
         "brief": {"type": "boolean", "default": True, "description": "short passage (saves tokens)"}}}},
    {"name": "read_passage",
     "description": "Read only the useful part of a note: the paragraph matching q, a section by its "
                    "title (H2), or the beginning. Cheaper than reading the whole note.",
     "inputSchema": {"type": "object", "required": ["p"], "properties": {
         "p": {"type": "string", "description": "path relative to the vault (field p of recall)"},
         "q": {"type": "string"}, "section": {"type": "string"},
         "width": {"type": "integer", "default": 800}}}},
    {"name": "write_note",
     "description": "Save memory following the vault contract. Before creating it looks for duplicates: "
                    "if it returns 'duplicates', read them and decide to update (update=path) or replace "
                    "(replaces=name). Never invent fields: if some are missing, it says which.",
     "inputSchema": {"type": "object", "properties": {
         "title": {"type": "string"}, "type": {"type": "string"}, "folder": {"type": "string"},
         "body": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}},
         "source": {"type": "string"}, "verified": {"type": "string", "description": "YYYY-MM-DD or today"},
         "replaces": {"type": "string"}, "key": {"type": "string"}, "expires": {"type": "string"},
         "fields": {"type": "object", "additionalProperties": {"type": "string"}},
         "update": {"type": "string", "description": "path of the note to update"},
         "append": {"type": "boolean"}, "force": {"type": "boolean"}}}},
    {"name": "doctor",
     "description": "Memory health: contract coverage, broken links and freshness (expired, replaced, "
                    "contradictions, near duplicates). Read only.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "flag_for_review",
     "description": "The user says a note may be wrong. Records why, without editing the note: it goes "
                    "first in the review queue until someone checks it at its source.",
     "inputSchema": {"type": "object", "required": ["p", "reason"], "properties": {
         "p": {"type": "string", "description": "path of the note (field p of recall)"},
         "reason": {"type": "string", "description": "one line: what was said against it and by whom"}}}},
    {"name": "save_answer",
     "description": "Keep an answer you gave from several notes as a new note: unverified, expiring unless "
                    "it is read, and queued for review. Call it only after the user said yes to saving it. "
                    "Every cite must be a current note (field p of recall), at least two by default.",
     "inputSchema": {"type": "object", "required": ["question", "answer", "cites"], "properties": {
         "question": {"type": "string", "description": "what the user asked"},
         "answer": {"type": "string", "description": "the answer as given, without the citations list"},
         "cites": {"type": "array", "items": {"type": "string"},
                   "description": "paths of the notes the answer came from (field p of recall)"}}}},
]

INSTRUCTIONS = ("Vault memory: call recall before searching elsewhere and answer from the notes, citing "
                "each one with how sure it is (verified date and confidence, or not verified). If nothing "
                "matches, say it is not in the memory. When the user disputes what a note says, weigh the "
                "evidence instead of giving in, and use flag_for_review so the note is checked at its source. "
                "Use write_note to save facts (it handles duplicates and freshness). After an answer that "
                "cites at least two notes you may offer once to save it; call save_answer only after the user "
                "says yes, never on your own. Reply to the user in their own language.")

HOW_SURE = ("For each claim cite the note (its path) and how sure it is: \"verified YYYY-MM-DD, N/10\" "
            "from its verified and confidence fields, or \"not verified\". A note whose source starts with "
            "\"distill\" was learned from past sessions by a model: say so. A note in state replaced, expired "
            "or outdated is not an answer: say what replaced it or that it is out of date.")

PROMPTS = {
    "ask": {
        "title": "Ask my memory",
        "description": "Answer a question only from the vault, citing each note and how verified it is.",
        "arg": ("question", "what you want to know"),
        "text": ("Answer from my vault memory only.\n\nQuestion: {value}\n\n"
                 "1. Call recall with 2 to 4 short queries in the words the notes would use (IDs and tickets "
                 "as they are). Use read_passage only when the passage is not enough.\n"
                 "2. Answer only with what the notes say. " + HOW_SURE + "\n"
                 "3. If nothing answers it, say it is not in my memory. Do not fill the gap with general "
                 "knowledge unless I ask, and then label it as not coming from my notes.\n"
                 "4. End with what is missing to be sure, if anything. Keep talking with me: follow-up "
                 "questions go through recall again.\n"
                 "5. If the answer cites at least two notes, offer once, in one short line, to save it. Call "
                 "save_answer only if I say yes.")},
    "discuss": {
        "title": "Discuss with my memory",
        "description": "Weigh a claim against what the vault says: evidence for and against, then a position.",
        "arg": ("claim", "what you claim, doubt or want to argue about"),
        "text": ("Let's discuss this against my vault memory.\n\nClaim: {value}\n\n"
                 "1. Call recall for the claim and for its opposite.\n"
                 "2. Lay out what the notes say, separating what supports the claim from what contradicts "
                 "it. " + HOW_SURE + "\n"
                 "3. Take a position on the evidence: the notes back the claim, contradict it, or do not "
                 "settle it. Do not give in because I insist; change your position only with new evidence, "
                 "and say which.\n"
                 "4. If I bring something that says a note is wrong, call flag_for_review with that note and "
                 "a one-line reason: the note is not edited, it goes first to be checked at its source. Save "
                 "a new fact with write_note only if I confirm it and say where it comes from.")},
}


def _recall(cfg, args):
    idx = RC.load(cfg)
    query = str(args.get("q", "")).strip()
    if not query:
        raise ValueError("q is missing")
    n = int(args.get("n", 5))
    # Semantic rerank of BM25's best candidates when embeddings are on (falls back silently).
    rerank = EM.enabled(cfg)
    results = RC.search(idx, query, note_type=args.get("type"), area=args.get("area"),
                        status=args.get("status"), since=args.get("since"),
                        n=max(n, int(EM.rerank_options(cfg)["candidates"])) if rerank else n)
    reranked = False
    if rerank:
        results, reranked = EM.rerank(cfg, query, results, origin="mcp")
    results = results[:n]
    query_terms = list(C.tokens(query, keep_stopwords=True))
    TL.record(cfg["_vault"], {"ev": "recall", "origin": "mcp", "q": query[:200], "n": len(results),
                              "top": round(results[0][0], 1) if results else 0,
                              "ps": [d["p"] for _, d in results[:3]],
                              **({"rerank": True} if reranked else {})}, cfg)
    width = 160 if args.get("brief", True) else 300
    return [{"score": round(s, 1), "p": d["p"], "t": d["t"], "type": d["type"], "date": d["date"],
             "status": d["status"], "state": FR.state(d), "replaced_by": d.get("replaced_by", ""),
             "verified": d.get("verified", ""), "confidence": d.get("confidence", ""),
             "checked": d.get("checked", ""), "source": d.get("source", ""),
             "passage": RC.passage(cfg, d, query_terms, width)} for s, d in results]


def _read(cfg, args):
    rel = str(args.get("p", "")).strip().lstrip("/")
    path = (cfg["_vault"] / rel).resolve()
    if (cfg["_vault"].resolve() not in path.parents or not path.is_file() or path.suffix != ".md"
            or IX.SENSITIVE.search(rel)):
        raise ValueError(f"path outside the vault or not allowed: {rel}")
    text = path.read_text(encoding="utf-8")
    _, body = C.frontmatter(text)
    TL.record(cfg["_vault"], {"ev": "read", "p": rel, "via": "mcp"}, cfg)
    width = int(args.get("width", 800))
    if args.get("section"):
        parts = re.split(r"(?m)^##\s+", body)
        wanted = C.norm(args["section"])
        for part in parts[1:]:
            if wanted in C.norm(part.split("\n", 1)[0]):
                return {"p": rel, "text": ("## " + part).strip()[:width]}
        return {"p": rel, "text": "", "notice": "there is no section with that title",
                "sections": [part.split("\n", 1)[0].strip() for part in parts[1:]]}
    if args.get("q"):
        query_terms = list(C.tokens(args["q"], keep_stopwords=True))
        draft = {"p": rel, "s": body.strip()[:width]}
        return {"p": rel, "text": RC.passage(cfg, draft, query_terms, width)}
    return {"p": rel, "text": body.strip()[:width]}


def _write(cfg, args):
    try:
        result = NT.write(cfg, title=args.get("title"), note_type=args.get("type"), folder=args.get("folder"),
                          body=args.get("body", ""), tags=args.get("tags"), source=args.get("source"),
                          verified=args.get("verified"), replaces=args.get("replaces"), key=args.get("key"),
                          expires=args.get("expires"), fields=args.get("fields"), update=args.get("update"),
                          append=bool(args.get("append")), force=bool(args.get("force")))
        TL.record(cfg["_vault"], {"ev": "note", "p": result["p"], "mode": result["mode"], "origin": "mcp"}, cfg)
        return result
    except NT.NoteError as e:
        return {"error": e.message, "code": e.code, **e.details}


def _doctor(cfg, args):
    docs, link_graph, coverage = IX.build(cfg, quiet=True, write=False)
    result = PT.build(cfg, docs, link_graph, coverage, write=False)
    report = FR.analyze({"docs": docs}, cfg)
    return {"health": result["health"], "freshness": {k: v[:15] for k, v in report.items()},
            "totals": {k: len(v) for k, v in report.items()}}


class InvalidParams(ValueError):
    """Bad arguments to a tool: answered as a JSON-RPC -32602 error, not as a tool result."""


def _save_answer(cfg, args):
    import answer as AN
    cites = args.get("cites")
    if isinstance(cites, str):
        cites = [c for c in cites.split(",") if c.strip()]
    if not isinstance(cites, list):
        raise InvalidParams("cites must be a list of note paths")
    try:
        return AN.save(cfg, str(args.get("question", "")), str(args.get("answer", "")),
                       [str(c) for c in cites], origin="mcp")
    except NT.NoteError as e:
        if e.code == 2:
            raise InvalidParams(e.message)
        return {"error": e.message, "code": e.code, **e.details}


def _flag(cfg, args):
    import review as RV
    try:
        return RV.flag(cfg, str(args.get("p", "")), str(args.get("reason", "")), origin="mcp")
    except NT.NoteError as e:
        return {"error": e.message, "code": e.code}


HANDLERS = {"recall": _recall, "read_passage": _read, "write_note": _write, "doctor": _doctor,
            "flag_for_review": _flag, "save_answer": _save_answer}


def prompt_list():
    return [{"name": name, "title": spec["title"], "description": spec["description"],
             "arguments": [{"name": spec["arg"][0], "description": spec["arg"][1], "required": True}]}
            for name, spec in PROMPTS.items()]


def prompt_get(params):
    spec = PROMPTS.get(params.get("name"))
    if not spec:
        raise KeyError(params.get("name"))
    value = str((params.get("arguments") or {}).get(spec["arg"][0], "")).strip()
    if not value:
        raise ValueError(f"{spec['arg'][0]} is missing")
    return {"description": spec["description"],
            "messages": [{"role": "user", "content": {"type": "text", "text": spec["text"].format(value=value)}}]}


def respond(cfg, message, version):
    method, message_id = message.get("method"), message.get("id")
    if message_id is None:                 # notification: no response
        return None
    if method == "initialize":
        requested = (message.get("params") or {}).get("protocolVersion")
        return {"protocolVersion": requested if requested in VERSIONS else VERSIONS[0],
                "capabilities": {"tools": {}, "prompts": {}},
                "serverInfo": {"name": "vault-memory", "version": version},
                "instructions": INSTRUCTIONS}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "prompts/list":
        return {"prompts": prompt_list()}
    if method == "prompts/get":
        return prompt_get(message.get("params") or {})
    if method == "tools/call":
        params = message.get("params") or {}
        handler = HANDLERS.get(params.get("name"))
        if not handler:
            raise KeyError(params.get("name"))
        try:
            payload = handler(cfg, params.get("arguments") or {})
            is_error = isinstance(payload, dict) and "error" in payload
        except InvalidParams:
            raise
        except Exception as e:             # a tool error is a result, not a server crash
            payload, is_error = {"error": str(e)}, True
        return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}], "isError": is_error}
    raise LookupError(method)


def serve(cfg, version, stdin=sys.stdin, stdout=sys.stdout):
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            stdout.write(json.dumps({"jsonrpc": "2.0", "id": None,
                                     "error": {"code": -32700, "message": "invalid JSON"}}) + "\n")
            stdout.flush()
            continue
        try:
            result = respond(cfg, message, version)
            if result is None:
                continue
            out = {"jsonrpc": "2.0", "id": message.get("id"), "result": result}
        except KeyError as e:              # before LookupError: KeyError is a subclass of it
            kind = "prompt" if message.get("method") == "prompts/get" else "tool"
            out = {"jsonrpc": "2.0", "id": message.get("id"), "error": {"code": -32602, "message": f"unknown {kind}: {e}"}}
        except ValueError as e:
            out = {"jsonrpc": "2.0", "id": message.get("id"), "error": {"code": -32602, "message": str(e)}}
        except LookupError as e:
            out = {"jsonrpc": "2.0", "id": message.get("id"), "error": {"code": -32601, "message": f"unknown method: {e}"}}
        stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        stdout.flush()
