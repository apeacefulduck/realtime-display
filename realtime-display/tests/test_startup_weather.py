import asyncio
import json
import sys
from pathlib import Path
import httpx
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'api'))
import main
from fastapi.testclient import TestClient

@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(main, 'weather_lock', asyncio.Lock())
    monkeypatch.setattr(main, 'weather_refresh', asyncio.Event())
    monkeypatch.setattr(main, 'weather_cache', {'data': None, 'updated_at': 0, 'location': None})
    monkeypatch.setattr(main, 'weather_enabled', True)
    monkeypatch.setattr(main, 'weather_location', (41.0082, 28.9784))
    monkeypatch.setattr(main, 'weather_revision', 0)
    monkeypatch.setattr(main, 'manager', main.ConnectionManager())

def test_no_browser_initial_refresh_then_provider_retry(monkeypatch):
    async def scenario():
        # A connected device is sufficient; no browser exists.
        main.manager.active_connections.add(object())
        calls=[]
        async def weather(*args):
            calls.append(args)
            if len(calls)==1: raise main.HTTPException(502, 'provider_unavailable')
            return {'type':'weather','temperature':22,'humidity':45,'wind':5,'stale':False,'updatedAt':100}
        monkeypatch.setattr(main,'weather',weather)
        received=asyncio.Queue()
        async def broadcast(message, sender): await received.put(json.loads(message))
        monkeypatch.setattr(main.manager,'broadcast',broadcast)
        task=asyncio.create_task(main.refresh_weather_loop())
        try:
            error=await asyncio.wait_for(received.get(),1)
            assert error['type']=='weather_error' and error['retryAfter']==60
            main.weather_refresh.set() # reconnect wakes retry without waiting 15m
            data=await asyncio.wait_for(received.get(),1)
            assert data['enabled'] and data['temperature']==22
            assert len(calls)==2
        finally:
            task.cancel(); await asyncio.gather(task,return_exceptions=True)
    asyncio.run(scenario())

def test_disabled_during_request_never_reenabled(monkeypatch):
    async def scenario():
        main.manager.active_connections.add(object())
        started=asyncio.Event(); release=asyncio.Event(); messages=[]
        async def weather(*args):
            started.set(); await release.wait()
            return {'type':'weather','stale':False}
        async def broadcast(message,sender): messages.append(message)
        monkeypatch.setattr(main,'weather',weather)
        monkeypatch.setattr(main.manager,'broadcast',broadcast)
        task=asyncio.create_task(main.refresh_weather_loop())
        try:
            await asyncio.wait_for(started.wait(),1)
            main.configure_weather({'enabled':False})
            release.set(); await asyncio.sleep(.02)
            assert messages==[]
        finally:
            task.cancel(); await asyncio.gather(task,return_exceptions=True)
    asyncio.run(scenario())

def test_invalid_coordinates_and_provider_schema(monkeypatch):
    class Client:
        def __init__(self,**kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def get(self,url,params): return httpx.Response(200,json={'current':{}},request=httpx.Request('GET',url))
    monkeypatch.setattr(main.httpx,'AsyncClient',Client)
    with pytest.raises(main.HTTPException) as error: asyncio.run(main.weather())
    assert error.value.status_code==502
    with pytest.raises(main.HTTPException) as error: asyncio.run(main.weather(float('nan'),0))
    assert error.value.status_code==400

def test_stale_replay_preserves_measurement_time(monkeypatch):
    monkeypatch.setattr(main.time,'time',lambda:2000)
    cached={'type':'weather','enabled':True,'temperature':21,'updatedAt':1000}
    snapshot=main.weather_snapshot(cached)
    assert snapshot['ageSeconds']==1000 and snapshot['stale']
    assert snapshot['updatedAt']==1000 and 'stale' not in cached

def test_concurrent_weather_requests_share_provider_client(monkeypatch):
    calls=[]
    class Client:
        def __init__(self,**kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def get(self,url,params):
            calls.append(params); await asyncio.sleep(.01)
            return httpx.Response(200,json={'current':{'temperature_2m':22,'relative_humidity_2m':45,'weather_code':0,'wind_speed_10m':5}},request=httpx.Request('GET',url))
    monkeypatch.setattr(main.httpx,'AsyncClient',Client)
    async def scenario():
        data=await asyncio.gather(main.weather(),main.weather(),main.weather())
        assert all(d['temperature']==22 for d in data)
    asyncio.run(scenario())
    assert len(calls)==1

def test_websocket_weather_fetch_does_not_block_application_pong(monkeypatch):
    async def nothing(): pass
    monkeypatch.setattr(main.player, 'start', nothing)
    monkeypatch.setattr(main.player, 'stop', nothing)
    async def slow_weather(*args):
        await asyncio.sleep(.1)
        return {'type':'weather','temperature':22,'humidity':45,'wind':5,'stale':False}
    monkeypatch.setattr(main, 'weather', slow_weather)
    with TestClient(main.app) as client:
        with client.websocket_connect('/ws?role=device') as ws:
            assert ws.receive_json()['type']=='indoor'
            ws.send_json({'type':'weather_request'})
            ws.send_json({'type':'ping'})
            assert ws.receive_json()['type']=='pong'
            data=ws.receive_json()
            assert data['type']=='weather' and data['enabled'] and data['temperature']==22
