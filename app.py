"""Standalone same-origin API. Run: uvicorn app:create_app --factory --host 127.0.0.1.

DATABASE_URL points to PostgreSQL; JCAP_DATA_URL and JCAP_SERVICE_KEY configure
the central pool. APP_USERNAME and APP_PASSWORD must be set together. Local
development permits loopback access without them. For public deployment set
APP_HOST=0.0.0.0 (or the public bind address), configure both credentials or
explicitly enable ALLOW_PUBLIC_ACCESS=1, and terminate TLS at a reverse proxy.
Preserve the public Host header at the proxy.
ALLOW_PUBLIC_ACCESS=1 explicitly enables a site without login.
APIs, health and static files all use the same authentication policy. Use one
server worker for uninterrupted polling: jobs are process-local, prices,
coverage and catalog are durable, and fills coordinate across all processes.
"""
import base64
import binascii
from contextlib import asynccontextmanager
import hmac
import ipaddress
import os
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse
import psycopg

from data_agent import DataAgent
from instrument_resolver import InstrumentResolver
from price_history import PriceHistory


STATIC_ROOT = Path(__file__).resolve().parent / 'static'


def _loopback(host):
    if host in ('localhost', 'testclient'):
        return True
    try:
        return ipaddress.ip_address(host or '').is_loopback
    except ValueError:
        return False


def create_app(history=None, agent=None, resolver=None):
    username, password = os.environ.get('APP_USERNAME', ''), os.environ.get('APP_PASSWORD', '')
    public_access = os.environ.get('ALLOW_PUBLIC_ACCESS', '0') == '1'
    if bool(username) != bool(password):
        raise ValueError('APP_USERNAME and APP_PASSWORD must both be configured')
    if not _loopback(os.environ.get('APP_HOST', '127.0.0.1')) and not username and not public_access:
        raise ValueError('Public deployment requires APP_USERNAME and APP_PASSWORD')

    @asynccontextmanager
    async def lifespan(application):
        provider = agent
        if application.state.history is None:
            dsn = os.environ.get('DATABASE_URL', '')
            if not dsn:
                raise RuntimeError('DATABASE_URL is required')
            try:
                provider = agent if agent is not None else DataAgent()
                fetch = provider.fetch if hasattr(provider, 'fetch') else provider
                application.state.history = PriceHistory(
                    lambda: psycopg.connect(dsn, connect_timeout=5,
                                            options='-c statement_timeout=30000'), fetch)
            except Exception:
                raise RuntimeError('Backend initialization failed; check database and provider configuration') from None
        if (application.state.resolver is None and provider is not None
                and hasattr(provider, 'search') and hasattr(application.state.history, 'connect')):
            try:
                application.state.resolver = InstrumentResolver(application.state.history, provider)
            except Exception:
                raise RuntimeError('Instrument resolver initialization failed; check database configuration') from None
        yield

    application = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    application.state.history = history
    application.state.resolver = resolver

    @application.middleware('http')
    async def authentication(request: Request, call_next):
        if username:
            authorized = False
            scheme, _, encoded = request.headers.get('authorization', '').partition(' ')
            if scheme.lower() == 'basic':
                try:
                    user, sep, secret = base64.b64decode(encoded, validate=True).decode('utf-8').partition(':')
                    user_ok = hmac.compare_digest(user.encode(), username.encode())
                    password_ok = hmac.compare_digest(secret.encode(), password.encode())
                    authorized = bool(sep) and user_ok and password_ok
                except (binascii.Error, UnicodeError, ValueError):
                    pass
            if not authorized:
                return JSONResponse({'ok': False, 'error': 'Authentication required'}, status_code=401,
                                    headers={'WWW-Authenticate': 'Basic realm="Rolling Analysis", charset="UTF-8"',
                                             'Cache-Control': 'no-store'})
        elif not public_access and not (_loopback(request.url.hostname) and request.client and _loopback(request.client.host)):
            return JSONResponse({'ok': False, 'error': 'Public access requires authentication configuration'},
                                status_code=503, headers={'Cache-Control': 'no-store'})
        try:
            response = await call_next(request)
        except Exception:
            response = JSONResponse({'ok': False, 'error': 'Backend unavailable'}, status_code=503)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    @application.get('/health')
    def health(request: Request):
        try:
            if request.app.state.history.health():
                return {'ok': True}
        except Exception:
            pass
        return JSONResponse({'ok': False}, status_code=503)

    @application.get('/api/history')
    def history_route(request: Request, tickers: str = Query(..., max_length=8000),
                      start: str = Query(..., alias='from', max_length=10),
                      end: str = Query(..., alias='to', max_length=10),
                      job: str | None = Query(None, max_length=64), predecessor: bool = False):
        try:
            names = [t.strip() for t in tickers.split(',') if t.strip()]
            result = request.app.state.history.request(names, start, end, job, predecessor=predecessor)
            return {**result, 'data': {
                t: {'dates': series['dates'], 'last': series['values']}
                for t, series in result['data'].items()}}
        except (ValueError, TypeError):
            return JSONResponse({'ok': False, 'error': 'Invalid history request; check tickers, dates and job'}, status_code=400)
        except Exception:
            return JSONResponse({'ok': False, 'error': 'History database unavailable; retry shortly'}, status_code=503)

    @application.get('/api/instruments')
    def instruments(request: Request, q: str = Query('', max_length=64)):
        query = q.strip()
        if not query:
            return {'ok': True, 'items': []}
        try:
            if request.app.state.resolver is not None:
                return request.app.state.resolver.search(query)
            return {'ok': True, 'items': request.app.state.history.instruments(query)}
        except Exception:
            return JSONResponse({'ok': False, 'error': 'Instrument catalog unavailable'}, status_code=503)

    def static_file(path):
        root = STATIC_ROOT.resolve()
        target = (root / path).resolve()
        allowed = {'.html', '.js', '.css', '.png', '.svg', '.ico', '.woff', '.woff2'}
        if (not target.is_relative_to(root) or any(part.startswith('.') for part in Path(path).parts)
                or target.suffix.lower() not in allowed or not target.is_file()):
            return JSONResponse({'error': 'Not found'}, status_code=404)
        return FileResponse(target)

    @application.get('/')
    def index():
        return static_file('index.html')

    @application.get('/static/{path:path}')
    def static(path: str):
        return static_file(path)

    # Support the extracted UI's same-directory asset references, explicitly.
    @application.get('/{asset}')
    def asset(asset: str):
        if asset not in ('index.html', 'rolling-change.js', 'rolling-change.css', 'chart.min.js', 'favicon.ico'):
            return JSONResponse({'error': 'Not found'}, status_code=404)
        return static_file(asset)

    return application
