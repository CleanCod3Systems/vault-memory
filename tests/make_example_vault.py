"""Builds tests/example_vault: a synthetic, neutral vault (fictional "Atlas" project).

Deterministic: same notes, same content, always. The result is committed so tests do not
depend on this script; running it again is only needed when adding cases. It carries the
freshness cases (replaces, expires, repeated key, near duplicate).

Usage: python3 tests/make_example_vault.py
"""
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "example_vault"

CONFIG = {
    "modules": {"contract": True, "graph": True, "patterns": True, "panel": False, "bases": False,
                "templates": False, "tickets": True, "chat": False, "ids": True, "schedule": False},
    "vault": "example_vault",
    "areas": {"Atlas/Incidents": "support", "Atlas/Runbooks": "support", "Atlas/Decisions": "product",
              "Atlas/Docs": "engineering", "Atlas/Daily": "log", "Memory": "memory"},
    "default_area": "general",
    "types_by_folder": {"Atlas/Incidents": "incident", "Atlas/Runbooks": "runbook",
                        "Atlas/Decisions": "decision", "Atlas/Docs": "doc", "Atlas/Daily": "daily",
                        "Templates": "template", "Memory": "doc"},
    "references": {"tickets": {"prefixes": ["ATL"], "url": "https://tickets.example/ATL-{n}"},
                   "chat": {"domain": ""}, "ids": {"words": ["order", "customer", "case"]}},
    "language": {"stemmer": "en"},
    "recall": {"skip_paths": ["Atlas/Daily/"]},
}

