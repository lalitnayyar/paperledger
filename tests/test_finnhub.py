"""Finnhub and source modes: all provider requests use a mock transport."""
import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import db, trading
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.delenv('APP_ENCRYPTION_KEY', raising=False)
    with TestClient(app) as instance:
        yield instance


@pytest.fixture
def feeds(monkeypatch):
    state = {'calls': [], 'times': [], 'responses': {}}
    original = httpx.AsyncClient

    def handler(request):
        state['calls'].append(request)
        state['times'].append(time.monotonic())
        symbol = request.url.params['symbol']
        result = state['responses'].get(symbol)
        if isinstance(result, Exception):
            raise result
        if isinstance(result, httpx.Response):
            return result
        if result is None:
            if request.url.host == 'finnhub.io':
                result = {'c': 154.25, 'dp': -1.5, 't': int(time.time())}
            else:
                result = {'Global Quote': {'01. symbol': symbol, '05. price': '77', '06. volume': '10',
                                           '07. latest trading day': '2026-01-01', '10. change percent': '1%'}}
        return httpx.Response(200, json=result)

    def factory(**kwargs):
        assert kwargs['follow_redirects'] is False and kwargs['trust_env'] is False
        kwargs['transport'] = httpx.MockTransport(handler)
        return original(**kwargs)
    monkeypatch.setattr(trading.httpx, 'AsyncClient', factory)
    return state


def set_finnhub(client, symbols=('IBM',)):
    assert client.put('/api/market-data/finnhub', json={'api_key': 'fake-secret-finnhub-123'}).json() == {'has_api_key': True}
    assert client.put('/api/settings', json={'market_mode': 'stocks', 'stock_source_mode': 'api',
                                            'stock_provider': 'finnhub', 'stock_symbols': list(symbols)}).status_code == 200


def test_settings_credentials_separate_encrypted_and_redacted(client, tmp_path, feeds, caplog):
    secret = 'fake-secret-finnhub-123'
    caplog.set_level(logging.INFO)
    assert client.get('/api/dashboard').json()['settings']['stock_provider'] == 'alpha_vantage'
    assert client.get('/api/dashboard').json()['settings']['stock_source_mode'] == 'both'
    assert client.get('/api/market-data/finnhub').json() == {'has_api_key': False}
    for payload in ({}, {'api_key': ''}, {'api_key': secret, 'clear_key': True},
                    {'api_key': secret, secret: secret}, {'api_key': 'a\nb'}, {'api_key': 123}):
        response = client.put('/api/market-data/finnhub', json=payload)
        assert response.status_code == 422 and secret not in response.text
    assert client.put('/api/market-data', json={'api_key': 'fake-alpha'}).status_code == 200
    set_finnhub(client)
    assert secret.encode() not in (tmp_path / 'providers.sqlite3').read_bytes()
    with sqlite3.connect(tmp_path / 'providers.sqlite3') as conn:
        encrypted = conn.execute('SELECT encrypted_key FROM finnhub_market_credentials').fetchone()[0]
    assert Fernet((tmp_path / '.encryption-key').read_bytes()).decrypt(encrypted).decode() == secret
    scan = client.post('/api/scan')
    assert scan.status_code == 200
    request = feeds['calls'][0]
    assert str(request.url) == 'https://finnhub.io/api/v1/quote?symbol=IBM'
    assert request.headers['X-Finnhub-Token'] == secret and secret not in str(request.url)
    assert secret not in scan.text + client.get('/api/dashboard').text + caplog.text
    assert client.put('/api/market-data/finnhub', json={'clear_key': True}).json() == {'has_api_key': False}
    assert client.get('/api/market-data').json()['has_api_key'] is True


@pytest.mark.parametrize('payload', [{}, {'c': 10}, {'c': 10, 't': 0}, {'c': 10, 't': None},
                                     {'c': 10, 't': True}, {'c': 10, 't': 1.5},
                                     {'c': 10, 't': 10**17}, {'c': 0, 't': 100},
                                     {'c': 'NaN', 't': 100}, {'c': 10, 't': -1}])
