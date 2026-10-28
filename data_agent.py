"""Server-only central data pool client; never uses a local Bloomberg terminal.

Contract: bundled jcap-tool-data/references/api.md. BDH's data mapping is
returned unchanged, including null observations. Instrument search follows
ES's central instrument-search contract, with no asset-class filter.
"""
import os
import time
from urllib.parse import urlsplit

import httpx


class AgentError(RuntimeError):
    """Safe to show to callers; never includes upstream bodies or credentials."""


class DataAgent:
    def __init__(self, url=None, key=None, *, client=None, sleep=time.sleep):
        self.url = (url if url is not None else os.environ.get('JCAP_DATA_URL', '')).rstrip('/')
        self._key = key if key is not None else os.environ.get('JCAP_SERVICE_KEY', '')
        parsed = urlsplit(self.url)
        if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('JCAP_DATA_URL must be an HTTP(S) base URL without credentials')
        if not self._key or '\n' in self._key or '\r' in self._key:
            raise ValueError('JCAP_SERVICE_KEY is required')
        self._client, self._sleep = client, sleep

    def fetch(self, securities, start_date, end_date):
        body = {'securities': list(securities), 'fields': ['PX_LAST'],
                'start_date': start_date, 'end_date': end_date, 'periodicity': 'DAILY'}
        if not 1 <= len(body['securities']) <= 12:
            raise ValueError('BDH batches require 1 to 12 securities')
        if self._client is None:
            with httpx.Client(trust_env=False) as client:
                return self._fetch(client, body)
        return self._fetch(self._client, body)

    __call__ = fetch

    def search(self, query):
        if not isinstance(query, str) or len(query) > 64:
            raise ValueError('Instrument search query must be at most 64 characters')
        query = ' '.join(query.split())
        if len(query) < 2:
            return {'results': []}
        body = {'query': query, 'max_results': 10, 'security_type': None}
        if self._client is None:
            with httpx.Client(trust_env=False) as client:
                return self._search(client, body)
        return self._search(self._client, body)

    def _search(self, client, body):
        try:
            for attempt in range(3):
                response = client.post(
                    self.url + '/api/v1/bloomberg/instrument-search', json=body,
                    headers={'X-Service-Key': self._key},
                    timeout=httpx.Timeout(20, connect=5), follow_redirects=False)
                if response.status_code == 503 and attempt < 2:
                    self._sleep(.2 * (attempt + 1))
                    continue
                if response.status_code != 200:
                    raise AgentError('Central instrument search unavailable; saved results retained.')
                payload = response.json()
                if (not isinstance(payload, dict) or not isinstance(payload.get('data'), dict)
                        or not isinstance(payload['data'].get('results'), list)):
                    raise AgentError('Central instrument search returned an invalid response.')
                return {'results': payload['data']['results']}
        except (httpx.HTTPError, ValueError, TypeError):
            raise AgentError('Central instrument search returned an invalid response or is unavailable.') from None

    def _fetch(self, client, body):
        try:
            for attempt in range(3):
                response = client.post(
                    self.url + '/api/v1/bloomberg/bdh', json=body,
                    headers={'X-Service-Key': self._key},
                    timeout=httpx.Timeout(20, connect=5), follow_redirects=False)
                if response.status_code == 503 and attempt < 2:
                    self._sleep(.2 * (attempt + 1))
                    continue
                if response.status_code != 200:
                    raise AgentError('Central price provider unavailable; saved prices retained.')
                payload = response.json()
                if not isinstance(payload, dict) or not isinstance(payload.get('data'), dict):
                    raise AgentError('Central price provider returned an invalid response.')
                return payload['data']
        except (httpx.HTTPError, ValueError, TypeError):
            raise AgentError('Central price provider returned an invalid response or is unavailable.') from None
