# Rolling Change Analysis

Standalone extraction of the Risk System Rolling Analysis tab. Multiple Bloomberg tickers, closing prices, two configurable rolling charts, percentage/absolute changes, date presets/custom ranges, and 1M/3M/6M/12M latest-change percentile tables.

## Run

Requires Docker Compose, or Python 3.13+ and PostgreSQL. Copy `.env.example` to `.env` and set `POSTGRES_PASSWORD`, `JCAP_SERVICE_KEY`, `APP_USERNAME` and `APP_PASSWORD`. Use a URL-safe database password for the Compose connection string. Never put the central service key in browser code.

```sh
docker compose up --build -d
```

Open http://127.0.0.1:8798 and sign in with the configured app credentials. PostgreSQL stays on the private Compose network. Its named `price_history` volume survives app/container restarts. `docker compose down` keeps the volume; do not add `--volumes` unless intentionally deleting stored history.

For native Python, create a dedicated PostgreSQL database and set environment variables from `.env.example` in your shell. `.env` is read by Compose; native Python uses exported environment variables.

```sh
python -m venv .venv
# Activate .venv, then:
pip install -r requirements.txt
uvicorn app:create_app --factory --host 127.0.0.1 --port 8798 --workers 1
```

## Price data and storage

The server consumes the JCAP central data pool (`JCAP_DATA_URL`, `X-Service-Key` from `JCAP_SERVICE_KEY`). It requests daily `PX_LAST` via `/api/v1/bloomberg/bdh`; a local Bloomberg Terminal and the Risk server are not required. The key remains on the server. The website uses only same-origin app APIs.

Saved observations and fetch coverage are durable PostgreSQL tables. A request returns saved data immediately and a polling job when coverage needs filling. Only gaps are sent upstream, at most 12 tickers per provider batch and 100 per browser request. Overlapping fetches are serialized. Missing/null values are not zero, explicit zero is retained, and provider failures preserve historical prices. Incomplete historical responses are retried after six hours; provisional/current-day coverage after five minutes. Finalized complete history is reused. The previous available close at a weekend/holiday boundary is included; predecessor searches expand up to 4,000 days for long suspensions.

The ticker dropdown searches the app's saved instrument catalogue. Enter or paste full Bloomberg tickers to add instruments that are not yet stored. Browser selections/settings are saved locally on that browser; they are not portfolio positions and do not require Risk data.

## Calculations

Rolling windows use calendar dates with month-end clamping. For each actual close, the baseline is the last available close on or before the lookback date. Percentage change = `(price / baseline - 1) * 100`; absolute change = `price - baseline`. A missing baseline or zero percentage denominator produces a gap. Prices/absolute changes use native instrument units; no FX conversion is implied.

Percentiles rank valid rolling changes inside the selected displayed period, including the latest observation. Ties use average ranks. Fewer than two valid samples show a dash. Red is the bottom decile; green the top decile. Each table shows its actual latest observation date. All shows up to 2,600 calendar days; custom ranges including warm-up are limited to 4,000 days.

## Import saved history

Use a copy of an existing `bbg_history_cache.json` or `bbg_field_history__PX_LAST.json`. Imports read the file and preserve newer database observations; they never call the data agent. Real caches are private data and must not be committed.

```sh
python tools/import_history.py /private/path/bbg_history_cache.json
```

Set `DATABASE_URL` before running. New installations start with an empty database unless history is imported or requested from the agent.

## Backups

Create PostgreSQL-consistent backups rather than copying live database files:

```sh
docker compose exec -T db pg_dump -U rolling -d rolling -Fc > rolling-history.dump
# Restore into a separately created recovery database, then verify row counts/date coverage:
pg_restore --no-owner --dbname="$RECOVERY_DATABASE_URL" rolling-history.dump
```

Use a binary-safe redirect for custom-format dumps (PowerShell 7.4+, bash, or `pg_dump -f` inside the container followed by `docker compose cp`). Keep backups outside Git and test restores before a host migration. No production history has been copied into this repository.

## Website integration

The future website can reverse-proxy this app at its own dedicated origin. Forward ordinary cookies/Authorization, preserve the `/api/` paths and use HTTPS. Keep app authentication enabled or replace it with the host site's trusted server-side auth integration. The central data-agent key is never sent to users. Compose binds loopback; this repository does not create a public tunnel or deploy to production.

## Validation

```sh
# Use a dedicated disposable database: tests may clear its app tables.
# Set TEST_DATABASE_URL to that database, then:
pip install -r requirements-dev.txt
python -m pytest tests -q
npm ci
npx playwright install chromium
node tests/test_frontend.js
```

The browser tests require Playwright and an Edge/Chromium browser. Tests use synthetic prices and a fake provider; no production Bloomberg or Enfusion calls are required.

## Source extraction

Extracted from `vascovalla/Risk-System` commit `0e2d41b` (v1168). The calendar math and prior-close fixes are retained. See `migration/` for a separately reviewable Risk source-removal patch. Existing Risk caches/databases are not deleted. The new repository is independent; it contains no Risk server or credentials.