def test_invalid_quote_fails_closed(payload):
    assert trading._finnhub_candidate(payload, 'IBM', db.utc_now()) is None


def test_parser_utc_date_timestamp_and_no_volume():
    fetched = '2026-01-02T12:30:00Z'
    item = trading._finnhub_candidate({'c': 100.25, 'dp': -2.25, 't': 1767225599, 'v': 987}, 'IBM', fetched)
    assert item['observed_at'] == fetched
    assert item['quote_timestamp'] == '2025-12-31T23:59:59Z'
    assert item['quote_date'] == '2025-12-31' and item['stock_volume_shares'] is None
    assert item['stock_change_percent'] == '-2.25' and item['risk_status'] == 'manual only'
    assert 'warning' in item['reason'].lower() and 'not current' in item['reason'].lower()
    assert trading._finnhub_candidate({'c': 10, 't': 1767225600}, 'IBM', fetched)['stock_change_percent'] is None


def test_25_sequential_header_only_requests_then_five_minute_cache(client, feeds):
    symbols = [chr(ord('A') + i) for i in range(25)]
    set_finnhub(client, symbols)
    first = client.post('/api/scan')
    assert first.status_code == 200 and len(first.json()['candidates']) == 25
    assert len(feeds['calls']) == 25
    assert all(b - a >= .045 for a, b in zip(feeds['times'], feeds['times'][1:]))
    assert all(set(r.url.params) == {'symbol'} and r.url.host == 'finnhub.io' for r in feeds['calls'])
    for item in first.json()['candidates']:
        assert item['source'] == 'Finnhub' and item['cache_status'] == 'fresh'
        assert item['stock_volume_shares'] is None and item['volume_24h'] is None
        assert item['quote_timestamp'] and item['observed_at']
    second = client.post('/api/scan').json()
    assert len(feeds['calls']) == 25 and all(x['cache_status'] == 'cached' for x in second['candidates'])
    assert [(x['observed_at'], x['quote_timestamp']) for x in first.json()['candidates']] == [(x['observed_at'], x['quote_timestamp']) for x in second['candidates']]
    assert client.get('/api/dashboard').json()['candidates'] == second['candidates']
    with db.read_connection() as conn:
        assert conn.execute('SELECT count(*) FROM stock_quote_cache WHERE provider=?', (db.FINNHUB_QUOTE_CACHE,)).fetchone()[0] == 25


@pytest.mark.parametrize('status', [401, 403, 429])
def test_stop_on_provider_backpressure_preserves_stale_cache_and_redacts(client, feeds, status):
    set_finnhub(client, ['IBM', 'AAPL', 'MSFT'])
    first = client.post('/api/scan').json()['candidates']
    old = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat().replace('+00:00', 'Z')
    with db.transaction() as conn:
        conn.execute('UPDATE stock_quote_cache SET observed_at=? WHERE provider=?', (old, db.FINNHUB_QUOTE_CACHE))
        conn.execute("UPDATE candidates SET observed_at=? WHERE source='Finnhub'", (old,))
    assert all(c['cache_status'] == 'stale' for c in client.get('/api/dashboard').json()['candidates'])
    feeds['calls'].clear()
    feeds['responses']['IBM'] = httpx.Response(status, text='private upstream fake-secret-finnhub-123')
    response = client.post('/api/scan')
    assert response.status_code == 200
    result = response.json()
    assert len(feeds['calls']) == 1 and str(status) in response.text
    assert all(c['cache_status'] == 'stale' and c['observed_at'] == old for c in result['candidates'])
    assert [c['quote_timestamp'] for c in result['candidates']] == [c['quote_timestamp'] for c in first]
    assert 'MSFT' in response.text and 'skipped' in response.text
    assert 'fake-secret-finnhub-123' not in response.text and 'private upstream' not in response.text
    if status == 429:
        assert 'no reset time is assumed' in response.text


