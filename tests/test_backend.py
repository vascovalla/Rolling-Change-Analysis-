"""Standalone tests: pytest --confcutdir=tests --rootdir=. tests/test_backend.py.
Integration tests use TEST_DATABASE_URL, each in a disposable unique schema.
"""
import datetime as dt
import json
import os
import time
import threading
import multiprocessing
import uuid
from pathlib import Path
import pytest
import psycopg
from psycopg import sql
from fastapi.testclient import TestClient
from price_history import PriceHistory
from app import create_app

NOW = dt.datetime(2026, 10, 26, 12, tzinfo=dt.timezone.utc).timestamp()

@pytest.fixture
def connect():
    dsn = os.environ.get('TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('TEST_DATABASE_URL required for isolated PostgreSQL integration')
    schema = 'rolling_test_' + uuid.uuid4().hex
    with psycopg.connect(dsn) as c:
        c.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    def connection():
        return psycopg.connect(dsn, options='-csearch_path=' + schema)
    connection.dsn, connection.schema = dsn, schema
    yield connection
    with psycopg.connect(dsn) as c:
        c.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))

def service(connect, fetch, seed=None, mirror=None):
    return PriceHistory(connect, fetch, [str(seed)] if seed else [], mirror, clock=lambda: NOW)


class FakeHistory:
    def request(self, tickers, start, end, job=None, predecessor=False):
        if tickers == ['BAD']:
            raise ValueError('Invalid history range')
        self.args = tickers, start, end, job, predecessor
        return {'ok': True, 'status': 'cached', 'data': {'A': {'dates': ['2026-10-23'], 'values': [0]}}}

    def instruments(self, query):
        return [{'ticker': 'A', 'name': 'A'}]

    def health(self):
        return True


def test_history_http_contract_and_auth(monkeypatch):
    monkeypatch.setenv('APP_USERNAME', 'analyst')
    monkeypatch.setenv('APP_PASSWORD', 'test-password')
    history = FakeHistory()
    with TestClient(create_app(history=history)) as client:
        url = '/api/history?tickers=A,B&from=2026-10-22&to=2026-10-23&job=abc&predecessor=1'
        assert client.get(url).status_code == 401
        assert client.get('/').status_code == 401
        assert client.get('/static/rolling-change.js').status_code == 401
        assert client.get('/health').status_code == 401
        response = client.get(url, auth=('analyst', 'test-password'))
        assert response.status_code == 200
        assert response.json()['data']['A'] == {'dates': ['2026-10-23'], 'last': [0]}
        assert history.args == (['A', 'B'], '2026-10-22', '2026-10-23', 'abc', True)
        assert 'no-store' in response.headers['cache-control']
        assert client.get('/api/instruments?q=A', auth=('analyst', 'test-password')).json()['items'] == [{'ticker': 'A', 'name': 'A'}]


@pytest.fixture
def local_app(monkeypatch):
    monkeypatch.delenv('APP_USERNAME', raising=False)
    monkeypatch.delenv('APP_PASSWORD', raising=False)
    monkeypatch.setenv('APP_HOST', '127.0.0.1')
    return create_app(history=FakeHistory())


def test_local_validation_and_fixed_static_root(local_app):
    with TestClient(local_app, base_url='http://127.0.0.1') as client:
        assert client.get('/health').json() == {'ok': True}
        for path in ('/.env', '/data/prices.json', '/app.py', '/static/../app.py', '/static/%2e%2e/app.py'):
            assert client.get(path).status_code == 404
        assert client.get('/api/history?tickers=BAD&from=2026-01-01&to=2026-10-23').status_code == 400
        assert client.get('/api/history').status_code == 422
    with TestClient(local_app, base_url='https://public.example') as client:
        assert client.get('/api/instruments?q=A').status_code == 503


