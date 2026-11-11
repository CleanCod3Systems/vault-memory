"""Sources: where distill reads the day's sessions from.

Each adapter takes `(cfg, day)` and yields raw sessions in one shape:
  {sid, cwd, day, prompts, messages, [origin, ref, url]}
- `day` is the local date of the first human prompt (a session belongs to that day).
- `prompts` are the human prompts (distill skips sessions with fewer than `min_prompts`).
- `messages` is [(role, text)]: only what a person wrote and the assistant's reply text;
  tool output and messages the agent injects itself never count.
- `origin`, `ref` and `url` say where a note came from (left out for Claude Code, whose
  notes keep the short session id as before).

Nothing here redacts or talks to a model: distill redacts the rendered text before any
provider sees it. Turned on with `distill.sources` (default `["claude-code"]`); an adapter
whose folder or command is missing yields nothing.
"""
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

DEFAULTS = {"sources": ["claude-code"], "gh_repos": []}
NOT_HUMAN = ("<task-notification", "<command-", "<local-command", "<system-reminder",
             "<agent-message", "<pasted_content")
# Codex and Gemini add their own context as user-role messages: AGENTS.md instructions and
# blocks such as <environment_context> or <session_context>. None of them is the person.
INJECTED = re.compile(r"^(?:<[A-Za-z][\w-]*>|# AGENTS\.md instructions)")
SOURCE_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,39}$")


class SourceError(ValueError):
    pass


def options(cfg):
    return {**DEFAULTS, **{k: v for k, v in (cfg.get("distill") or {}).items() if k in DEFAULTS}}


def render(messages):
    """The text a provider reads: one `[role] text` line per message."""
    return "\n".join(f"[{role}] {text}" for role, text in messages if text)


def _local_day(timestamp):
    try:
        moment = dt.datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.astimezone().date().isoformat()


def _near(day, stamp):
    """True when a file dated `stamp` (YYYY-MM-DD) can hold a session of `day`: the file date
    may be local or UTC, so one day either side. Unknown dates are kept."""
    try:
        gap = abs((dt.date.fromisoformat(stamp) - dt.date.fromisoformat(day)).days)
    except ValueError:
        return True
    return gap <= 1


def _turns_to_session(sid, cwd, day, turns, **extra):
    messages = []
    for prompt, reply in turns:
        messages.append(("user", prompt))
        if reply:
            messages.append(("assistant", reply))
    return {"sid": sid, "cwd": cwd, "day": day, "prompts": [t[0] for t in turns],
            "messages": messages, **extra}


def _agent_prompt(text):
    stripped = (text or "").strip()
    return bool(stripped) and not INJECTED.match(stripped)


