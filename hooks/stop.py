#!/usr/bin/env python3
"""Stop: propose (never write) saving or verifying memory, once per session and only with
evidence in that session's telemetry:
  - a vault search without results (gap: a note is missing), or
  - reading an expired or unverified note (confirm whether it is still true).
Turned off with `hook.stop_prompt: false` in the vault config. Respects `stop_hook_active`.
The message is in English; the model answers the user in the user's language."""
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import core as C          # noqa: E402
import freshness as FR    # noqa: E402
import recall as RC       # noqa: E402
import telemetry as TL    # noqa: E402


def states(vault, paths):
    """Freshness state of those notes, with the engine (same rule as recall/doctor)."""
    cfg = C.load_config(Path(vault))
    by_path = {d["p"]: d for d in RC.load(cfg, auto=False)["docs"]}
    return {p: FR.state(by_path[p]) for p in paths if p in by_path}


def main():
    vault = C.hook_vault()
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    session_id = payload.get("session_id")
    if not vault or not session_id or payload.get("stop_hook_active"):
        return
    try:
        cfg = C.read_config_file(Path(vault))
    except BaseException:
        return
    if not cfg or (cfg.get("hook") or {}).get("stop_prompt") is False:
        return
    events = TL.session_events(vault, session_id)
    if any(e.get("ev") == "stop" for e in events):
        return
    gaps = [e.get("q", "") for e in events if e.get("ev") == "recall" and not e.get("n") and e.get("q")]
    read_paths = list(dict.fromkeys(e["p"] for e in events if e.get("ev") == "read" and e.get("p")))
    try:
        stale = [p for p, s in states(vault, read_paths).items() if s in ("expired", "unverified")]
    except BaseException:
        stale = []
    if not gaps and not stale:
        return
    parts = []
    if gaps:
        parts.append(f"\"{gaps[-1][:60]}\" was searched and the vault had nothing: if it was found out in "
                     "this session, save it with `vault-memory note --title … --type … --folder … --body … --source …`")
    if stale:
        parts.append(f"{stale[0]} was read (expired or unverified): if it is still true, "
                     f"`vault-memory note --update \"{stale[0]}\" --verified today`; if it changed, update it")
    TL.record(vault, {"ev": "stop", "gaps": len(gaps), "stale": len(stale)}, cfg, session_id)
    print(json.dumps({"decision": "block",
                      "reason": "[vault] Before finishing: " + "; ".join(parts) +
                                ". If it is not worth it, finish without doing anything."}, ensure_ascii=False))


if __name__ == "__main__":
    main()
