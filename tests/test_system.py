"""HTTP -> central-agent contract -> PostgreSQL -> restart/cache regression."""
import datetime as dt
import json
import os
import time
import uuid

import httpx
import psycopg
from psycopg import sql
import pytest
from fastapi.testclient import TestClient

from app import create_app
from data_agent import DataAgent
from price_history import PriceHistory


def test_http_agent_database_and_restart(monkeypatch):
    dsn = os.environ.get('TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('TEST_DATABASE_URL required')
    schema = 'rolling_system_' + uuid.uuid4().hex
    with psycopg.connect(dsn) as conn:
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))

    def connect():
        return psycopg.connect(dsn, options='-csearch_path=' + schema)

    calls = []

    def provider(request):
        body = json.loads(request.content)
        calls.append(body)
        assert request.headers['X-Service-Key'] == 'fixture-only'
        assert request.url.path == '/api/v1/bloomberg/bdh'
        bars = [{'date': '2026-10-21', 'PX_LAST': 0},
                {'date': '2026-10-22', 'PX_LAST': 11},
                {'date': '2026-10-23', 'PX_LAST': 12}]
        return httpx.Response(200, json={'data': {
            t: [r for r in bars if body['start_date'] <= r['date'] <= body['end_date']]
            for t in body['securities']}})

    monkeypatch.setenv('APP_USERNAME', 'fixture')
    monkeypatch.setenv('APP_PASSWORD', 'fixture-only')
    url = '/api/history?tickers=AAA%20US%20Equity,BBB%20US%20Equity&from=2026-10-21&to=2026-10-23&predecessor=1'
    now = dt.datetime(2026, 10, 28, 12, tzinfo=dt.timezone.utc).timestamp()
    try:
        with httpx.Client(transport=httpx.MockTransport(provider)) as upstream:
            agent = DataAgent('https://fixture.example', 'fixture-only', client=upstream)
            history = PriceHistory(connect, agent.fetch, clock=lambda: now)
            with TestClient(create_app(history=history)) as client:
                response = client.get(url, auth=('fixture', 'fixture-only'))
                assert response.status_code == 200
                result = response.json()
                deadline = time.monotonic() + 5
                while result['status'] == 'refreshing' and time.monotonic() < deadline:
                    time.sleep(.01)
                    result = client.get(url + '&job=' + result['job'], auth=('fixture', 'fixture-only')).json()
                assert result['status'] == 'complete'
                assert result['data']['AAA US Equity']['last'] == [0, 11, 12]
                assert len(calls) == 1
                assert client.get('/').status_code == 401
                assert client.get('/', auth=('fixture', 'fixture-only')).status_code == 200
            restarted = PriceHistory(connect, agent.fetch, clock=lambda: now)
            with TestClient(create_app(history=restarted)) as client:
                cached = client.get(url, auth=('fixture', 'fixture-only')).json()
                assert cached['status'] == 'cached'
                assert cached['data'] == result['data']
                assert len(calls) == 1
                items = client.get('/api/instruments?q=AAA', auth=('fixture', 'fixture-only')).json()['items']
                assert items[0]['ticker'] == 'AAA US Equity'
    finally:
        with psycopg.connect(dsn) as conn:
            conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
