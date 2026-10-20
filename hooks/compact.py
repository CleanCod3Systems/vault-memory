#!/usr/bin/env python3
"""SessionStart(compact): after compaction, remind which vault notes were read in this
session (≤ 200 B), so they are not searched again. stdout is reinjected as context."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import core as C          # noqa: E402
import telemetry as TL    # noqa: E402


def main():
    vault = C.hook_vault()
    try:
        session_id = json.load(sys.stdin).get("session_id")
    except Exception:
        return
    if not vault or not session_id:
        return
    read_paths = []
    for event in TL.session_events(vault, session_id):
        if event.get("ev") == "read" and event.get("p") not in read_paths:
            read_paths.append(event["p"])
    if not read_paths:
        return
    text = "[vault] already read in this session: " + " · ".join(read_paths[-4:])
    print(text[:200])


if __name__ == "__main__":
    main()
