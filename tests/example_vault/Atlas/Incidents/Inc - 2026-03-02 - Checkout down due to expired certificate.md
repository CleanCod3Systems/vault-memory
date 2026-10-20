---
type: incident
area: support
tags: [incident, support]
date: 2026-03-02
status: resolved
severity: high
system: checkout
class: certificate
ticket: ATL-101
---

# Inc - 2026-03-02 - Checkout down due to expired certificate

Checkout returned 502 because the load balancer TLS certificate expired. It was renewed and an alert was added.
Runbook: [[Runbook - Renew TLS certificate]].
