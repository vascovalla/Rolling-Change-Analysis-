"""Resolver tests use fake providers and disposable PostgreSQL schemas only."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from fastapi.testclient import TestClient

from app import create_app
from data_agent import AgentError
from instrument_resolver import InstrumentResolver, normalize_security
from price_history import PriceHistory
from test_backend import connect, FakeHistory


class Agent:
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else [
            {'security': 'NVDA US<equity>', 'description': 'NVIDIA CORP'},
            {'security': 'NVDA UW<equity>', 'description': 'NVIDIA CORP'}]
        self.calls = []
        self.error = False

    def search(self, query):
        self.calls.append(query)
        if self.error:
            raise AgentError('SECRET provider credential')
        return {'results': self.rows}


def setup(connect, agent=None):
    history = PriceHistory(connect, lambda *args: {})
    clock = [1000.0]
    agent = agent or Agent()
    return history, agent, clock, InstrumentResolver(history, agent, clock=lambda: clock[0])


@pytest.mark.parametrize('query', ['', ' ', 'a', '  a  '])
def test_short_queries_never_touch_provider_or_catalog(connect, query):
    history, agent, clock, resolver = setup(connect)
    history.instruments = lambda q: pytest.fail('short query used catalog')
    assert resolver.search(query)['items'] == []
    assert not agent.calls


@pytest.mark.parametrize('marker', ['equity', 'corp', 'govt', 'mtge', 'muni', 'pfd', 'curncy', 'index', 'comdty'])
def test_yellow_keys(marker):
    assert normalize_security('  ABC  <' + marker.upper() + '>') == 'ABC ' + marker.capitalize()
    assert normalize_security('  USGG10YR   Index  ') == 'USGG10YR Index'


def test_rank_names_normalization_and_restart_cache(connect):
    history, agent, clock, resolver = setup(connect)
    first = resolver.search('  Nvidia   Corp ')
    assert first['items'] == [
        {'ticker': 'NVDA US Equity', 'name': 'NVIDIA CORP'},
        {'ticker': 'NVDA UW Equity', 'name': 'NVIDIA CORP'}]
    assert not first.get('cached')
    reopened = InstrumentResolver(history, agent, clock=lambda: clock[0])
    assert reopened.search('NVIDIA CORP')['items'] == first['items']
    assert reopened.search('nvidia corp')['cached']
    assert len(agent.calls) == 1
    assert history.instruments('NVIDIA')[0]['name'] == 'NVIDIA CORP'
    with connect() as c:
        assert c.execute('SELECT pg_typeof(items)::text FROM instrument_search_cache').fetchone()[0] == 'jsonb'
    clock[0] += 24 * 3600
    reopened.search('nvidia corp')
    assert len(agent.calls) == 2


def test_negative_cache_expires_in_five_minutes(connect):
    history, agent, clock, resolver = setup(connect, Agent([]))
    assert resolver.search('unmatched')['items'] == []
    clock[0] += 299
    assert resolver.search('UNMATCHED')['cached']
    assert len(agent.calls) == 1
    clock[0] += 1
    resolver.search('unmatched')
    assert len(agent.calls) == 2


def test_stale_if_error_then_catalog_fallback(connect):
    history, agent, clock, resolver = setup(connect)
    expected = resolver.search('nvidia')['items']
    agent.error = True
    clock[0] += 86401
    stale = resolver.search('NVIDIA')
    assert stale['items'] == expected and stale['cached']
    assert stale['warning'] and 'SECRET' not in str(stale)
    fallback = resolver.search('NVDA')
    assert fallback['items'] and fallback['cached'] and 'SECRET' not in str(fallback)
    with connect() as c:
        assert c.execute('SELECT count(*) FROM instrument_search_cache').fetchone()[0] == 1


def test_error_without_saved_catalog_is_safe(connect):
    history, agent, clock, resolver = setup(connect)
    agent.error = True
    result = resolver.search('unknown')
    assert result['items'] == [] and result['warning']
    assert 'SECRET' not in str(result)


@pytest.mark.parametrize('error', [False, True])
def test_identical_concurrent_requests_coalesce(connect, error, monkeypatch):
    import instrument_resolver
    from concurrent.futures import Future
    joined, join_lock = threading.Event(), threading.Lock()
    count = 0
    class WaitingFuture(Future):
        def result(self, timeout=None):
            nonlocal count
            with join_lock:
                count += 1
                if count == 7:
                    joined.set()
            return super().result(timeout)
    monkeypatch.setattr(instrument_resolver, 'Future', WaitingFuture)
    agent = Agent()
    entered, release = threading.Event(), threading.Event()
    original = agent.search
    def search(query):
        entered.set()
        assert release.wait(5)
        return original(query)
    agent.search = search
    agent.error = error
    history, agent, clock, resolver = setup(connect, agent)
    with ThreadPoolExecutor(max_workers=8) as pool:
        first = pool.submit(resolver.search, 'Nvidia')
        assert entered.wait(5)
        # All waiters join the same in-flight operation before it completes.
        waiting = [pool.submit(resolver.search, ' NVIDIA ') for _ in range(7)]
        assert joined.wait(5)
        release.set()
        results = [f.result(5) for f in [first, *waiting]]
    assert all(r['items'] == results[0]['items'] for r in results)
    assert len(agent.calls) == 1


def test_query_length_and_literal_sql_input(connect):
    history, agent, clock, resolver = setup(connect, Agent([]))
    with pytest.raises(ValueError):
        resolver.search('x' * 65)
    assert not agent.calls
    assert resolver.search("'; DROP TABLE instrument_catalog; --")['items'] == []
    assert history.health()


def test_http_injection_blank_guard_and_saved_fallback(monkeypatch):
    monkeypatch.setenv('APP_HOST', '127.0.0.1')
    monkeypatch.delenv('APP_USERNAME', raising=False)
    monkeypatch.delenv('APP_PASSWORD', raising=False)
    history = FakeHistory()
    history.instruments = lambda q: [{'ticker': 'SPX Index', 'name': 'S&P 500'}]
    with TestClient(create_app(history=history), base_url='http://127.0.0.1') as client:
        assert client.get('/api/instruments?q=  ').json()['items'] == []
        assert client.get('/api/instruments?q=A').json()['items']
        assert client.get('/api/instruments', params={'q': 'x' * 65}).status_code == 422
    class Resolver:
        def search(self, query):
            return {'ok': True, 'items': [{'ticker': 'NVDA US Equity', 'name': 'NVIDIA CORP'}], 'cached': True}
    with TestClient(create_app(history=history, resolver=Resolver()), base_url='http://127.0.0.1') as client:
        assert client.get('/api/instruments?q=Nvidia').json()['items'][0]['name'] == 'NVIDIA CORP'
        assert client.get('/api/instruments').json()['items'] == []


def test_http_builds_resolver_with_explicit_agent(connect, monkeypatch):
    monkeypatch.setenv('APP_HOST', '127.0.0.1')
    monkeypatch.delenv('APP_USERNAME', raising=False)
    monkeypatch.delenv('APP_PASSWORD', raising=False)
    history, agent, clock, resolver = setup(connect)
    with TestClient(create_app(history=history, agent=agent), base_url='http://127.0.0.1') as client:
        response = client.get('/api/instruments?q=Nvidia')
        assert response.status_code == 200
        assert response.json()['items'][0]['name'] == 'NVIDIA CORP'
    assert len(agent.calls) == 1


def test_money_market_yellow_key():
    assert normalize_security('US0001M<m-mkt>') == 'US0001M M-Mkt'


def test_live_initialization_builds_resolver(connect, monkeypatch):
    import app
    monkeypatch.setenv('APP_HOST', '127.0.0.1')
    monkeypatch.delenv('APP_USERNAME', raising=False)
    monkeypatch.delenv('APP_PASSWORD', raising=False)
    monkeypatch.setenv('DATABASE_URL', 'fixture-dsn')
    real_history = PriceHistory
    monkeypatch.setattr(app, 'PriceHistory', lambda unused, fetch: real_history(connect, fetch))
    agent = Agent()
    agent.fetch = lambda *args: {}
    monkeypatch.setattr(app, 'DataAgent', lambda: agent)
    with TestClient(create_app(), base_url='http://127.0.0.1') as client:
        assert client.get('/api/instruments?q=Nvidia').json()['items'][0]['name'] == 'NVIDIA CORP'
        assert isinstance(client.app.state.resolver, InstrumentResolver)
    assert len(agent.calls) == 1


@pytest.mark.parametrize('rows', [None, ['SECRET'], [{'security': 42, 'description': 'SECRET'}]])
def test_malformed_search_does_not_poison_cache(connect, rows):
    history, agent, clock, resolver = setup(connect)
    agent.search = lambda q: {'results': rows}
    result = resolver.search('nvidia')
    assert result['items'] == [] and 'SECRET' not in str(result)
    with connect() as c:
        assert c.execute('SELECT count(*) FROM instrument_search_cache').fetchone()[0] == 0