# (folder, name, extra frontmatter, body)
INC = "Atlas/Incidents"
NOTES = [
    ("", "Home", {"type": "home"}, "Front page of the Atlas vault. See [[System map]]."),
    (INC, "Inc - 2026-03-02 - Checkout down due to expired certificate", {"date": "2026-03-02", "status": "resolved", "severity": "high", "system": "checkout", "class": "certificate", "ticket": "ATL-101"},
     "Checkout returned 502 because the load balancer TLS certificate expired. It was renewed and an alert was added.\nRunbook: [[Runbook - Renew TLS certificate]]."),
    (INC, "Inc - 2026-04-11 - Checkout down due to exhausted connection pool", {"date": "2026-04-11", "status": "resolved", "severity": "high", "system": "checkout", "class": "db-pool", "ticket": "ATL-118"},
     "Checkout went down with timeouts: the database connection pool was exhausted by a slow query. The pool was raised and the orders table was indexed."),
    (INC, "Inc - 2026-05-20 - Payments declined by antifraud", {"date": "2026-05-20", "status": "open", "severity": "medium", "system": "payments", "class": "antifraud", "ticket": "ATL-140"},
     "Several card payments were declined by the amount antifraud rule. Order 48213 and 48220. Threshold adjustment pending."),
    (INC, "Inc - 2026-06-03 - Confirmation emails not arriving", {"date": "2026-06-03", "status": "resolved", "severity": "medium", "system": "notifications", "class": "smtp", "ticket": "ATL-152"},
     "Order confirmation emails were not being sent: the SMTP credential rotated and the service did not reload it."),
    (INC, "Inc - 2026-06-18 - Duplicate refund to customer", {"date": "2026-06-18", "status": "resolved", "severity": "high", "system": "payments", "class": "refund", "ticket": "ATL-160"},
     "A retry of the refunds job paid back order 50112 twice. Idempotency per order was added."),
    (INC, "Inc - 2026-07-07 - Slow product search", {"date": "2026-07-07", "status": "escalated", "severity": "low", "system": "catalog", "class": "performance", "ticket": "ATL-171"},
     "Product search takes more than 4 seconds with combined filters. Escalated to the catalog team."),
    (INC, "Inc - 2026-07-30 - Negative stock in inventory", {"date": "2026-07-30", "status": "open", "severity": "medium", "system": "inventory", "class": "concurrency", "ticket": "ATL-180"},
     "Two simultaneous sales of the last item left stock at -1. An optimistic lock is missing."),
    (INC, "Inc - 2026-08-12 - Login fails with SSO", {"date": "2026-08-12", "status": "resolved", "severity": "high", "system": "auth", "class": "sso", "ticket": "ATL-190"},
     "Signing in with SSO failed because of clock skew on the identity server. NTP was synchronized."),
    (INC, "Inc - 2026-09-01 - Checkout down after deploy without migration", {"date": "2026-09-01", "status": "resolved", "severity": "high", "system": "checkout", "class": "deploy", "ticket": "ATL-201"},
     "A deploy went out without running the orders table migration and checkout went down. The step was added to the pipeline."),
    ("Atlas/Runbooks", "Runbook - Renew TLS certificate", {"status": "current", "system": "checkout"},
     "Steps to renew the load balancer TLS certificate: generate the CSR, upload it to the manager, reload."),
    ("Atlas/Runbooks", "Runbook - Restart the payments service", {"status": "current", "system": "payments"},
     "If the payments service answers 503: check the queue, restart it with the start script and verify the health check."),
    ("Atlas/Runbooks", "Runbook - Diagnose declined payments", {"status": "current", "system": "payments"},
     "For a declined payment: look for the real reason in the processor log, not in the generic message shown to the customer. Never suggest retrying on antifraud."),
    ("Atlas/Runbooks", "Runbook - Rotate SMTP credentials", {"status": "current", "system": "notifications"},
     "Rotate the SMTP credential in the secrets manager and restart the notifications service so it reloads it."),
    ("Atlas/Decisions", "Decision - 2026-02-10 - Use queues for refunds", {"date": "2026-02-10", "status": "current"},
     "Refunds are processed through a queue with idempotent retries, never synchronously."),
    ("Atlas/Decisions", "Decision - 2026-05-02 - Antifraud threshold by amount", {"date": "2026-05-02", "status": "current"},
     "The antifraud amount threshold is 900 per order for new customers."),
    ("Atlas/Decisions", "Decision - 2026-08-20 - Feature flags for deploys", {"date": "2026-08-20", "status": "current"},
     "Every checkout change ships behind a feature flag and with the migration run first."),
    ("Atlas/Docs", "System map", {},
     "Atlas systems: checkout, payments, catalog, inventory, notifications, auth. Each one has an owner and a runbook."),
    ("Atlas/Docs", "Checkout architecture", {},
     "Checkout builds the cart, reserves stock in inventory, charges in payments and triggers the email confirmation."),
    ("Atlas/Docs", "Orders glossary", {},
     "Purchase: a confirmed buy. Order: an instruction to the processor. Refund: full or partial return of the charge."),
    ("Atlas/Docs", "Code style guide", {},
     "Full names with units, short functions, comments that explain why."),
    ("Atlas/Docs", "Team onboarding", {},
     "First day: access, reading the system map and a shadowed on-call shift."),
    ("Atlas/Daily", "2026-09", {"date": "2026-09-01"},
     "September log. " + " ".join(["checkout payments refund stock login search deploy"] * 60)),
    ("Templates", "Template - Incident", {"type": "template"}, "# {{title}}\nSymptom, cause, solution."),
    ("_archive", "Old manual refunds process", {"type": "doc"}, "Obsolete: manual refunds are no longer done."),
    # ─── freshness cases ───
    ("Memory", "Retry policy v2", {"key": "retry-policy", "replaces": "[[Retry policy v1]]", "verified": "2026-09-10", "source": "architecture decision"},
     "Payment retries are 3 with exponential backoff and jitter."),
    ("Memory", "Retry policy v1", {"key": "retry-policy", "status": "replaced"},
     "Payment retries are 5 with a fixed interval of 10 seconds."),
    ("Memory", "Temporary shipping provider", {"expires": "2026-06-30", "source": "commercial agreement"},
     "Until the end of June shipments go out through the temporary provider."),
    ("Memory", "On-call schedule - team A", {"key": "on-call-schedule", "verified": "2026-08-01"},
     "Team A on-call covers 8 to 20 h."),
    ("Memory", "On-call schedule - team B", {"key": "on-call-schedule", "verified": "2026-08-15"},
     "On-call covers 9 to 21 h."),
    ("Memory", "How to request production access", {},
     "To request production access: open a ticket to the platform team with a justification and a duration."),
    ("Memory", "Request production access", {},
     "To request production access you have to open a ticket to the platform team with the justification and the duration."),
]


def by_prefix(table, path, default):
    keys = [k for k in table if path == k or path.startswith(k + "/")]
    return table[max(keys, key=len)] if keys else default


def frontmatter(folder, extra):
    # The full contract from the config: the example vault starts at 100 %.
    note_type = extra.get("type") or by_prefix(CONFIG["types_by_folder"], folder, "note")
    area = by_prefix(CONFIG["areas"], folder, CONFIG["default_area"])
    fields = {"type": note_type, "area": area, "tags": f"[{note_type}, {area}]", **extra}
    return "---\n" + "".join(f"{k}: {v}\n" for k, v in fields.items()) + "---\n"


def main():
    if ROOT.exists():
        shutil.rmtree(ROOT)
    (ROOT / ".vault-memory").mkdir(parents=True)
    (ROOT / ".obsidian").mkdir()
    (ROOT / ".vault-memory" / "config.json").write_text(json.dumps(CONFIG, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for folder, name, extra, body in NOTES:
        target = ROOT / folder / f"{name}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(frontmatter(folder, extra) + f"\n# {name}\n\n{body}\n", encoding="utf-8")
    # A sensitive file that must never be indexed.
    (ROOT / "Atlas" / ".env").write_text("TOKEN=do-not-index\n", encoding="utf-8")
    print(f"{len(NOTES)} notes in {ROOT}")


if __name__ == "__main__":
    main()
