import asyncio
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'api'))
os.environ.update(SPOTIFY_CLIENT_ID='test-client', SPOTIFY_CLIENT_SECRET='test-only-secret',
                  SPOTIFY_REDIRECT_URI='https://testserver/spotify/callback', HOMEFLOW_DEVICE_TOKEN='test-device')
import main
import spotify_player as sp
from spotify_auth import SpotifyAuthManager
from spotify_session import seal, make_session, unseal


def song(playing=True, name='First song'):
    return {'is_playing': playing, 'progress_ms': 42000,
            'device': {'is_active': True, 'name': 'Desktop'},
            'item': {'id': name, 'name': name, 'duration_ms': 180000,
                     'artists': [{'name': 'Artist'}], 'album': {'name': 'Album', 'images': [
                         {'url': 'https://i.scdn.co/image/big', 'width': 640},
                         {'url': 'https://i.scdn.co/image/small', 'width': 64}]}}}


class FakeApi:
    def __init__(self):
        self.calls = []
        self.payload = song()
        self.responses = []

    async def __call__(self, url, session, method='GET'):
        self.calls.append((method, url))
        if self.responses:
            return self.responses.pop(0), session
        if method == 'GET': return httpx.Response(200, json=self.payload), session
        if url.endswith('/pause'): self.payload['is_playing'] = False
        if url.endswith('/play'): self.payload['is_playing'] = True
        if url.endswith('/next'): self.payload = song(name='Next song')
        if url.endswith('/previous'): self.payload = song(name='Previous song')
        return httpx.Response(204), session


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv('SPOTIFY_SESSION_FILE', str(tmp_path / 'session'))
    fake = FakeApi()
    main.manager = main.ConnectionManager()
    main.auth_manager = SpotifyAuthManager(main.spotify_token_auth)
    async def restored():
        main.auth_manager.restore_pending = False
        return True
    monkeypatch.setattr(main.auth_manager, 'restore', restored)
    main.player = sp.SpotifyPlayer(fake, main.publish_spotify, lambda: bool(main.manager.active_connections), unseal, main.auth_manager)
    token = make_session({'access_token': 'access', 'refresh_token': 'refresh', 'expires_in': 3600})
    asyncio.run(main.auth_manager.register(token))
    asyncio.run(main.player.remember(token))
    return main.player, fake, token


def test_state_artwork_pause_and_null():
    state = sp.playback_state(song(False))
    assert state['album_art_url'].endswith('/small') and not state['is_playing']
    assert state['title'] == 'First song' and state['duration_ms'] == 180000
    assert sp.playback_state({'item': None})['title'] == ''
    assert not sp.playback_state({'item': None})['has_active_device']
    for payload in [[], {'item': []}, {'device': 'invalid'}]:
        if payload == {'item': []}: continue  # empty item is a no-playback response
        with pytest.raises(ValueError): sp.playback_state(payload)


def test_control_methods_and_followup(service):
    player, fake, _ = service
    async def run():
        await player.refresh()
        for action, method, path in [('toggle','PUT','pause'), ('resume','PUT','play'),
                                      ('next','POST','next'), ('previous','POST','previous')]:
            player.last_command = 0
            result = await player.control(action)
            assert result['ok'] and result['state_refreshed']
            assert (method, sp.PLAYER + '/' + path) in fake.calls
            assert fake.calls[-1] == ('GET', sp.PLAYER)
        assert player.state['track_name'] == 'Previous song'
    asyncio.run(run())


@pytest.mark.parametrize('status,error', [(401,'authorization_required'), (403,'scope_or_premium_required'),
                                        (404,'no_active_device'), (429,'rate_limited'), (500,'service_unavailable')])
def test_errors_clear_stale_state_and_backoff(service, status, error):
    player, fake, _ = service
    async def run():
        await player.refresh()
        player.last_fetch = 0
        fake.responses = [httpx.Response(status, headers={'Retry-After':'60'})]
        with pytest.raises(HTTPException): await player.refresh()
        assert player.state['error'] == error and not player.state['title']
        if status == 429:
            count = len(fake.calls)
            player.last_fetch = 0
            with pytest.raises(HTTPException): await player.refresh()
            assert len(fake.calls) == count
    asyncio.run(run())


def test_no_active_device_and_empty_response(service):
    player, fake, _ = service
    async def run():
        await player.refresh()
        fake.responses = [httpx.Response(204)]
        player.last_fetch = 0
        await player.refresh()
        assert not player.state['track_id']
        with pytest.raises(HTTPException) as error: await player.control('next')
        assert error.value.detail == 'no_active_device'
        assert not any(method == 'POST' for method, _ in fake.calls)
    asyncio.run(run())


def test_malformed_response_is_safe(service):
    player, fake, _ = service
    fake.responses = [httpx.Response(200, text='not json')]
    async def run():
        with pytest.raises(HTTPException): await player.refresh()
        assert player.state['error'] == 'invalid_response'
    asyncio.run(run())


def test_ws_reconnect_authority_control_and_other_streams(service):
    player, fake, token = service
    with TestClient(main.app) as client:
        assert client.get('/spotify/current', headers={'Authorization': 'Bearer '+token}).status_code == 200
        with client.websocket_connect('/ws?role=device', headers={'x-homeflow-device-token':'test-device'}) as ws:
            assert ws.receive_json()['track_name'] == 'First song'
            ws.send_json({'type':'spotify_control','action':'next'})
            messages = [ws.receive_json(), ws.receive_json()]
            assert any(m.get('track_name') == 'Next song' for m in messages)
            assert any(m.get('ok') for m in messages)
            assert 'access_token' not in json.dumps(messages) and 'session_token' not in json.dumps(messages)
        with client.websocket_connect('/ws?role=device') as ws:
            assert ws.receive_json()['track_name'] == 'Next song'
            ws.send_json({'type':'spotify_control', 'action':'pause'})
            assert ws.receive_json()['error'] == 'device_not_paired'
            ws.send_json({'type':'spotify','title':'spoofed'})
            assert ws.receive_json()['title'] == 'Next song'
            ws.send_json({'type':'display','text':''})
            ws.send_json({'type':'ping'})
            assert ws.receive_json()['type'] == 'pong'
        with client.websocket_connect('/ws') as ws:
            snapshots = [ws.receive_json(), ws.receive_json()]
            assert any(m.get('type') == 'display' and m['text'] == '' for m in snapshots)