def test_old_provider_quote_warning_even_when_fetched_fresh(client, feeds):
    set_finnhub(client)
    feeds['responses']['IBM'] = {'c': 7, 't': 1577836800}
    response = client.post('/api/scan')
    assert response.status_code == 200
    item = response.json()['candidates'][0]
    assert item['cache_status'] == 'fresh' and item['quote_date'] == '2020-01-01'
    assert 'old' in ' '.join(response.json()['warnings']).lower()
    assert 'not current' in client.get('/api/dashboard').json()['candidates'][0]['reason']


def test_no_key_no_fallback_and_provider_switch_does_not_cross_cache(client, feeds):
    client.put('/api/settings', json={'market_mode': 'stocks', 'stock_source_mode': 'api'})
    assert client.post('/api/scan').json()['candidates'][0]['source'] == 'Alpha Vantage'
    client.put('/api/settings', json={'stock_provider': 'finnhub'})
    assert client.get('/api/dashboard').json()['candidates'] == []
    assert client.post('/api/scan').status_code == 502
    assert len(feeds['calls']) == 1
    assert client.put('/api/market-data/finnhub', json={'api_key': 'key'}).status_code == 200
    assert client.post('/api/scan').json()['candidates'][0]['source'] == 'Finnhub'
    client.put('/api/settings', json={'stock_provider': 'alpha_vantage'})
    assert client.get('/api/dashboard').json()['candidates'] == []
    alpha = client.post('/api/scan').json()['candidates'][0]
    assert alpha['source'] == 'Alpha Vantage' and alpha['cache_status'] == 'cached'
    assert len(feeds['calls']) == 2


def test_inflight_provider_or_mode_change_aborts_without_saving(client, feeds, monkeypatch):
    set_finnhub(client)
    original = trading._scan_finnhub
    async def changing(symbols):
        result = await original(symbols)
        trading.update_settings({'stock_provider': 'alpha_vantage'})
        return result
    monkeypatch.setattr(trading, '_scan_finnhub', changing)
    response = client.post('/api/scan')
    assert response.status_code == 502 and 'changed during scan' in response.text
    with db.read_connection() as conn:
        assert conn.execute('SELECT count(*) FROM stock_quote_cache').fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM candidates').fetchone()[0] == 0


def test_csv_modes_and_synthetic_never_use_network_or_ledger_positions(client, feeds):
    client.put('/api/settings', json={'market_mode': 'stocks', 'stock_source_mode': 'csv', 'auto_paper': True})
    assert client.post('/api/scan').status_code == 502
    assert client.post('/api/trades', json={'symbol': 'IBM', 'asset_type': 'stock', 'side': 'BUY', 'quantity': 1, 'price': 10, 'reason': 'manual'}).status_code == 200
    assert client.post('/api/stock-import', json={'csv': 'Symbol,Price\nIBM,22\nAAPL,30\n'}).status_code == 200
    result = client.post('/api/cycle').json()
    assert len(result['candidates']) == 2 and result['trades'] == []
    assert all(c['source'] == 'User CSV import' for c in result['candidates'])
    assert feeds['calls'] == []
    client.put('/api/settings', json={'stock_source_mode': 'simulation'})
    result = client.post('/api/cycle').json()
    assert result['trades'] == [] and result['candidates'][0]['source'] == 'Simulation'
    assert result['candidates'][0]['quote_timestamp'] is None and result['candidates'][0]['cache_status'] == 'simulated'
    assert all(c['source'] == 'Simulation' for c in client.get('/api/dashboard').json()['candidates'])
    assert client.post('/api/simulation/stocks/cycle').status_code == 200
    data = client.get('/api/dashboard').json()
    assert data['positions'][0]['mark_price'] is None and len(data['trades']) == 1
    assert feeds['calls'] == []
    client.put('/api/settings', json={'stock_source_mode': 'csv'})
    assert len(client.get('/api/dashboard').json()['candidates']) == 2
    with db.read_connection() as conn:
        assert conn.execute('SELECT count(*) FROM stock_quote_cache').fetchone()[0] == 0