def test_public_bind_requires_complete_auth(monkeypatch):
    monkeypatch.setenv('APP_HOST', '0.0.0.0')
    monkeypatch.delenv('APP_USERNAME', raising=False)
    monkeypatch.delenv('APP_PASSWORD', raising=False)
    with pytest.raises(ValueError, match='APP_USERNAME'):
        create_app(history=FakeHistory())
    monkeypatch.setenv('APP_USERNAME', 'user')
    with pytest.raises(ValueError, match='APP_PASSWORD'):
        create_app(history=FakeHistory())


def test_health_and_unexpected_errors_are_sanitized(monkeypatch):
    monkeypatch.setenv('APP_USERNAME', 'user')
    monkeypatch.setenv('APP_PASSWORD', 'password')
    class Broken(FakeHistory):
        def health(self):
            raise RuntimeError('SECRET DATABASE_URL')
        def request(self, *args, **kwargs):
            raise RuntimeError('SECRET DATABASE_URL')
    with TestClient(create_app(history=Broken())) as client:
        for url in ('/health', '/api/history?tickers=A&from=2026-10-23&to=2026-10-23'):
            response = client.get(url, auth=('user', 'password'))
            assert response.status_code == 503
            assert 'SECRET' not in response.text


def test_reopen_retains_coverage_corrections_and_catalog(connect, tmp_path):
    history = service(connect, lambda names, a, b: {t: [] for t in names})
    result = history.request(['Selected US Equity'], '2026-10-25', '2026-10-25')
    assert result['status'] == 'cached'
    assert history.fill(['A'], '2026-10-23', '2026-10-23')
    reopened = service(connect, lambda *args: pytest.fail('covered range was fetched'))
    assert not reopened.gaps(['A'], '2026-10-23', '2026-10-23')
    assert reopened.request(['A'], '2026-10-23', '2026-10-23')['status'] == 'partial'
    assert reopened.instruments('selected') == [{'ticker': 'Selected US Equity', 'name': 'Selected US Equity'}]
    with connect() as c:
        c.execute('UPDATE instrument_catalog SET name=%s WHERE ticker=%s', ('Saved Company Name', 'Selected US Equity'))
    assert reopened.instruments('Company') == [{'ticker': 'Selected US Equity', 'name': 'Saved Company Name'}]


def _process_fill(dsn, schema, queue, gate):
    def connect():
        return psycopg.connect(dsn, options='-csearch_path=' + schema)
    def fetch(names, a, b):
        queue.put(names)
        time.sleep(.15)
        return {t: [{'date': a, 'PX_LAST': 5}] for t in names}
    history = service(connect, fetch)
    gate.wait(10)
    history.fill(['A'], '2026-10-23', '2026-10-23')


def test_advisory_lock_coalesces_across_processes(connect):
    service(connect, lambda *args: {})
    ctx = multiprocessing.get_context('spawn')
    queue, gate = ctx.Queue(), ctx.Event()
    processes = [ctx.Process(target=_process_fill, args=(connect.dsn, connect.schema, queue, gate)) for _ in range(2)]
    try:
        for process in processes:
            process.start()
        gate.set()
        for process in processes:
            process.join(15)
            assert process.exitcode == 0
        assert queue.get(timeout=3) == ['A']
        import queue as queues
        with pytest.raises(queues.Empty):
            queue.get(timeout=.2)
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)


def test_request_validation_before_fetch(connect):
    history = service(connect, lambda *args: pytest.fail('invalid request fetched'))
    for names, a, b in [([], '2026-10-23', '2026-10-23'), (['A'] * 101 + ['B' + str(i) for i in range(101)], '2026-10-23', '2026-10-23'), (['A\n'], '2026-10-23', '2026-10-23'), (['A'], '2026-10-24', '2026-10-23'), (['A'], '2026-1-01', '2026-10-23'), (['A'], '2026-10-23', '2027-10-23')]:
        with pytest.raises(ValueError):
            history.request(names, a, b)
    with pytest.raises(ValueError):
        history.request(['A'], '2026-10-23', '2026-10-23', job='wrong')