# ───────────────────────────────────────────────────────────── Claude Code
def config_dir():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def _blocks_text(content):
    """Text of a message, or None when it carries tool results (not a human prompt)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            if block.get("type") == "tool_result":
                return None
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
    return "".join(parts)


def _is_human_prompt(event, text):
    if event.get("isMeta") or event.get("isSidechain") or text is None:
        return False
    origin = event.get("origin")
    if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
        return False
    stripped = text.strip()
    return bool(stripped) and not stripped.startswith(NOT_HUMAN)


def parse_transcript(path):
    """{sid, cwd, day, prompts, turns} of one transcript; turns = [(prompt, last assistant text)]."""
    sid, cwd, day = Path(path).stem, "", None
    turns, assistant_text = [], ""
    for line in Path(path).read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        cwd = cwd or event.get("cwd") or ""
        if event.get("sessionId") and not turns:
            sid = event["sessionId"]
        message = event.get("message") or {}
        if event.get("type") == "user":
            text = _blocks_text(message.get("content"))
            if _is_human_prompt(event, text):
                if turns:
                    turns[-1][1] = assistant_text
                assistant_text = ""
                day = day or _local_day(event.get("timestamp"))
                turns.append([text.strip(), ""])
        elif event.get("type") == "assistant" and turns and not event.get("isSidechain"):
            text = _blocks_text(message.get("content"))
            if text and text.strip():
                assistant_text = text.strip()
    if turns:
        turns[-1][1] = assistant_text
    return {"sid": sid, "cwd": cwd, "day": day, "prompts": [t[0] for t in turns], "turns": turns}


def claude_code(cfg, day):
    """$CLAUDE_CONFIG_DIR/projects/<project>/<session>.jsonl (subagent files live deeper)."""
    for path in sorted((config_dir() / "projects").glob("*/*.jsonl")):
        try:
            session = parse_transcript(path)
        except OSError:
            continue
        yield _turns_to_session(session["sid"], session["cwd"], session["day"], session["turns"])


# ───────────────────────────────────────────────────────────── Codex CLI
def codex_home():
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def _codex_text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(part.get("text", "") for part in content
                   if isinstance(part, dict) and part.get("type") in ("input_text", "output_text", "text"))


def parse_codex(path):
    """One rollout file: JSONL lines {timestamp, type, payload}. `session_meta` gives id and
    cwd; `response_item` messages with role user/assistant give the turns. Developer
    messages, tool calls, reasoning and injected context are left out; so are subagent and
    internal sessions."""
    sid, cwd, day = Path(path).stem, "", None
    turns, reply = [], ""
    for line in Path(path).read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = event.get("payload") if isinstance(event, dict) else None
        if not isinstance(payload, dict):
            continue
        if event.get("type") == "session_meta":
            source = payload.get("source")
            if isinstance(source, dict) and {"subagent", "internal"} & set(source):
                return None
            if not turns:
                sid = payload.get("id") or sid
            cwd = cwd or payload.get("cwd") or ""
        elif event.get("type") == "response_item" and payload.get("type") == "message":
            text = _codex_text(payload.get("content"))
            if payload.get("role") == "user" and _agent_prompt(text):
                if turns:
                    turns[-1][1] = reply
                reply = ""
                day = day or _local_day(event.get("timestamp"))
                turns.append([text.strip(), ""])
            elif payload.get("role") == "assistant" and turns and text.strip():
                reply = text.strip()
    if turns:
        turns[-1][1] = reply
    return _turns_to_session(sid, cwd, day, turns, origin="codex", ref=f"codex:{sid[:8]}")


def codex(cfg, day):
    """$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<time>-<id>.jsonl (folders in local time)."""
    for path in sorted((codex_home() / "sessions").glob("*/*/*/rollout-*.jsonl")):
        if not _near(day, "-".join(path.parts[-4:-1])):
            continue
        try:
            session = parse_codex(path)
        except OSError:
            continue
        if session:
            yield session


# ───────────────────────────────────────────────────────────── Gemini CLI
def _gemini_text(content):
    """Text of a PartListUnion, or None when it carries a tool response (not a prompt)."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        content = [content]
    if not isinstance(content, list):
        return ""
    parts = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict):
            if "functionResponse" in part:
                return None
            parts.append(part.get("text") or "")
    return "".join(parts)


def _gemini_records(raw):
    """(metadata, messages) of a chat file: JSONL replayed ($set, $rewindTo, $patch), or the
    legacy single JSON ConversationRecord."""
    try:
        record = json.loads(raw)
    except json.JSONDecodeError:
        record = None
    if isinstance(record, dict) and isinstance(record.get("messages"), list):
        return ({k: v for k, v in record.items() if k != "messages"},
                [m for m in record["messages"] if isinstance(m, dict)])
    meta, messages = {}, []
    for line in raw.splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        if "$set" in rec and isinstance(rec["$set"], dict):
            meta.update({k: v for k, v in rec["$set"].items() if k != "messages"})
            if isinstance(rec["$set"].get("messages"), list):
                messages = list(rec["$set"]["messages"])
        elif "$rewindTo" in rec:
            ids = [m.get("id") for m in messages]
            messages = messages[:ids.index(rec["$rewindTo"])] if rec["$rewindTo"] in ids else []
        elif "$patch" in rec and isinstance(rec["$patch"], dict):
            messages = _gemini_patch(messages, rec["$patch"])
        elif "type" in rec and "id" in rec:
            ids = [m.get("id") for m in messages]
            if rec["id"] in ids:
                messages[ids.index(rec["id"])] = rec
            else:
                messages.append(rec)
        elif "sessionId" in rec:
            meta.update(rec)
    return meta, messages