def test_both_keeps_distinct_same_symbol_quotes_and_csv_on_api_failure(client, feeds):
    set_finnhub(client, ['IBM'])
    assert client.put('/api/settings', json={'stock_source_mode': 'both'}).status_code == 200
    assert client.post('/api/stock-import', json={'csv': 'Symbol,Price\nIBM,999\nAAPL,12\n'}).json()['imported'] == 2
    response = client.post('/api/scan')
    assert response.status_code == 200
    scan = response.json()
    assert [(c['symbol'], c['source'], c['price_usd'], c['cache_status']) for c in scan['candidates']] == [
        ('IBM', 'Finnhub', 154.25, 'fresh'), ('IBM', 'User CSV import', 999, 'manual'),
        ('AAPL', 'User CSV import', 12, 'manual')]
    assert all(c['risk_status'] == 'manual only' for c in scan['candidates'])
    assert client.get('/api/dashboard').json()['candidates'] == scan['candidates']
    with db.transaction() as conn:
        conn.execute('DELETE FROM stock_quote_cache')
    feeds['responses']['IBM'] = httpx.Response(429, text='private')
    result = client.post('/api/scan')
    assert result.status_code == 200 and len(result.json()['candidates']) == 2
    assert all(c['source'] == 'User CSV import' for c in result.json()['candidates'])
    assert [(c['symbol'], c['price_usd'], c['cache_status']) for c in result.json()['candidates']] == [
        ('IBM', 999, 'manual'), ('AAPL', 12, 'manual')]
    assert '429' in result.text and 'no quotes' in result.text
    assert '429' in ' '.join(result.json()['warnings']) and 'no quotes' in ' '.join(result.json()['warnings'])
    assert 'private' not in result.text
    assert client.get('/api/dashboard').json()['candidates'] == result.json()['candidates']


def test_additive_migrations_preserve_old_cache_and_candidates(client):
    client.put('/api/settings', json={'market_mode': 'stocks'})
    observed = db.utc_now()
    with db.transaction() as conn:
        conn.execute("INSERT INTO candidates(symbol,chain,pair_address,price_usd,risk_status,reason,observed_at,asset_type,source,quote_date,stock_volume_shares) VALUES('IBM','','','11','manual only','legacy',?,'stock','Alpha Vantage','2020-01-01',1)", (observed,))
        db.put_stock_quote_cache(conn, provider=db.ALPHA_VANTAGE_QUOTE_CACHE, item={'symbol': 'IBM', 'price_usd': '11', 'observed_at': observed, 'quote_date': '2020-01-01', 'stock_volume_shares': 1})
        conn.execute('ALTER TABLE candidates DROP COLUMN quote_timestamp')
        conn.execute('ALTER TABLE stock_quote_cache DROP COLUMN quote_timestamp')
    db.init_db()
    db.init_db()
    with db.read_connection() as conn:
        candidate = conn.execute('SELECT * FROM candidates').fetchone()
        cache = conn.execute('SELECT * FROM stock_quote_cache').fetchone()
        assert candidate['price_usd'] == cache['price_usd'] == '11'
        assert candidate['observed_at'] == cache['observed_at'] == observed
        assert candidate['quote_timestamp'] is cache['quote_timestamp'] is None


def test_finnhub_redirect_error_and_decryption_fail_closed(client, feeds, monkeypatch):
    set_finnhub(client, ['IBM', 'AAPL'])
    feeds['responses']['IBM'] = httpx.Response(302, headers={'Location': 'https://evil.example/steal'})
    response = client.post('/api/scan')
    assert response.status_code == 502 and len(feeds['calls']) == 1
    assert all(r.url.host == 'finnhub.io' for r in feeds['calls'])
    feeds['calls'].clear()
    monkeypatch.setenv('APP_ENCRYPTION_KEY', Fernet.generate_key().decode())
    assert client.get('/api/market-data/finnhub').status_code == 503
    result = client.post('/api/scan')
    assert result.status_code == 502 and 'decrypt' in result.text and feeds['calls'] == []
    assert 'fake-secret-finnhub-123' not in response.text + result.text