def test_browserless_poll_and_restart(service, monkeypatch):
    player, fake, token = service
    monkeypatch.setattr(sp, 'POLL_SECONDS', .02)
    async def run():
        player.has_clients = lambda: True
        player.session = ''
        await player.start()
        await asyncio.sleep(.075)
        await player.stop()
        assert unseal(player.session, 'session')['refresh_token'] == 'refresh' and len(fake.calls) >= 2
        assert 'refresh' not in player.path.read_text()
    asyncio.run(run())


def test_oauth_scopes_and_state_cookie(service):
    with TestClient(main.app, base_url='https://testserver') as client:
        response = client.get('/spotify/login', follow_redirects=False)
        query = parse_qs(urlparse(response.headers['location']).query)
        assert 'show_dialog' not in query
        assert 'user-modify-playback-state' in query['scope'][0]
        assert client.get('/spotify/callback?code=test&state=wrong').status_code == 400


def test_refresh_and_retry_on_401(service):
    _, _, token = service
    calls = []
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            calls.append('refresh')
            return httpx.Response(200, json={'access_token':'new', 'expires_in':3600})
        async def request(self, method, url, **kwargs):
            calls.append(method)
            return httpx.Response(204 if kwargs['headers']['Authorization'] == 'Bearer new' else 401)
    with patch.object(main.httpx, 'AsyncClient', Client):
        response, saved = asyncio.run(main.spotify_get(sp.PLAYER+'/pause', token, 'PUT'))
    assert response.status_code == 204 and calls == ['PUT','refresh','PUT']
    assert unseal(saved, 'session')['access_token'] == 'new'


def test_disable_and_current_requires_auth(service):
    _, _, token = service
    with TestClient(main.app) as client:
        assert client.get('/spotify/current').status_code == 401
        headers = {'Authorization':'Bearer '+token}
        response = client.post('/spotify/enabled', headers=headers, json={'enabled':False})
        assert response.status_code == 200 and not response.json()['enabled']
        assert client.post('/spotify/enabled', headers=headers, json={'enabled':'yes'}).status_code == 400
        assert client.post('/spotify/control', headers=headers, json={'action':[]}).status_code == 400
        assert client.post('/spotify/control', headers=headers, content='{').status_code == 400


def test_oauth_callback_registers_backend_session(service):
    player, _, _ = service
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kwargs):
            return httpx.Response(200, json={'access_token':'new-access', 'refresh_token':'new-refresh', 'expires_in':3600})
    with TestClient(main.app, base_url='https://testserver') as client:
        response = client.get('/spotify/login', follow_redirects=False)
        state = parse_qs(urlparse(response.headers['location']).query)['state'][0]
        with patch.object(main.httpx, 'AsyncClient', Client):
            callback = client.get('/spotify/callback', params={'code':'test-code', 'state':state}, follow_redirects=False)
        assert callback.status_code == 307
        browser_session = parse_qs(urlparse(callback.headers['location']).fragment)['spotify_session'][0]
        browser_payload = unseal(browser_session, 'session')
        assert browser_payload['session_id']
        assert 'access_token' not in browser_payload and 'refresh_token' not in browser_payload
        assert unseal(player.session, 'session')['refresh_token'] == 'new-refresh'
        assert player.path.read_text() == player.session


def test_expired_token_and_failed_refresh(service):
    asyncio.run(main.auth_manager._commit({**main.auth_manager.tokens, 'expires_at': 0}))
    token = seal({'purpose':'session','access_token':'expired','refresh_token':'refresh','expires_at':0})
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kwargs): return httpx.Response(400, json={'error':'invalid_grant'})
    with patch.object(main.httpx, 'AsyncClient', Client):
        with pytest.raises(HTTPException) as error:
            asyncio.run(main.spotify_get(sp.PLAYER, token))
        assert error.value.status_code == 401


def test_network_failure(service):
    player, _, _ = service
    async def failing(*args): raise httpx.ConnectError('offline')
    player.request = failing
    async def run():
        with pytest.raises(HTTPException) as error: await player.refresh()
        assert error.value.status_code == 502
        assert player.state['error'] == 'service_unavailable'
    asyncio.run(run())


def test_browser_handle_survives_rotation_and_cannot_replace_household(service):
    player, fake, token = service
    with TestClient(main.app) as client:
        response = client.get('/spotify/current', headers={'Authorization': 'Bearer ' + token})
        handle = response.json()['session_token']
        asyncio.run(main.auth_manager._commit({**main.auth_manager.tokens, 'refresh_token': 'rotated'}))
        asyncio.run(player.remember(main.auth_manager.session))
        response = client.post('/spotify/control', headers={'Authorization': 'Bearer ' + handle}, json={'action': 'pause'})
        assert response.status_code == 200
        other = make_session({'access_token':'another', 'refresh_token':'other-household'})
        assert client.get('/spotify/current', headers={'Authorization':'Bearer ' + other}).status_code == 403
        assert main.auth_manager.tokens['refresh_token'] == 'rotated'
