---
type: decision
area: product
tags: [decision, product]
date: 2026-02-10
status: current
---

# Decision - 2026-02-10 - Use queues for refunds

Refunds are processed through a queue with idempotent retries, never synchronously.