def test_stale_cache_after_invalid_quote_and_exact_five_minute_boundary(client, feeds):
    set_finnhub(client)
    item = client.post('/api/scan').json()['candidates'][0]
    now = datetime.now(timezone.utc)
    assert not trading._finnhub_cache_is_fresh((now - timedelta(minutes=5)).isoformat(), now)
    assert trading._finnhub_cache_is_fresh((now - timedelta(minutes=4, seconds=59)).isoformat(), now)
    old = (now - timedelta(minutes=6)).isoformat().replace('+00:00', 'Z')
    with db.transaction() as conn:
        conn.execute('UPDATE stock_quote_cache SET observed_at=? WHERE provider=?', (old, db.FINNHUB_QUOTE_CACHE))
    feeds['responses']['IBM'] = {'c': 100, 't': 0, 'error': 'private upstream fake-secret-finnhub-123'}
    scan = client.post('/api/scan')
    assert scan.status_code == 200
    saved = scan.json()['candidates'][0]
    assert saved['cache_status'] == 'stale' and saved['quote_timestamp'] == item['quote_timestamp']
    assert 'invalid quote' in scan.text.lower() and 'fake-secret-finnhub-123' not in scan.text


def test_source_mode_change_inflight_aborts(client, feeds, monkeypatch):
    set_finnhub(client)
    original = trading._scan_finnhub
    async def change_mode(symbols):
        result = await original(symbols)
        trading.update_settings({'stock_source_mode': 'csv'})
        return result
    monkeypatch.setattr(trading, '_scan_finnhub', change_mode)
    result = client.post('/api/scan')
    assert result.status_code == 502 and 'changed during scan' in result.text
    with db.read_connection() as conn:
        assert conn.execute('SELECT count(*) FROM candidates').fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM stock_quote_cache').fetchone()[0] == 0


def test_standard_user_can_run_simulation_cycle_but_needs_csrf(tmp_path, monkeypatch):
    from app import auth
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setenv('APP_AUTH_ENABLED', '1')
    monkeypatch.setenv('APP_BIND_ADDR', '0.0.0.0')
    monkeypatch.setattr(auth, 'limiter', auth.LoginLimiter())
    auth.create_user('Admin', 'secure-admin-pass-123', 'admin')
    auth.create_user('Trader', 'secure-trader-pass-123', 'user')
    with TestClient(app) as admin:
        assert admin.post('/api/auth/login', json={'username': 'Admin', 'password': 'secure-admin-pass-123'}).status_code == 200
        csrf = admin.get('/api/auth/session').json()['csrf']
        assert admin.put('/api/settings', json={'stock_source_mode': 'simulation', 'market_mode': 'stocks'}, headers={'X-CSRF-Token': csrf}).status_code == 200
    with TestClient(app) as user:
        assert user.post('/api/auth/login', json={'username': 'Trader', 'password': 'secure-trader-pass-123'}).status_code == 200
        assert user.post('/api/simulation/stocks/cycle').status_code == 403
        csrf = user.get('/api/auth/session').json()['csrf']
        response = user.post('/api/simulation/stocks/cycle', headers={'X-CSRF-Token': csrf})
        assert response.status_code == 200 and response.json()['step'] == 1
        assert user.put('/api/settings', json={'stock_source_mode': 'api'}, headers={'X-CSRF-Token': csrf}).status_code == 403


def test_transport_debug_filter_is_request_scoped(caplog):
    from app import market_data
    logger = logging.getLogger('httpcore.http11')
    caplog.set_level(logging.DEBUG, logger='httpcore.http11')
    logger.debug('unrelated trace before')
    with market_data.private_finnhub_request():
        logger.debug('response headers contain fake-secret-finnhub-123')
    logger.debug('unrelated trace after')
    assert 'fake-secret-finnhub-123' not in caplog.text
    assert 'unrelated trace before' in caplog.text and 'unrelated trace after' in caplog.text
