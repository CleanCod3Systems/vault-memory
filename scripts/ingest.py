"""vault-memory ingest — learn from material an agent collected (a chat thread, an issue, a PR).

vault-memory never talks to those systems: an agent with its own tools (MCP or CLI) collects
the material and hands it over as JSON or Markdown (sources.material). From there it is the
distill pipeline: redact → propose → evidence and support checks → inbox (`auto: false`) or
`note.write` with an expiry (`auto: true`). The provider only ever sees the redacted text, and
each candidate records where it came from (`source: distill <day> <source>:<id>`, `origin`, `url`).
"""
import distill as DS
import sources as SR
import telemetry as TL


def run(cfg, session, provider=None, dry_run=False):
    """Counts of one ingested session (same keys as a distill day, plus `source`)."""
    opts = DS.options(cfg)
    # same gate as distill: `auto: "earned"` writes notes only while audits back it
    decision = DS.CB.autonomy(cfg)
    opts = {**opts, "auto": decision["auto"]}
    day = session["day"]
    session["url"] = DS.redact(session.get("url") or "")
    session["sid"], session["ref"] = DS.redact(session["sid"]), DS.redact(session["ref"])
    session["text"] = DS.redact(SR.render(session.pop("messages")))[-int(opts["max_chars"]):]
    counts = {"day": day, "source": session["ref"], **{k: 0 for k in DS.COUNTS}, "discarded_reasons": {}}
    counts["sessions"] = 1
    counts["write"] = {"to": "notes" if decision["auto"] else "inbox", "reason": decision["reason"]}
    DS._process(cfg, opts, session, day, provider or DS.get_provider(cfg), dry_run, counts)
    if not dry_run:
        TL.record(cfg["_vault"], {"ev": "ingest", "day": day, "origin": session["origin"],
                                  **{k: counts[k] for k in DS.COUNTS if k != "errors"}}, cfg)
    return counts