def _gemini_patch(messages, patch):
    by_id = {m.get("id"): m for m in messages}
    updates = list(patch.get("updates") or [])
    if patch.get("id"):
        updates.append(patch)
    for update in updates:
        if isinstance(update, dict) and update.get("id") in by_id and "content" in update:
            by_id[update["id"]] = {**by_id[update["id"]], "content": update["content"]}
    removed = set(patch.get("removeIds") or [])
    order = [i for i in patch.get("orderIds") or [] if i in by_id] or [m.get("id") for m in messages]
    order += [m.get("id") for m in messages if m.get("id") not in order]
    return [by_id[i] for i in order if i not in removed]


def parse_gemini(path):
    """One chat file under ~/.gemini/tmp/<project>/chats/. Messages of type `user` (what the
    person typed: displayContent when present) and `gemini` give the turns; info, error and
    warning messages, tool responses and injected context are left out; so are subagent chats."""
    path = Path(path)
    meta, messages = _gemini_records(path.read_text(encoding="utf-8", errors="ignore"))
    if meta.get("kind") == "subagent":
        return None
    sid = str(meta.get("sessionId") or path.stem)
    marker = path.parent.parent / ".project_root"
    try:
        cwd = marker.read_text(encoding="utf-8").strip()
    except OSError:
        cwd = next(iter(meta.get("directories") or []), "")
    day, turns, reply = None, [], ""
    for message in messages:
        if message.get("type") == "user":
            shown = message.get("displayContent")
            text = _gemini_text(shown if shown else message.get("content"))
            if text is not None and _agent_prompt(text):
                if turns:
                    turns[-1][1] = reply
                reply = ""
                day = day or _local_day(message.get("timestamp"))
                turns.append([text.strip(), ""])
        elif message.get("type") == "gemini" and turns:
            text = _gemini_text(message.get("content"))
            if text and text.strip():
                reply = text.strip()
    if turns:
        turns[-1][1] = reply
    return _turns_to_session(sid, str(cwd), day, turns, origin="gemini", ref=f"gemini:{sid[:8]}")


def gemini(cfg, day):
    """~/.gemini/tmp/<project>/chats/session-<time>-<id>.jsonl (or legacy .json)."""
    for path in sorted((Path.home() / ".gemini" / "tmp").glob("*/chats/session-*.json*")):
        if path.suffix not in (".json", ".jsonl") or not _near(day, path.name[8:18]):
            continue
        try:
            session = parse_gemini(path)
        except OSError:
            continue
        if session:
            yield session


# ───────────────────────────────────────────────────────────── GitHub PRs (gh CLI)
def _gh(args, timeout=60):
    try:
        proc = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def _pr_session(repo, pr, day):
    """A PR as a session: title, body, comments and review bodies. People appear by role
    (author, reviewer), never by login."""
    author = (pr.get("author") or {}).get("login")
    messages = [("pr", f"{repo}#{pr.get('number')} {pr.get('title') or ''}".strip())]
    if (pr.get("body") or "").strip():
        messages.append(("author", pr["body"].strip()))
    for item in sorted((pr.get("comments") or []) + (pr.get("reviews") or []),
                       key=lambda c: str(c.get("createdAt") or c.get("submittedAt") or "")):
        text = (item.get("body") or "").strip()
        if not text:
            continue
        role = "author" if (item.get("author") or {}).get("login") == author else "reviewer"
        messages.append((role, text))
    sid = f"{repo}#{pr.get('number')}"
    return {"sid": sid, "cwd": "", "day": day, "prompts": [t for _, t in messages], "messages": messages,
            "origin": "gh-prs", "ref": f"gh-prs:{sid}", "url": pr.get("url") or ""}