def test_batch_transaction_rolls_back_prices_if_coverage_write_fails(connect):
    history = service(connect, lambda names, a, b: {t: [{'date': a, 'PX_LAST': 10}] for t in names})
    with connect() as c:
        c.execute("ALTER TABLE daily_price_coverage ADD CONSTRAINT reject_b CHECK (ticker <> 'B')")
    with pytest.raises(psycopg.errors.CheckViolation):
        history.fill(['A', 'B'], '2026-10-23', '2026-10-23')
    assert history.read(['A', 'B'], '2026-10-23', '2026-10-23') == {
        'A': {'dates': [], 'values': []}, 'B': {'dates': [], 'values': []}}
    with connect() as c:
        assert c.execute('SELECT count(*) FROM daily_price_coverage').fetchone() == (0,)
        c.execute('ALTER TABLE daily_price_coverage DROP CONSTRAINT reject_b')
    # Rollback/disconnect released the advisory lock; the next batch succeeds.
    assert history.fill(['A', 'B'], '2026-10-23', '2026-10-23') == []


def test_http_real_store_fake_provider_and_reopen(connect, monkeypatch):
    monkeypatch.setenv('APP_USERNAME', 'user')
    monkeypatch.setenv('APP_PASSWORD', 'password')
    from data_agent import DataAgent
    import httpx
    def provider(request):
        body = json.loads(request.content)
        assert body['securities'] == ['A US Equity']
        return httpx.Response(200, json={'data': {'A US Equity': [{'date': body['start_date'], 'PX_LAST': 17}]}})
    with httpx.Client(transport=httpx.MockTransport(provider)) as transport:
        history = service(connect, DataAgent('https://pool.example', 'fake-key', client=transport))
        with TestClient(create_app(history=history)) as client:
            assert client.get('/health', auth=('user', 'password')).json() == {'ok': True}
            url = '/api/history?tickers=A%20US%20Equity&from=2026-10-23&to=2026-10-23'
            response = client.get(url, auth=('user', 'password')).json()
            deadline = time.monotonic() + 5
            while response['status'] == 'refreshing' and time.monotonic() < deadline:
                time.sleep(.01)
                response = client.get(url + '&job=' + response['job'], auth=('user', 'password')).json()
            assert response['status'] == 'complete'
            assert response['data']['A US Equity'] == {'dates': ['2026-10-23'], 'last': [17]}
            assert 'fake-key' not in json.dumps(response)
    reopened = service(connect, lambda *args: pytest.fail('saved price was refetched'))
    assert reopened.request(['A US Equity'], '2026-10-23', '2026-10-23')['status'] == 'cached'


def test_static_reads_only_allowed_files_in_fixed_root(tmp_path, monkeypatch):
    import app
    monkeypatch.setenv('APP_USERNAME', 'user')
    monkeypatch.setenv('APP_PASSWORD', 'password')
    static = tmp_path / 'static'
    static.mkdir()
    (static / 'index.html').write_text('<title>Rolling</title>')
    (static / 'rolling-change.js').write_text('window.loaded=true;')
    (static / '.env').write_text('SECRET')
    (static / 'data.json').write_text('SECRET')
    (tmp_path / 'app.py').write_text('SECRET')
    monkeypatch.setattr(app, 'STATIC_ROOT', static)
    with TestClient(create_app(history=FakeHistory())) as client:
        for path in ('/', '/static/rolling-change.js', '/rolling-change.js'):
            response = client.get(path, auth=('user', 'password'))
            assert response.status_code == 200
        for path in ('/static/.env', '/static/data.json', '/static/%2e%2e/app.py'):
            response = client.get(path, auth=('user', 'password'))
            assert response.status_code == 404
            assert 'SECRET' not in response.text

def test_saved_prices_fill_only_missing_dates(tmp_path, connect):
    seed = tmp_path / 'saved.json'
    seed.write_text(json.dumps({'data': {'A US Equity': {'dates': ['2026-10-19', '2026-10-21'], 'last': [10, 12]}}}))
    calls = []
    def fetch(tickers, start, end):
        calls.append((tickers, start, end))
        return {'A US Equity': [{'date': start, 'PX_LAST': 11}]}
    s = service(connect, fetch, seed)
    s.seed()
    assert s.fill(['A US Equity'], '2026-10-19', '2026-10-21') == []
    assert calls == [(['A US Equity'], '2026-10-20', '2026-10-20')]
    assert s.read(['A US Equity'], '2026-10-19', '2026-10-21')['A US Equity']['values'] == [10, 11, 12]
    s.fill(['A US Equity'], '2026-10-19', '2026-10-21')
    assert len(calls) == 1

