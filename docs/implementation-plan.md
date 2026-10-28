# Rolling extraction Implementation Plan

Goal: standalone price analysis with PostgreSQL and central data-agent access.
Architecture: static vanilla JS UI + FastAPI same-origin server + PostgreSQL cache/job service + server-only JCAP client. Extract existing reviewed math/cache code; adapt boundaries rather than rewrite calculations.
Spec: .scratch/rolling-standalone/spec.md

1. Backend (server files, tests/test_backend.py): port price_history.py SQL to psycopg, persist coverage, advisory-lock fill, retain predecessor logic. Add /api/history (old query/result shape with dates,last), /api/instruments?q= (items ticker,name), /health. Validate dates/tickers. Config only env DATABASE_URL, JCAP_SERVICE_KEY, JCAP_DATA_URL, APP_USERNAME, APP_PASSWORD. Dependency-inject provider/store in create_app for isolated tests. Mock provider and verify stored reopen, batching, partials, provisional, predecessor, errors.
2. Frontend (static/*, tests/test_frontend.js): extract pane and CSS/chart library. Remove HISTORY, Positions and jcapStore dependencies. Standalone cached catalog search endpoint, localStorage settings, /api/history polling, init on load; preserve original math and UI. Verify multi-ticker, charts, summaries, search, mode, cancel and mobile.
3. Risk removal (separate checkout): exact tagged blocks and routes only, preserve mixed line endings and data. Verify source syntax and unaffected release tests. Save source patch with base metadata.
4. Integrate: requirements, Dockerfile/Compose PostgreSQL volume and healthcheck, .env.example, gitignore, README provenance, migrations/backup and reverse-proxy instructions. Run isolated PostgreSQL-backed HTTP tests + browser suite, review, secret/artifact scan. Move finished standalone project under Desktop/Agg/Equity Risk/Rolling Analysis and push initial main to requested new repository. Report runtime/deployment separately.
