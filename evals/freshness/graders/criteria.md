---
type: llm
---
PASS if it answers 3 retries with exponential backoff (and jitter) per the current policy (v2). If it mentions v1 (5 retries every 10 s) it must say it was replaced. FAIL if it answers 5 retries with a fixed interval as if it were current.
