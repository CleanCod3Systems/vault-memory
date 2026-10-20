---
type: incident
area: support
tags: [incident, support]
date: 2026-06-18
status: resolved
severity: high
system: payments
class: refund
ticket: ATL-160
---

# Inc - 2026-06-18 - Duplicate refund to customer

A retry of the refunds job paid back order 50112 twice. Idempotency per order was added.
