#!/usr/bin/env python3
"""SessionStart: once per local day, hand Claude the daily summary (next, waiting, due soon).

Acts only when the session `source` is `startup`, `CLAUDE_CODE_ENTRYPOINT` is in
`today.entrypoints`, `VAULT_MEMORY_DISABLE` is not 1, `today.type` is configured and the
summary was not already shown today (state file `<state>/today-shown` holds the date).
stdout is injected as context. Any error is swallowed: a session never waits on this.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import core as C          # noqa: E402
import telemetry as TL    # noqa: E402
import today as TD        # noqa: E402

INSTRUCTION = "Show this summary to the user at the start of your first reply, in their language."


def main():
    if os.environ.get("VAULT_MEMORY_DISABLE") == "1":
        return
    vault = C.hook_vault()
    try:
        payload = json.load(sys.stdin)
        if not vault or not isinstance(payload, dict) or payload.get("source") != "startup":
            return
        vault = Path(vault)
        cfg = C.read_config_file(vault)
        opts = TD.options(cfg)
        entry = os.environ.get("CLAUDE_CODE_ENTRYPOINT", "")
        if not opts["type"] or not entry or entry not in opts["entrypoints"]:
            return
        cfg = C.load_config(vault)
        today = C.today()
        marker = cfg["_state"] / "today-shown"
        try:
            if marker.read_text(encoding="utf-8").strip() == today.isoformat():
                return
        except OSError:
            pass
        text = TD.render(cfg, today=today)
        if not text or text == "today: nothing open":
            return
        lines = text.split("\n")
        print("[vault] " + "\n".join(lines))
        print(INSTRUCTION)
        marker.write_text(today.isoformat() + "\n", encoding="utf-8")
        TL.record(vault, {"ev": "today", "n": len(lines)}, cfg, payload.get("session_id"))
    except BaseException:
        return


if __name__ == "__main__":
    main()
