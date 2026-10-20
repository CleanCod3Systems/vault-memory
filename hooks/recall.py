#!/usr/bin/env python3
"""UserPromptSubmit: query the local vault index and emit pointers only.

It does not inject notes: only pointers, plus a short passage of the top-1 when the answer is
clear (`hook.brief_floor`), to answer without opening the note. No hits, no output. Output is
JSON `hookSpecificOutput.additionalContext` (the documented shape).

Vault: core.hook_vault() — the profile settings.json env > VAULT_MEMORY_VAULT > plugin option.
Without a vault the hook does nothing. What is searched and what is emitted: scripts/hookfilter.py
(the same thing `vault-memory eval --hook` measures). Within a session it does not repeat a note
already suggested `hook.repeat_max` times without anyone reading it, nor the previous pointer set.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
CLI = SCRIPTS / "cli.py"
sys.path.insert(0, str(SCRIPTS))
import core as C          # noqa: E402
import hookfilter as HF   # noqa: E402
import telemetry as TL    # noqa: E402


def repeated(events, repeat_max):
    """Notes suggested `repeat_max` times or more in the session without a later read, and the
    set of the last pointer."""
    counts, last = {}, []
    for event in events:
        if event.get("ev") in ("hook", "hook_brief"):
            last = event.get("emitted", [])
            for path in last:
                counts[path] = counts.get(path, 0) + 1
        elif event.get("ev") == "read":
            counts.pop(event.get("p"), None)
    return {p for p, n in counts.items() if repeat_max and n >= repeat_max}, last


def main():
    vault = C.hook_vault()
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    if not vault:
        return
    prompt, session_id = payload.get("prompt", ""), payload.get("session_id")
    try:
        cfg = C.read_config_file(Path(vault))
    except BaseException:
        return
    if not cfg:
        return
    hook = HF.options(cfg)

    def silent(reason):
        if TL.options(cfg)["log_silent"] and not prompt.startswith(("/", "!")):
            TL.record(vault, {"ev": "hook_silent", "reason": reason, "q": prompt[:200]}, cfg, session_id)

    should_search, memory_only, reason = HF.decide(prompt, cfg)
    if not should_search:
        return silent(reason)
    try:
        # Without --no-auto: the fingerprint check is cheap and avoids answering from a stale index.
        raw = subprocess.run([sys.executable, str(CLI), "--vault", vault, "recall", prompt,
                              "--json", "--n", "12"], capture_output=True, text=True, timeout=8,
                             env={**os.environ, "VAULT_MEMORY_ORIGIN": "hook"}).stdout
        rows = json.loads(raw)
    except Exception:
        return

    exclude, last = (repeated(TL.session_events(vault, session_id), hook["repeat_max"])
                     if session_id else (set(), []))
    hits, reason = HF.select(rows, prompt, cfg, memory_only, exclude)
    if not hits:
        return silent(reason)
    if [r["p"] for r in hits] == last:
        return silent("repeated")
    text = HF.pointer(hits, hook["format"])
    passage = HF.brief_passage(hits, hook)
    if passage:
        text += f"\n  ↳ {passage}"
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                             "additionalContext": text}}, ensure_ascii=False))
    TL.record(vault, {"ev": "hook_brief" if passage else "hook", "q": prompt[:200],
                      "emitted": [r["p"] for r in hits], "top": round(hits[0]["score"], 1)}, cfg, session_id)


if __name__ == "__main__":
    main()