def test_batches_preserve_all_tickers_and_seed_cannot_overwrite(tmp_path, connect):
    calls = []
    def fetch(tickers, start, end):
        calls.append(tickers)
        return {t: [{'date': start, 'PX_LAST': 20}] for t in tickers}
    s = service(connect, fetch)
    names = [f'T{i} US Equity' for i in range(25)]
    s.fill(names, '2026-10-23', '2026-10-23')
    assert list(map(len, calls)) == [12, 12, 1]
    assert all(s.read(names, '2026-10-23', '2026-10-23')[t]['values'] == [20] for t in names)
    seed = tmp_path / 'old.json'
    seed.write_text(json.dumps({names[0]: {'dates': ['2026-10-23'], 'values': [1]}}))
    os.utime(seed, (NOW - 3600, NOW - 3600))
    s.seed_paths = [str(seed)]
    s.seed()
    assert s.read(names[:1], '2026-10-23', '2026-10-23')[names[0]]['values'] == [20]

def test_outage_retains_cache_and_retries_gap(tmp_path, connect):
    seed = tmp_path / 'old.json'
    seed.write_text(json.dumps({'A': {'dates': ['2026-10-22'], 'last': [10]}}))
    def offline(*args):
        raise RuntimeError('secret vendor error')
    s = service(connect, offline, seed)
    s.seed()
    errors = s.fill(['A'], '2026-10-22', '2026-10-23')
    assert 'secret' not in str(errors)
    assert s.read(['A'], '2026-10-22', '2026-10-23')['A']['values'] == [10]
    assert s.gaps(['A'], '2026-10-22', '2026-10-23') == {'A': [['2026-10-23', '2026-10-23']]}

def test_empty_and_null_are_not_permanent_coverage(tmp_path, connect):
    s = service(connect, lambda *args: {'A': [], 'B': [{'date': '2026-10-23', 'PX_LAST': None}]})
    assert s.fill(['A', 'B'], '2026-10-23', '2026-10-23')
    assert 'A' not in s.gaps(['A', 'B'], '2026-10-23', '2026-10-23')
    assert 'B' in s.gaps(['A', 'B'], '2026-10-23', '2026-10-23')
    s.clock = lambda: NOW + 21601
    assert set(s.gaps(['A', 'B'], '2026-10-23', '2026-10-23')) == {'A', 'B'}

def test_explicit_zero_and_negative_prices_are_preserved(tmp_path, connect):
    s = service(connect, lambda *args: {'A': [{'date': '2026-10-22', 'PX_LAST': 0}, {'date': '2026-10-23', 'PX_LAST': -1}]})
    assert not s.fill(['A'], '2026-10-22', '2026-10-23')
    assert s.read(['A'], '2026-10-22', '2026-10-23')['A']['values'] == [0, -1]

def test_cache_first_and_shared_inflight_job(tmp_path, connect):
    import threading
    entered, release = threading.Event(), threading.Event()
    calls = []
    def fetch(tickers, start, end):
        calls.append(tickers)
        entered.set()
        assert release.wait(3)
        return {'A': [{'date': start, 'PX_LAST': 11}]}
    s = service(connect, fetch)
    a = s.request(['A'], '2026-10-23', '2026-10-23')
    assert a['status'] == 'refreshing'
    assert entered.wait(3)
    b = s.request(['A'], '2026-10-23', '2026-10-23')
    assert b['job'] == a['job']
    release.set()
    for _ in range(100):
        result = s.request(['A'], '2026-10-23', '2026-10-23', a['job'])
        if result['status'] != 'refreshing':
            break
        time.sleep(.01)
    assert result['status'] == 'complete'
    assert result['data']['A']['values'] == [11]
    assert len(calls) == 1
    assert s.request(['A'], '2026-10-23', '2026-10-23')['status'] == 'cached'

