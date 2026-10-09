import asyncio
import sys
from pathlib import Path
from email.utils import formatdate
import httpx
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'api'))
import main
import weather_retry


@pytest.fixture
def clock(monkeypatch):
    tick = [1000.0]
    monkeypatch.setattr(main, 'weather_lock', asyncio.Lock())
    monkeypatch.setattr(main, 'weather_cache', {'data': None, 'updated_at': 0})
    monkeypatch.setattr(main, 'weather_retry', main.WeatherRetry())
    monkeypatch.setattr(weather_retry.time, 'monotonic', lambda: tick[0])
    monkeypatch.setattr(weather_retry.time, 'time', lambda: tick[0])
    return tick


def provider(monkeypatch, responses):
    calls = []
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, url, params):
            calls.append(params)
            status, headers, body = responses[min(len(calls)-1, len(responses)-1)]
            return httpx.Response(status, headers=headers, json=body, request=httpx.Request('GET', url))
    monkeypatch.setattr(main.httpx, 'AsyncClient', Client)
    return calls


def test_reconnect_http_and_coordinates_share_429_cooldown(monkeypatch, clock):
    calls = provider(monkeypatch, [(429, {}, {'reason':'Daily API request limit exceeded'})])
    async def scenario():
        results = await asyncio.gather(main.weather(), main.weather(), main.weather(39,32), return_exceptions=True)
        assert all(isinstance(result, main.HTTPException) for result in results)
        assert all(result.headers['Retry-After']=='300' for result in results)
        assert all(result.detail=='rate_limited' for result in results)
    asyncio.run(scenario())
    assert len(calls)==1
    clock[0] += 299
    with pytest.raises(main.HTTPException): asyncio.run(main.weather())
    assert len(calls)==1
    clock[0] += 1
    with pytest.raises(main.HTTPException) as error: asyncio.run(main.weather())
    assert error.value.headers['Retry-After']=='600'
    assert len(calls)==2


@pytest.mark.parametrize('header', ['1200', formatdate(2200, usegmt=True)])
def test_provider_retry_after_seconds_or_date(monkeypatch, clock, header):
    calls = provider(monkeypatch, [(429, {'Retry-After':header}, {})])
    with pytest.raises(main.HTTPException) as error: asyncio.run(main.weather())
    assert error.value.headers['Retry-After']=='1200'
    clock[0] += 1199
    with pytest.raises(main.HTTPException): asyncio.run(main.weather())
    assert len(calls)==1


def test_stale_data_keeps_timestamp_and_other_location_is_rejected(monkeypatch, clock):
    cached = {'type':'weather','updatedAt':50,'temperature':22}
    main.weather_cache.update(data=cached, updated_at=50, location=(41.0082,28.9784))
    calls = provider(monkeypatch, [(429, {}, {})])
    result = asyncio.run(main.weather())
    assert result['stale'] and result['updatedAt']==50 and result['retryAfter']==300
    assert result['providerError']=='rate_limited'
    assert asyncio.run(main.weather())==result
    with pytest.raises(main.HTTPException): asyncio.run(main.weather(39,32))
    assert len(calls)==1 and 'stale' not in cached


def test_success_resets_backoff_and_fresh_cache_prevents_calls(monkeypatch, clock):
    good = {'current':{'temperature_2m':22,'relative_humidity_2m':45,'weather_code':0,'wind_speed_10m':5}}
    calls = provider(monkeypatch, [(429, {}, {}), (200, {}, good)])
    with pytest.raises(main.HTTPException): asyncio.run(main.weather())
    clock[0] += 300
    assert asyncio.run(main.weather())['temperature']==22
    assert main.weather_retry.failures==0 and main.weather_retry.remaining()==0
    asyncio.run(main.weather())
    assert len(calls)==2


@pytest.mark.parametrize('status,body', [(500, {}), (200, {'current':{}})])
def test_other_failures_also_coalesce(monkeypatch, clock, status, body):
    calls = provider(monkeypatch, [(status, {}, body)])
    for _ in range(3):
        with pytest.raises(main.HTTPException) as error: asyncio.run(main.weather())
        assert error.value.headers['Retry-After']=='60'
    assert len(calls)==1


@pytest.mark.parametrize('value', ['nonsense', 'nan', '-1', 'inf'])
def test_bad_retry_after_uses_safe_fallback(value):
    assert weather_retry.retry_after_seconds(value)==0


def test_backoff_is_bounded_without_provider_header(clock):
    assert [main.weather_retry.fail(429) for _ in range(9)] == [300,600,1200,2400,3600,3600,3600,3600,3600]


def test_device_refresh_events_cannot_skip_real_provider_cooldown(monkeypatch):
    monkeypatch.setattr(main, 'weather_retry', main.WeatherRetry())
    monkeypatch.setattr(main, 'weather_lock', asyncio.Lock())
    monkeypatch.setattr(main, 'weather_refresh', asyncio.Event())
    monkeypatch.setattr(main, 'weather_cache', {'data':None,'updated_at':0})
    monkeypatch.setattr(main, 'weather_enabled', True)
    monkeypatch.setattr(main, 'manager', main.ConnectionManager())
    calls = provider(monkeypatch, [(429, {}, {})])
    async def scenario():
        main.manager.active_connections.add(object())
        messages = asyncio.Queue()
        async def broadcast(message, sender): await messages.put(__import__('json').loads(message))
        monkeypatch.setattr(main.manager, 'broadcast', broadcast)
        task = asyncio.create_task(main.refresh_weather_loop())
        try:
            first = await asyncio.wait_for(messages.get(), 1)
            assert first['error']=='rate_limited' and first['retryAfter']==300
            for _ in range(3):
                main.weather_refresh.set()
                again = await asyncio.wait_for(messages.get(), 1)
                assert again['error']=='rate_limited' and 299 <= again['retryAfter'] <= 300
            assert len(calls)==1
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())
