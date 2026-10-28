import json
import httpx
import pytest
from data_agent import DataAgent, AgentError


def test_bdh_contract_and_null_is_preserved():
    requests = []
    def provider(request):
        requests.append(request)
        return httpx.Response(200, json={'data': {'A US Equity': [
            {'date': '2026-10-23', 'PX_LAST': None},
            {'date': '2026-10-22', 'PX_LAST': 0}]}})
    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        agent = DataAgent('https://pool.example', 'test-key', client=client)
        data = agent.fetch(['A US Equity'], '2026-10-22', '2026-10-23')
    assert data['A US Equity'][0]['PX_LAST'] is None
    assert data['A US Equity'][1]['PX_LAST'] == 0
    request = requests[0]
    assert str(request.url) == 'https://pool.example/api/v1/bloomberg/bdh'
    assert request.headers['X-Service-Key'] == 'test-key'
    assert json.loads(request.content) == {
        'securities': ['A US Equity'], 'fields': ['PX_LAST'],
        'start_date': '2026-10-22', 'end_date': '2026-10-23', 'periodicity': 'DAILY'}
    assert request.extensions['timeout']['read'] <= 30


@pytest.mark.parametrize('status,attempts', [(503, 3), (401, 1), (500, 1), (302, 1)])
def test_bounded_retries_and_sanitized_errors(status, attempts):
    calls, delays = [], []
    def provider(request):
        calls.append(request)
        return httpx.Response(status, text='SECRET upstream host and key')
    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        agent = DataAgent('https://pool.example', 'SECRET', client=client, sleep=delays.append)
        with pytest.raises(AgentError) as error:
            agent.fetch(['A'], '2026-10-23', '2026-10-23')
    assert 'SECRET' not in str(error.value)
    assert len(calls) == attempts
    assert sum(delays) < 2


def test_503_then_success():
    count = 0
    def provider(request):
        nonlocal count
        count += 1
        return httpx.Response(503 if count == 1 else 200, json={'data': {'A': []}})
    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        assert DataAgent('https://pool.example', 'key', client=client, sleep=lambda _: None).fetch(['A'], '2026-10-23', '2026-10-23') == {'A': []}


@pytest.mark.parametrize('payload', [None, [], {'data': []}, {'unexpected': 'SECRET'}])
def test_bad_envelope_is_sanitized(payload):
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))) as client:
        with pytest.raises(AgentError, match='invalid response'):
            DataAgent('https://pool.example', 'key', client=client).fetch(['A'], '2026-10-23', '2026-10-23')


def test_timeout_is_sanitized():
    def provider(request):
        raise httpx.ReadTimeout('SECRET', request=request)
    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        with pytest.raises(AgentError) as error:
            DataAgent('https://pool.example', 'key', client=client).fetch(['A'], '2026-10-23', '2026-10-23')
    assert 'SECRET' not in str(error.value)