def test_current_day_refreshes_but_historical_bars_do_not(tmp_path, connect):
    calls = []
    def fetch(tickers, start, end):
        calls.append(start)
        return {'A': [{'date': start, 'PX_LAST': 10}]}
    s = service(connect, fetch)
    s.fill(['A'], '2026-10-26', '2026-10-26')
    s.fill(['A'], '2026-10-26', '2026-10-26')
    assert len(calls) == 1
    s.clock = lambda: NOW + 301
    s.fill(['A'], '2026-10-26', '2026-10-26')
    assert len(calls) == 2

def test_longer_range_fetches_prefix_only(tmp_path, connect):
    calls = []
    def fetch(tickers, start, end):
        calls.append((start, end))
        return {'A': [{'date': start, 'PX_LAST': 10}, {'date': end, 'PX_LAST': 12}]}
    s = service(connect, fetch)
    s.fill(['A'], '2026-10-22', '2026-10-23')
    s.fill(['A'], '2026-10-19', '2026-10-23')
    assert calls == [('2026-10-22', '2026-10-23'), ('2026-10-19', '2026-10-21')]

def test_provisional_price_is_finalized_after_rollover(tmp_path, connect):
    calls = []
    def fetch(tickers, start, end):
        calls.append(start)
        return {'A': [{'date': start, 'PX_LAST': 10 + len(calls)}]}
    s = service(connect, fetch)
    s.fill(['A'], '2026-10-26', '2026-10-26')
    s.clock = lambda: NOW + 86400
    s.fill(['A'], '2026-10-26', '2026-10-26')
    assert len(calls) == 2
    assert s.read(['A'], '2026-10-26', '2026-10-26')['A']['values'] == [12]
    s.clock = lambda: NOW + 172800
    assert not s.gaps(['A'], '2026-10-26', '2026-10-26')

def test_truncated_nonempty_response_retries_omitted_dates(tmp_path, connect):
    s = service(connect, lambda *args: {'A': [{'date': '2026-10-19', 'PX_LAST': 10}]})
    assert s.fill(['A'], '2026-10-19', '2026-10-23')
    s.clock = lambda: NOW + 21601
    assert s.gaps(['A'], '2026-10-19', '2026-10-23') == {'A': [['2026-10-20', '2026-10-23']]}

def test_newer_shared_cache_correction_propagates(tmp_path, connect):
    s = service(connect, lambda *args: {'A': [{'date':'2026-10-23','PX_LAST':10}]})
    s.fill(['A'], '2026-10-23', '2026-10-23')
    seed = tmp_path / 'corrected.json'
    seed.write_text(json.dumps({'timestamp':'2026-10-27T12:00:00+00:00','data':{'A':{'dates':['2026-10-23'],'last':[25]}}}))
    s.seed_paths = [str(seed)]
    s.seed()
    assert s.read(['A'], '2026-10-23', '2026-10-23')['A']['values'] == [25]

def test_overlapping_jobs_fetch_common_ticker_once(tmp_path, connect):
    import threading
    calls = []
    def fetch(tickers, start, end):
        calls.append(tickers)
        return {t: [{'date': start, 'PX_LAST': 10}] for t in tickers}
    s = service(connect, fetch)
    a = threading.Thread(target=lambda: s.fill(['A', 'B'], '2026-10-23', '2026-10-23'))
    b = threading.Thread(target=lambda: s.fill(['B', 'C'], '2026-10-23', '2026-10-23'))
    a.start(); b.start(); a.join(3); b.join(3)
    assert not a.is_alive() and not b.is_alive()
    assert sum(batch.count('B') for batch in calls) == 1

