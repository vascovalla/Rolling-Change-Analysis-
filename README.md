# Rolling Change Analysis

Standalone extraction of the Risk System Rolling Analysis tab. Multiple Bloomberg tickers, closing prices, two configurable rolling charts, percentage/absolute changes, date presets/custom ranges, and 1M/3M/6M/12M latest-change percentile tables.

## Run

Requires Docker Compose, or Python 3.13+ and PostgreSQL. Copy `.env.example` to `.env` and set `POSTGRES_PASSWORD`, `JCAP_SERVICE_KEY`, `APP_USERNAME` and `APP_PASSWORD`. Use a URL-safe database password for the Compose connection string. Never put the central service key in browser code.

```sh
docker compose up --build -d
```

Open http://127.0.0.1:8798 and sign in with the configured app credentials. To run a deliberately public site without login, set `ALLOW_PUBLIC_ACCESS=1` and leave both `APP_USERNAME` and `APP_PASSWORD` empty. Public access is disabled by default. PostgreSQL stays on the private Compose network. Its named `price_history` volume survives app/container restarts. `docker compose down` keeps the volume; do not add `--volumes` unless intentionally deleting stored history.

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

The ticker dropdown stays closed when empty and searches after at least two characters. Company names and symbols use the same Bloomberg instrument-search contract and yellow-key normalization as ES, through the central data agent. Results keep Bloomberg's ranking so you can select the listing and market. Lookup results and verified descriptions are cached in PostgreSQL (24 hours for matches, 5 minutes for empty results); stored results remain available if the agent is unavailable. Enter or paste full Bloomberg tickers directly when needed. Browser selections/settings are saved locally on that browser; they are not portfolio positions and do not require Risk data.

## Price overlay scale

Use the controls beside the Price chart: **Price**, **Rebase 100**, **% Change**, **Z-score**. One selected instrument defaults to native price; multiple instruments default to Rebase 100. An explicit choice is saved in this browser.

Rebase 100 and % Change use the earliest shared actual closing date with positive baseline prices inside the displayed range. Both a $1,000-to-$1,100 move and a $10-to-$11 move become 100-to-110 (or 0%-to-10%). Points before the shared start stay gaps. Instruments without a positive starting price remain gaps; if the available instruments have no common start, the chart explains why normalization is unavailable.

Z-score uses the trailing 60 actual closes, including the current close, and population standard deviation: `(price - mean) / SD`. It never uses future prices. Fewer than 60 observations or zero SD produce gaps. The price tooltip keeps the native price alongside its scaled value. Scale changes use loaded prices, with no extra provider request. Rolling-change charts and summary calculations remain independent; their default mode is percentage change.

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

The future website can reverse-proxy this app at its own dedicated origin. Forward ordinary cookies/Authorization, preserve the `/api/` paths and use HTTPS. Use the configured authentication policy, or explicitly set `ALLOW_PUBLIC_ACCESS=1` for a public site. The central data-agent key is never sent to users. Compose binds loopback; this repository does not create a public tunnel or deploy to production.

## Validation

```sh
# Use a dedicated disposable database: tests may clear its app tables.
# Set TEST_DATABASE_URL to that database, then:
pip install -r requirements-dev.txt
python -m pytest tests -q
npm ci
npx playwright install chromium
npm test
```

The browser tests require Playwright and an Edge/Chromium browser. Tests use synthetic prices and a fake provider; no production Bloomberg or Enfusion calls are required.

## Source extraction

Extracted from `vascovalla/Risk-System` commit `0e2d41b` (v1168). The calendar math and prior-close fixes are retained. See `migration/` for a separately reviewable Risk source-removal patch. Existing Risk caches/databases are not deleted. The new repository is independent; it contains no Risk server or credentials.
