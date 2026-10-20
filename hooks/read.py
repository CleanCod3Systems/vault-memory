#!/usr/bin/env python3
"""PostToolUse(Read): records which vault note was read. With that `vault-memory usage`
measures whether hook pointers are used and which notes are read without being suggested."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import core as C          # noqa: E402
import telemetry as TL    # noqa: E402


def main():
    vault = C.hook_vault()
    if not vault:
        return
    try:
        payload = json.load(sys.stdin)
        path = Path(payload.get("tool_input", {}).get("file_path", "")).resolve()
        base = Path(vault).resolve()
        if path.suffix != ".md" or base not in path.parents:
            return
        cfg = C.read_config_file(base)
    except BaseException:
        return
    if not cfg:
        return
    TL.record(vault, {"ev": "read", "p": path.relative_to(base).as_posix()}, cfg, payload.get("session_id"))


if __name__ == "__main__":
    main()