def test_delayed_seed_keeps_capture_day_provisional(tmp_path, connect):
    seed = tmp_path / 'old-intraday.json'
    seed.write_text(json.dumps({'timestamp': '2026-10-23T12:00:00+00:00', 'data': {'A': {'dates': ['2026-10-23'], 'last': [10]}}}))
    s = service(connect, lambda *args: {'A': [{'date': '2026-10-23', 'PX_LAST': 12}]}, seed)
    s.seed()
    assert s.gaps(['A'], '2026-10-23', '2026-10-23') == {'A': [['2026-10-23', '2026-10-23']]}
    s.fill(['A'], '2026-10-23', '2026-10-23')
    assert s.read(['A'], '2026-10-23', '2026-10-23')['A']['values'] == [12]

def test_unrelated_cache_rewrite_cannot_finalize_same_provisional_bar(tmp_path, connect):
    s = service(connect, lambda *args: {'A': [{'date': '2026-10-26', 'PX_LAST': 10}]})
    s.fill(['A'], '2026-10-26', '2026-10-26')
    s.clock = lambda: NOW + 172800
    seed = tmp_path / 'rewritten.json'
    seed.write_text(json.dumps({'timestamp': '2026-10-28T12:00:00+00:00', 'data': {'A': {'dates': ['2026-10-26'], 'last': [10]}, 'B': {'dates': ['2026-10-27'], 'last': [5]}}}))
    s.seed_paths = [str(seed)]
    s.seed()
    assert s.gaps(['A'], '2026-10-26', '2026-10-26') == {'A': [['2026-10-26', '2026-10-26']]}

def test_predecessor_cached_weekend_and_exact_boundary(tmp_path, connect):
    calls = []
    def fetch(names, a, b):
        calls.append((names, a, b))
        return {t: [{'date': a, 'PX_LAST': 100}] for t in names}
    s = service(connect, fetch)
    s.fill(['A'], '2025-10-24', '2025-10-24')
    calls.clear()
    assert s.fill_predecessors(['A'], '2025-10-26') == []
    assert not calls
    assert s.read(['A'], '2025-10-26', '2025-10-26', predecessor=True)['A']['dates'] == ['2025-10-24']
    assert s.read(['A'], '2025-10-26', '2025-10-26')['A']['dates'] == []
    assert s.fill_predecessors(['A'], '2025-10-24') == []
    assert not calls

def test_predecessor_search_fills_between_old_cache_and_boundary(tmp_path, connect):
    calls = []
    bars = [{'date': '2025-09-01', 'PX_LAST': 90}, {'date': '2025-10-10', 'PX_LAST': 100}]
    def fetch(names, a, b):
        calls.append((a, b))
        return {t: [r for r in bars if a <= r['date'] <= b] for t in names}
    s = service(connect, fetch)
    s.fill(['A'], '2025-09-01', '2025-09-01')
    s.fill_predecessors(['A'], '2025-10-26')
    assert s.read(['A'], '2025-10-26', '2025-10-26', predecessor=True)['A']['values'] == [100]
    count = len(calls)
    s.fill_predecessors(['A'], '2025-10-26')
    assert len(calls) == count
    assert s.read(['A'], '2025-09-01', '2025-09-01')['A']['values'] == [90]

def test_predecessor_missing_cache_expands_and_batches(tmp_path, connect):
    calls = []
    def fetch(names, a, b):
        calls.append((names, a, b))
        return {t: ([{'date': '2025-10-10', 'PX_LAST': 100}] if a <= '2025-10-10' <= b else []) for t in names}
    s = service(connect, fetch)
    names = ['T' + str(i) for i in range(25)]
    s.fill_predecessors(names, '2025-10-26')
    assert max(len(c[0]) for c in calls) <= 12
    assert all(v['values'] == [100] for v in s.read(names, '2025-10-26', '2025-10-26', predecessor=True).values())

def test_predecessor_outage_stops_without_unbounded_retries(tmp_path, connect):
    calls = []
    def fetch(*args):
        calls.append(args)
        raise RuntimeError('offline')
    s = service(connect, fetch)
    assert 'unavailable' in s.fill_predecessors(['A'], '2025-10-26')[0]
    assert len(calls) == 1

