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
]

INSTRUCTIONS = ("Vault memory: call recall before searching elsewhere; use write_note to save facts "
                "(it handles duplicates and freshness). Reply to the user in their own language.")


def _recall(cfg, args):
    idx = RC.load(cfg)
    query = str(args.get("q", "")).strip()
    if not query:
        raise ValueError("q is missing")
    results = RC.search(idx, query, note_type=args.get("type"), area=args.get("area"),
                        status=args.get("status"), since=args.get("since"), n=int(args.get("n", 5)))
    query_terms = list(C.tokens(query, keep_stopwords=True))
    TL.record(cfg["_vault"], {"ev": "recall", "origin": "mcp", "q": query[:200], "n": len(results),
                              "top": round(results[0][0], 1) if results else 0}, cfg)
    width = 160 if args.get("brief", True) else 300
    return [{"score": round(s, 1), "p": d["p"], "t": d["t"], "type": d["type"], "date": d["date"],
             "status": d["status"], "state": FR.state(d), "replaced_by": d.get("replaced_by", ""),
             "passage": RC.passage(cfg, d, query_terms, width)} for s, d in results]


def _read(cfg, args):
    rel = str(args.get("p", "")).strip().lstrip("/")
    path = (cfg["_vault"] / rel).resolve()
    if (cfg["_vault"].resolve() not in path.parents or not path.is_file() or path.suffix != ".md"
            or IX.SENSITIVE.search(rel)):
        raise ValueError(f"path outside the vault or not allowed: {rel}")
    text = path.read_text(encoding="utf-8")
    _, body = C.frontmatter(text)
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


HANDLERS = {"recall": _recall, "read_passage": _read, "write_note": _write, "doctor": _doctor}


def respond(cfg, message, version):
    method, message_id = message.get("method"), message.get("id")
    if message_id is None:                 # notification: no response
        return None
    if method == "initialize":
        requested = (message.get("params") or {}).get("protocolVersion")
        return {"protocolVersion": requested if requested in VERSIONS else VERSIONS[0],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "vault-memory", "version": version},
                "instructions": INSTRUCTIONS}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        params = message.get("params") or {}
        handler = HANDLERS.get(params.get("name"))
        if not handler:
            raise KeyError(params.get("name"))
        try:
            payload = handler(cfg, params.get("arguments") or {})
            is_error = isinstance(payload, dict) and "error" in payload
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
            out = {"jsonrpc": "2.0", "id": message.get("id"), "error": {"code": -32602, "message": f"unknown tool: {e}"}}
        except LookupError as e:
            out = {"jsonrpc": "2.0", "id": message.get("id"), "error": {"code": -32601, "message": f"unknown method: {e}"}}
        stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        stdout.flush()
