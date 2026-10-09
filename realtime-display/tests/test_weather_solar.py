import asyncio
from datetime import datetime, timezone
from pathlib import Path
import sys

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'api'))
import main

def epoch(text):
    return int(datetime.fromisoformat(text).timestamp())

@pytest.fixture
def provider(monkeypatch):
    start = epoch('2026-10-05T00:00:00+03:00')
    data = {'utc_offset_seconds': 10800, 'current': {'temperature_2m': 21, 'relative_humidity_2m': 45, 'weather_code': 0, 'wind_speed_10m': 7}, 'daily': {
        'time': [start, start+86400],
        'sunrise': [epoch('2026-10-05T07:12:00+03:00'), epoch('2026-10-06T07:13:00+03:00')],
        'sunset': [epoch('2026-10-05T18:41:00+03:00'), epoch('2026-10-06T18:40:00+03:00')],
        'weather_code': [0, 3], 'temperature_2m_max': [22, 20], 'temperature_2m_min': [12, 13],
        'precipitation_probability_max': [0, 10]}}
    calls = []
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, url, params):
            calls.append(params)
            return httpx.Response(200, json=data, request=httpx.Request('GET',url))
    monkeypatch.setattr(main.httpx, 'AsyncClient', Client)
    monkeypatch.setattr(main, 'weather_cache', {'data': None, 'updated_at': 0, 'location': None})
    return data, calls

def test_solar_utc_epochs_and_local_forecast_dates(provider):
    data, calls = provider
    result = asyncio.run(main.weather())
    assert len(calls) == 1
    assert {'sunrise', 'sunset'} <= set(calls[0]['daily'].split(','))
    assert calls[0]['timeformat'] == 'unixtime'
    assert calls[0]['timezone'] == 'auto'
    assert result['solar']['days'][0] == {'start': data['daily']['time'][0],
        'sunrise': data['daily']['sunrise'][0], 'sunset': data['daily']['sunset'][0]}
    assert datetime.fromtimestamp(result['solar']['days'][0]['sunrise'],timezone.utc).hour == 4
    assert result['forecast'][0]['day'] == '10/05'  # UTC calendar date is still Oct 4 at local midnight.
    assert result['solar']['latitude'] == 41.0082
    assert result['solar']['longitude'] == 28.9784
    assert asyncio.run(main.weather()) == result
    assert len(calls) == 1

def test_coordinate_change_never_reuses_other_locations_sun_times(provider):
    _, calls = provider
    asyncio.run(main.weather())
    result = asyncio.run(main.weather(lat=39.9334, lon=32.8597))
    assert len(calls) == 2
    assert result['solar']['latitude'] == 39.9334
    assert calls[1]['longitude'] == 32.8597

def test_missing_polar_or_invalid_solar_keeps_weather_working(provider):
    data, _ = provider
    data['daily']['sunrise'] = [None, data['daily']['sunset'][1]+1]
    result = asyncio.run(main.weather())
    assert result['solar']['days'] == []
    assert result['temperature'] == 21 and len(result['forecast']) == 2

def test_provider_failure_retains_same_location_but_rejects_other_location(provider, monkeypatch):
    original = asyncio.run(main.weather())
    main.weather_cache['updated_at'] = 0
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, *args, **kwargs): raise httpx.ConnectError('offline')
    monkeypatch.setattr(main.httpx, 'AsyncClient', Client)
    cached = asyncio.run(main.weather())
    assert cached['temperature'] == original['temperature']
    assert cached['updatedAt'] == original['updatedAt']
    assert cached['stale'] is True
    with pytest.raises(main.HTTPException) as error:
        asyncio.run(main.weather(lat=0,lon=0))
    assert error.value.status_code == 502