def test_request_returns_fetched_predecessor_on_poll(tmp_path, connect):
    bars = [{'date': '2025-10-24', 'PX_LAST': 100}]
    s = service(connect, lambda names, a, b: {t: [r for r in bars if a <= r['date'] <= b] for t in names})
    result = s.request(['A'], '2025-10-26', '2025-10-26', predecessor=True)
    deadline = time.monotonic() + 3
    while result['status'] == 'refreshing' and time.monotonic() < deadline:
        time.sleep(.01)
        result = s.request(['A'], '2025-10-26', '2025-10-26', job=result['job'], predecessor=True)
    assert result['status'] != 'refreshing'
    assert result['data']['A'] == {'dates': ['2025-10-24'], 'values': [100]}

def test_predecessor_refreshes_provisional_close(tmp_path, connect):
    calls = []
    def fetch(names, a, b):
        calls.append((a, b))
        return {'A': [{'date': '2026-10-23', 'PX_LAST': 12}]}
    s = service(connect, fetch)
    with s.connect() as c:
        c.execute('INSERT INTO daily_prices VALUES (%s,%s,%s,%s,%s,%s)', ('A', 'PX_LAST', '2026-10-23', 10, NOW - 172800, 1))
    s.fill_predecessors(['A'], '2026-10-25')
    assert calls == [('2026-10-23', '2026-10-23')]
    assert s.read(['A'], '2026-10-25', '2026-10-25', predecessor=True)['A']['values'] == [12]

def test_predecessor_one_bad_ticker_does_not_block_healthy_long_gap(tmp_path, connect):
    def fetch(names, a, b):
        return {t: ([{'date': '2025-10-10', 'PX_LAST': 100}] if a <= '2025-10-10' <= b else []) for t in names if t != 'BAD'}
    s = service(connect, fetch)
    errors = s.fill_predecessors(['BAD', 'GOOD'], '2025-10-26')
    assert 'No valid provider response for BAD' in errors
    assert s.read(['GOOD'], '2025-10-26', '2025-10-26', predecessor=True)['GOOD']['values'] == [100]


def test_explicit_import_reports_affected_rows(connect, tmp_path):
    path = tmp_path / 'prices.json'
    path.write_text(json.dumps({'data': {'A': {'dates': ['2026-10-23'], 'last': [10]}}}))
    s = service(connect, lambda *args: {}, path)
    assert s.seed() == 1
    assert s.seed() == 0
    path.write_text(json.dumps({'timestamp': '2026-10-29T12:00:00+00:00', 'data': {'A': {'dates': ['2026-10-23'], 'last': [200]}}}))
    assert s.seed() == 1
    assert s.read(['A'], '2026-10-23', '2026-10-23')['A']['values'] == [200]


def test_explicit_public_site_without_login(monkeypatch):
    monkeypatch.delenv('APP_USERNAME', raising=False)
    monkeypatch.delenv('APP_PASSWORD', raising=False)
    monkeypatch.setenv('APP_HOST', '0.0.0.0')
    monkeypatch.setenv('ALLOW_PUBLIC_ACCESS', '1')
    with TestClient(create_app(history=FakeHistory()), base_url='https://public.example') as client:
        assert client.get('/').status_code == 200
        assert client.get('/health').json() == {'ok': True}
        assert client.get('/api/instruments?q=A').status_code == 200
        assert client.get('/.env').status_code == 404


def test_public_mode_does_not_override_configured_login(monkeypatch):
    monkeypatch.setenv('APP_USERNAME', 'analyst')
    monkeypatch.setenv('APP_PASSWORD', 'fixture-only')
    monkeypatch.setenv('APP_HOST', '0.0.0.0')
    monkeypatch.setenv('ALLOW_PUBLIC_ACCESS', '1')
    with TestClient(create_app(history=FakeHistory()), base_url='https://public.example') as client:
        assert client.get('/').status_code == 401
        assert client.get('/health', auth=('analyst', 'fixture-only')).status_code == 200
