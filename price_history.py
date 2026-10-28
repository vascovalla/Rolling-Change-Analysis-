"""Cache-first PostgreSQL daily prices, extracted from Risk v1168.

Only psycopg3 connections are supported. No server or Risk imports.

No imports of the server, credentials, startup work or live calls. Dependencies
are supplied by the host; tests use a temporary database and a fake provider.
"""
import datetime as dt
import hashlib
import json
import math
import os
import re
import threading
import time
from collections import defaultdict
from contextlib import closing, contextmanager


def date(value):
    parsed = dt.date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError('Dates must be YYYY-MM-DD')
    return parsed


def valid_price(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


class PriceHistory:
    def __init__(self, connect, fetch, seed_paths=(), mirror=None, clock=time.time):
        self.connect, self.fetch = connect, fetch
        self.seed_paths, self.mirror, self.clock = seed_paths, mirror, clock
        self.lock = threading.RLock()
        self.worker_lock = threading.Lock()
        self.jobs, self.seeds = {}, {}
        with connect() as c:
            c.execute("SELECT pg_advisory_xact_lock(7314907000)")
            c.execute('''
                CREATE TABLE IF NOT EXISTS daily_prices (
                    ticker TEXT NOT NULL, field TEXT NOT NULL, date DATE NOT NULL,
                    value DOUBLE PRECISION NOT NULL, updated DOUBLE PRECISION NOT NULL, provisional INTEGER NOT NULL, PRIMARY KEY(ticker,field,date));
                CREATE TABLE IF NOT EXISTS daily_price_coverage (
                    ticker TEXT NOT NULL, field TEXT NOT NULL, start DATE NOT NULL,
                    "end" DATE NOT NULL, expires DOUBLE PRECISION NOT NULL, partial INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(ticker,field,start,"end"));
                CREATE TABLE IF NOT EXISTS instrument_catalog (
                    ticker TEXT PRIMARY KEY, name TEXT NOT NULL, selected_at DOUBLE PRECISION NOT NULL);
            ''')

    @contextmanager
    def fill_connection(self):
        """One database-wide fill at a time, including other server processes.

        A session lock survives per-batch commits and is released on disconnect.
        Re-plan gaps only after acquiring it. Every bar/coverage batch is atomic.
        """
        with closing(self.connect()) as c:
            c.autocommit = True
            c.execute("SET lock_timeout = '30s'")
            c.execute('SELECT pg_advisory_lock(7314907001)')
            try:
                yield c
            finally:
                c.execute('SELECT pg_advisory_unlock(7314907001)')

    def health(self):
        with self.connect() as c:
            return c.execute('SELECT 1').fetchone() == (1,)

    def remember(self, tickers):
        """Persist selected tickers without overwriting locally saved names."""
        with self.connect() as c:
            c.cursor().executemany(
                'INSERT INTO instrument_catalog VALUES (%s,%s,%s) '
                'ON CONFLICT(ticker) DO UPDATE SET selected_at=excluded.selected_at',
                [(t, t, self.clock()) for t in tickers])

    def instruments(self, query=''):
        # Literal substring search: SQL wildcards in user input have no meaning.
        with self.connect() as c:
            rows = c.execute(
                'SELECT ticker,name FROM instrument_catalog '
                'WHERE strpos(lower(ticker),lower(%s))>0 OR strpos(lower(name),lower(%s))>0 '
                'ORDER BY selected_at DESC,ticker LIMIT 50', (query, query)).fetchall()
        return [{'ticker': t, 'name': n} for t, n in rows]

    def seed(self):
        """Import existing saved history without replacing newer database bars (snapshot revision timestamps)."""
        affected = 0
        with self.lock:
            for path in self.seed_paths:
                try:
                    stat = os.stat(path)
                    stamp = (stat.st_mtime_ns, stat.st_size)
                    if self.seeds.get(path) == stamp:
                        continue
                    with open(path, encoding='utf-8') as f:
                        data = json.load(f)
                    revised = stat.st_mtime
                    timestamp = data.get('timestamp')
                    if isinstance(timestamp, str):
                        try:
                            revised = dt.datetime.fromisoformat(timestamp.replace('Z', '+00:00')).timestamp()
                        except ValueError:
                            pass
                    data = data.get('data', data)
                    rows = []
                    for ticker, payload in data.items():
                        if not isinstance(payload, dict):
                            continue
                        values = payload.get('last', payload.get('values', []))
                        for day, value in zip(payload.get('dates', []), values):
                            try:
                                date(day)
                            except (ValueError, TypeError):
                                continue
                            if valid_price(value):
                                rows.append((ticker, 'PX_LAST', day, value, revised, self.provisional(day, revised)))
                    with closing(self.connect()) as c, c:
                        cursor = c.cursor()
                        cursor.executemany("INSERT INTO daily_prices VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(ticker,field,date) DO UPDATE SET value=excluded.value,updated=excluded.updated,provisional=CASE WHEN excluded.value=daily_prices.value THEN daily_prices.provisional ELSE excluded.provisional END WHERE excluded.updated > daily_prices.updated", rows)
                        affected += cursor.rowcount
                    self.seeds[path] = stamp
                except (OSError, ValueError, TypeError, AttributeError):
                    # One bad legacy cache must not prevent using the database.
                    continue

        return affected

    def provisional(self, day, observed_at=None):
        # Allow overnight markets to finish; re-fetch provisional observations
        # after this boundary instead of silently freezing an intraday quote.
        ready = dt.datetime.combine(date(day) + dt.timedelta(days=1), dt.time(12), dt.timezone.utc).timestamp()
        return int((self.clock() if observed_at is None else observed_at) < ready)

    def read(self, tickers, start, end, predecessor=False):
        out = {}
        with closing(self.connect()) as c:
            for ticker in tickers:
                rows = c.execute('SELECT date::text,value FROM daily_prices WHERE ticker=%s AND field=%s AND date BETWEEN %s AND %s ORDER BY date',
                                 (ticker, 'PX_LAST', start, end)).fetchall()
                if predecessor and (not rows or rows[0][0] != start):
                    prior = c.execute('SELECT date::text,value FROM daily_prices WHERE ticker=%s AND field=%s AND date<%s ORDER BY date DESC LIMIT 1',
                                      (ticker, 'PX_LAST', start)).fetchone()
                    if prior:
                        rows.insert(0, prior)
                out[ticker] = {'dates': [r[0] for r in rows], 'values': [r[1] for r in rows]}
        return out

    def predecessor_start(self, ticker, start):
        """Search through the boundary, not just a hardcoded weekend buffer."""
        entry = self.read([ticker], start, start, predecessor=True)[ticker]
        floor = max(dt.date.min, date(start) - dt.timedelta(days=min(4000, date(start).toordinal() - 1)))
        if entry['dates']:
            prior = entry['dates'][-1]
            if prior == start:
                return start
            return max(floor, date(prior)).isoformat()
        return floor.isoformat()

    def needs_predecessor(self, tickers, start):
        return any(self.gaps([t], self.predecessor_start(t, start), start) for t in tickers)

    def fill_predecessors(self, tickers, start):
        errors = []
        # Expanding windows handle holidays and long suspensions without fetching
        # years of data for the normal Friday-before-Sunday case. All passes use
        # the same gap planner, batch limit and persistent coverage as ordinary history.
        pending = list(tickers)
        failed = set()
        for width in (7, 30, 90, 365, 1460, 4000):
            groups = defaultdict(list)
            for ticker in pending:
                lower = self.predecessor_start(ticker, start)
                bound = date(start) - dt.timedelta(days=min(width, date(start).toordinal() - 1))
                groups[max(lower, bound.isoformat())].append(ticker)
            for lower, names in groups.items():
                result = self.fill(names, lower, start)
                errors.extend(result)
                if any('Price provider unavailable' in e for e in result):
                    failed.update(names)
                failed.update(t for t in names if 'No valid provider response for ' + t in result)
            pending = [t for t in pending if t not in failed and (self.needs_predecessor([t], start) or not self.read([t], start, start, predecessor=True)[t]['dates'])]
            if not pending:
                break
        if pending:
            errors.append('No previous close found within 4,000 days for ' + ', '.join(pending) + '; affected rolling values remain unavailable.')
        return list(dict.fromkeys(errors))

    def gaps(self, tickers, start, end):
        """Weekday holes plus expired current-day coverage; weekends need no BDH."""
        result = defaultdict(list)
        with closing(self.connect()) as c:
            for ticker in tickers:
                known = {r[0] for r in c.execute('SELECT date::text FROM daily_prices WHERE ticker=%s AND field=%s AND date BETWEEN %s AND %s AND provisional=0', (ticker, 'PX_LAST', start, end))}
                covered = c.execute('SELECT start::text,"end"::text FROM daily_price_coverage WHERE ticker=%s AND field=%s AND "end">=%s AND start<=%s AND expires>%s',
                                    (ticker, 'PX_LAST', start, end, self.clock())).fetchall()
                missing = []
                day, stop = date(start), date(end)
                while day <= stop:
                    iso = day.isoformat()
                    if day.weekday() < 5 and iso not in known and not any(a <= iso <= b for a, b in covered):
                        missing.append(day)
                    day += dt.timedelta(days=1)
                # Coalesce consecutive missing sessions, including intervening weekends.
                for day in missing:
                    if result[ticker] and (day - date(result[ticker][-1][1])).days <= (3 if day.weekday() == 0 else 1):
                        result[ticker][-1][1] = day.isoformat()
                    else:
                        result[ticker].append([day.isoformat(), day.isoformat()])
        return dict(result)

    def fill(self, tickers, start, end):
        """Serialized read/plan/fetch prevents overlapping jobs fetching twice."""
        errors = []
        with self.worker_lock, self.fill_connection() as db:
            groups = defaultdict(list)
            for ticker, ranges in self.gaps(tickers, start, end).items():
                for a, b in ranges:
                    groups[a, b].append(ticker)
            for (a, b), names in groups.items():
                for offset in range(0, len(names), 12):
                    batch = names[offset:offset + 12]
                    try:
                        data = self.fetch(batch, a, b)
                    except Exception:
                        errors.append('Price provider unavailable; saved prices retained.')
                        continue
                    now = self.clock()
                    mirror = {}
                    with db.transaction():
                        c = db
                        for ticker in batch:
                            payload = data.get(ticker) if isinstance(data, dict) else None
                            if not isinstance(payload, list):
                                errors.append('No valid provider response for ' + ticker)
                                continue
                            rows = []
                            invalid = False
                            for row in payload:
                                if not isinstance(row, dict):
                                    invalid = True
                                    continue
                                day, value = row.get('date'), row.get('PX_LAST')
                                try:
                                    date(day)
                                except (ValueError, TypeError):
                                    invalid = True
                                    continue
                                if a <= day <= b and valid_price(value):
                                    rows.append((ticker, 'PX_LAST', day, value, now, self.provisional(day)))
                                elif a <= day <= b:
                                    invalid = True
                            c.cursor().executemany("INSERT INTO daily_prices VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(ticker,field,date) DO UPDATE SET value=excluded.value,updated=excluded.updated,provisional=excluded.provisional", rows)
                            # BDH responses do not attest completeness. Never certify
                            # omitted weekdays permanently (holiday, suspension or truncation).
                            present = {r[2] for r in rows}
                            day, stop = date(a), date(b)
                            omissions = False
                            while day <= stop:
                                if day.weekday() < 5 and day.isoformat() not in present:
                                    omissions = True
                                day += dt.timedelta(days=1)
                            if not invalid:
                                provisional = self.provisional(b)
                                expires = now + (300 if provisional else 21600) if omissions or provisional else 253402300799
                                c.execute('INSERT INTO daily_price_coverage VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(ticker,field,start,"end") DO UPDATE SET expires=excluded.expires,partial=excluded.partial', (ticker, 'PX_LAST', a, b, expires, int(omissions)))
                            if omissions or invalid:
                                errors.append('Price gaps remain for ' + ticker + '; saved available prices. Missing dates will be checked again later.')
                            if rows:
                                mirror[ticker] = {'dates': [r[2] for r in rows], 'last': [r[3] for r in rows]}
                    if mirror and self.mirror:
                        try:
                            self.mirror(mirror)
                        except Exception:
                            errors.append('Prices saved in database; shared cache mirror could not be updated.')
        return list(dict.fromkeys(errors))

    def request(self, tickers, start, end, job=None, predecessor=False):
        tickers = list(dict.fromkeys(tickers))
        today = dt.datetime.fromtimestamp(self.clock(), dt.timezone.utc).date()
        if not tickers or len(tickers) > 100 or any(not isinstance(t, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 ./&-]{0,70}', t) for t in tickers):
            raise ValueError('Request between 1 and 100 Bloomberg tickers per batch')
        if date(start) > date(end) or date(end) > today or (date(end) - date(start)).days > 4000:
            raise ValueError('Invalid history range (maximum 4000 calendar days)')
        key = hashlib.sha256(json.dumps([sorted(tickers), start, end] + ([True] if predecessor else [])).encode()).hexdigest()[:24]
        if job and job != key:
            raise ValueError('History job does not match requested range')
        self.remember(tickers)
        if not job:
            self.seed()
        with self.lock:
            data = self.read(tickers, start, end, predecessor=predecessor)
            self.jobs = {k: v for k, v in self.jobs.items() if v['status'] == 'refreshing' or self.clock() - v['created'] < 3600}
            state = self.jobs.get(key)
            if not job and (not state or state['status'] != 'refreshing'):
                if not self.gaps(tickers, start, end) and not (predecessor and self.needs_predecessor(tickers, start)):
                    with closing(self.connect()) as c:
                        partial = [t for t in tickers if c.execute(
                            'SELECT 1 FROM daily_price_coverage WHERE ticker=%s AND field=%s AND start<=%s AND "end">=%s AND expires>%s AND partial=1 LIMIT 1',
                            (t, 'PX_LAST', end, start, self.clock())).fetchone()]
                    return {'ok': True, 'status': 'partial' if partial else 'cached', 'data': data,
                            'errors': ['Some dates have no returned prices for ' + ', '.join(partial) + '; saved prices shown. Missing dates will be checked again later.'] if partial else []}
                if sum(v['status'] == 'refreshing' for v in self.jobs.values()) >= 8:
                    return {'ok': True, 'status': 'partial', 'data': data, 'errors': ['Price fetch queue is busy; retry shortly.']}
                state = {'status': 'refreshing', 'created': self.clock(), 'errors': []}
                self.jobs[key] = state
                def work():
                    try:
                        errors = self.fill(tickers, start, end)
                        if predecessor:
                            errors.extend(self.fill_predecessors(tickers, start))
                    except Exception:
                        errors = ['Price update failed; saved prices retained.']
                    with self.lock:
                        state.update(status='partial' if errors else 'complete', errors=errors)
                threading.Thread(target=work, daemon=True, name='daily-price-gaps').start()
            if state is None:
                return {'ok': True, 'status': 'partial', 'data': data, 'errors': ['History job expired; Analyze again.']}
            return {'ok': True, 'job': key, 'status': state['status'], 'data': data, 'errors': list(state['errors'])}
