"""Bloomberg-ranked instrument lookup with a durable PostgreSQL query cache.

Only central-provider results supply verified names. Catalog search is an
outage fallback, never a source of guessed instruments or reordered live hits.
"""
from concurrent.futures import Future
import re
import threading
import time

from psycopg.types.json import Jsonb


WARNING = 'Bloomberg instrument search unavailable; showing saved results where available.'


def normalize_security(value):
    raw = re.sub(r'\s+', ' ', value.strip())
    match = re.search(r'<([a-z-]+)>$', raw, re.IGNORECASE)
    if not match:
        return raw
    marker = match.group(1).lower()
    suffix = {'m-mkt': 'M-Mkt'}.get(marker, marker.capitalize())
    return f'{raw[:match.start()].rstrip()} {suffix}'


class InstrumentResolver:
    def __init__(self, history, agent, clock=time.time):
        self.history, self.agent, self.clock = history, agent, clock
        self._lock = threading.Lock()
        self._inflight = {}
        with history.connect() as c:
            c.execute('SELECT pg_advisory_xact_lock(7314907002)')
            c.execute("""
                CREATE TABLE IF NOT EXISTS instrument_search_cache (
                    query TEXT PRIMARY KEY, items JSONB NOT NULL,
                    expires DOUBLE PRECISION NOT NULL)
            """)

    def search(self, query):
        if not isinstance(query, str) or len(query) > 64:
            raise ValueError('Instrument search query must be at most 64 characters')
        cleaned = ' '.join(query.split())
        if len(cleaned) < 2:
            return {'ok': True, 'items': []}
        key = cleaned.casefold()
        with self._lock:
            future = self._inflight.get(key)
            owner = future is None
            if owner:
                future = self._inflight[key] = Future()
        if not owner:
            return future.result()
        try:
            result = self._search(key, cleaned)
            future.set_result(result)
            return result
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with self._lock:
                del self._inflight[key]

    def _search(self, key, query):
        stale = None
        cache_available = True
        try:
            with self.history.connect() as c:
                row = c.execute(
                    'SELECT items,expires FROM instrument_search_cache WHERE query=%s',
                    (key,)).fetchone()
            if row:
                stale, expires = row
                if expires > self.clock():
                    return {'ok': True, 'items': stale, 'cached': True}
        except Exception:
            cache_available = False
        try:
            data = self.agent.search(query)
            if not isinstance(data, dict) or not isinstance(data.get('results'), list):
                raise ValueError('Invalid search response')
            items = []
            for row in data['results'][:10]:
                if not isinstance(row, dict) or not isinstance(row.get('security'), str):
                    raise ValueError('Invalid instrument')
                ticker = normalize_security(row['security'])
                name = row.get('description')
                if not ticker or not isinstance(name, str):
                    raise ValueError('Invalid instrument')
                items.append({'ticker': ticker, 'name': name.strip()})
        except Exception:
            if stale:
                return {'ok': True, 'items': stale, 'cached': True, 'warning': WARNING}
            try:
                items = self.history.instruments(query)[:10]
            except Exception:
                items = []
            return {'ok': True, 'items': items, 'cached': True, 'warning': WARNING}
        try:
            now = self.clock()
            with self.history.connect() as c:
                c.execute(
                    'INSERT INTO instrument_search_cache (query,items,expires) VALUES (%s,%s,%s) '
                    'ON CONFLICT(query) DO UPDATE SET items=excluded.items,expires=excluded.expires',
                    (key, Jsonb(items), now + (86400 if items else 300)))
                c.cursor().executemany(
                    'INSERT INTO instrument_catalog (ticker,name,selected_at) VALUES (%s,%s,%s) '
                    'ON CONFLICT(ticker) DO UPDATE SET name=excluded.name '
                    "WHERE excluded.name<>''",
                    [(item['ticker'], item['name'], now) for item in items if item['name']])
        except Exception:
            cache_available = False
        result = {'ok': True, 'items': items, 'cached': False}
        if not cache_available:
            result['warning'] = 'Instrument cache unavailable; results may need to be searched again.'
        return result