def gh_prs(cfg, day):
    """PRs the user authored or reviewed, updated on `day`, in `distill.gh_repos`. Needs the
    `gh` command, logged in; without it (or offline) it yields nothing."""
    repos = [r for r in options(cfg)["gh_repos"] if isinstance(r, str) and r.strip()]
    if not repos or not shutil.which("gh"):
        return
    scope = [arg for repo in repos for arg in ("--repo", repo)]
    found = {}
    for who in ("--author", "--reviewed-by"):
        for item in _gh(["search", "prs", who, "@me", "--updated", day, "--limit", "50",
                         "--json", "number,repository,url", *scope]) or []:
            repo = (item.get("repository") or {}).get("nameWithOwner")
            if repo and item.get("number"):
                found[(repo, int(item["number"]))] = item
    for repo, number in sorted(found):
        pr = _gh(["pr", "view", str(number), "--repo", repo,
                  "--json", "number,title,body,url,author,comments,reviews"])
        if isinstance(pr, dict):
            yield _pr_session(repo, pr, day)


ADAPTERS = {"claude-code": claude_code, "codex": codex, "gemini": gemini, "gh-prs": gh_prs}


def sessions(cfg, day):
    """Raw sessions of `day` from every source turned on, in config order. An unknown source
    name is an error now, not a silent empty day."""
    names = options(cfg)["sources"]
    names = [names] if isinstance(names, str) else list(names or [])
    unknown = [n for n in names if n not in ADAPTERS]
    if unknown:
        raise SourceError(f"unknown distill source {unknown[0]!r} (use {', '.join(ADAPTERS)})")
    return (session for name in dict.fromkeys(names) for session in ADAPTERS[name](cfg, day))


# ───────────────────────────────────────────────────────────── ingest: material an agent collected
def _clean(value, limit=200):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _role(value):
    return re.sub(r"[^a-z0-9_-]+", "-", str(value or "").lower()).strip("-")[:20] or "unknown"


def material(raw, source=None, url=None, ref_id=None, date=None, today=None):
    """A session from material collected elsewhere (a chat thread, an issue, a PR).

    JSON: {"source", "id", "url", "date", "title", "messages": [{"author_role", "text", "ts"}]}
    Anything else is read as plain Markdown (title = first `# ` heading). Flags win over the
    JSON fields. Raises ValueError when there is nothing to learn from."""
    data = None
    if raw.lstrip().startswith(("{", "[")):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"invalid JSON: {e}")
        if not isinstance(data, dict):
            raise ValueError("the JSON must be an object")
    if data is not None:
        title = _clean(data.get("title"))
        messages = []
        for item in data.get("messages") or []:
            if isinstance(item, dict) and isinstance(item.get("text"), str) and item["text"].strip():
                messages.append((_role(item.get("author_role")), item["text"].strip(), item.get("ts")))
        if not messages:
            raise ValueError("`messages` needs at least one item with `text`")
        stamps = [m[2] for m in messages if m[2]]
        source, url = source or data.get("source"), url or data.get("url")
        ref_id, date = ref_id or data.get("id"), date or data.get("date") or (stamps[0] if stamps else None)
        messages = [(role, text) for role, text, _ in messages]
    else:
        text = raw.strip()
        if not text:
            raise ValueError("the material is empty")
        heading = re.search(r"^#\s+(.+)$", text, re.M)
        title = _clean(heading.group(1)) if heading else ""
        messages = [("document", text)]
    source = _clean(source, 40).lower()
    if not SOURCE_NAME.match(source):
        raise ValueError("a source name is needed (lowercase, e.g. chat, issues, prs): --source or `source`")
    day = None
    if date:
        day = str(date)[:10] if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(date)) else _local_day(date)
        if not day:
            raise ValueError(f"unreadable date {date!r} (use YYYY-MM-DD or an ISO timestamp)")
    day = day or (today or dt.date.today()).isoformat()
    sid = re.sub(r"\s+", "-", _clean(ref_id, 80)) or _content_id(raw)
    if title:
        messages.insert(0, ("title", title))
    return {"sid": sid, "cwd": "", "day": day, "prompts": [t for _, t in messages], "messages": messages,
            "origin": source, "ref": f"{source}:{sid}", "url": _clean(url, 300)}


def _content_id(raw):
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
