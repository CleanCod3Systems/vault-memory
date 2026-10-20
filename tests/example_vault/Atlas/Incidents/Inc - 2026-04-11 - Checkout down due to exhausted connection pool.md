---
type: incident
area: support
tags: [incident, support]
date: 2026-04-11
status: resolved
severity: high
system: checkout
class: db-pool
ticket: ATL-118
---

# Inc - 2026-04-11 - Checkout down due to exhausted connection pool

Checkout went down with timeouts: the database connection pool was exhausted by a slow query. The pool was raised and the orders table was indexed.
