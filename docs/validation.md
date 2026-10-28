# Validation — 28 October 2026

Source extraction: Risk personal repository 0e2d41b (v1168).
Runtime verified: Python 3.13, PostgreSQL isolated local cluster, psycopg 3.3.6, FastAPI 0.141.1, httpx 0.28.1, Edge with Playwright 1.62.1.

- 48 standalone Python tests passed, plus the added affected-row import regression (49 total).
- PostgreSQL coverage: durable prices/catalog across app restart, gap-only retrieval, batching, explicit zero/null handling, corrections, provisional rollover, partial retries, predecessor weekends/holidays/long suspension, bad ticker isolation, serialized overlapping threads and separate processes, transaction rollback.
- HTTP system test: fake central data-agent endpoint contract through app API into PostgreSQL, then app restart uses stored history with no repeat provider call.
- Actual-file browser regression passed: math, autocomplete/stale request handling, cache fallback, paste, charts/modes/horizons, percentile summary/predecessor, missing history, provider outage, poll/cancel/clear, mobile, local restore, 100+1 request splitting.
- Independent review: fixed market-sector ticker coverage and explicit import failure/affected-row reporting.
- Risk removal: server syntax, four inline JS script blocks, 14 strike release checks, Positions combobox and render-race checks passed.

No production provider calls or real prices/credentials were used. Docker Compose structure/volume/loopback configuration checked; Docker containers and public website deployment were not run on this desktop. Environment credentials and a persistent PostgreSQL volume must be configured on the future host.


## Bloomberg name-search update

- Reused ES central instrument-search contract and yellow-key normalization with all-asset results, preserving provider ranking.
- Removed blank/one-character searches and the default saved SPX popup. No historical prices deleted.
- 92 backend tests passed against isolated PostgreSQL schemas; full browser suite passed including blank focus/no requests, NVDA results, selection clearing and company-name-versus-ticker ambiguity.
- Review findings fixed and re-reviewed: reset stale keyboard selection; prefer Bloomberg results over lexical literal candidates.
- Live verification: NVDA and NVIDIA CORP return real NVDA listings; blank query returns no suggestions; repeat lookup is PostgreSQL cached.
