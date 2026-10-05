"""Behavioral restart, rotation, failure and concurrency tests; Spotify is mocked."""
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'api'))
import main
import spotify_auth as auth_module
from spotify_auth import SpotifyAuthManager
from spotify_player import SpotifyPlayer, PLAYER
from spotify_session import make_session, seal, unseal, cipher
from spotify_token_store import SpotifyTokenStore


class Spotify:
    def __init__(self):
        self.refreshes = []
        self.requests = []
        self.refresh_payload = {'access_token': 'fresh', 'expires_in': 3600}
        self.refresh_status = 200
        self.api_statuses = [204]
        self.offline = False
        self.fail_api = False
        self.max_refreshes_in_flight = 0
        self.in_flight = 0

    def client(self, **kwargs):
        owner = self
        class Client:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def post(self, url, **kwargs):
                owner.refreshes.append(kwargs['data']['refresh_token'])
                owner.in_flight += 1
                owner.max_refreshes_in_flight = max(owner.max_refreshes_in_flight, owner.in_flight)
                await asyncio.sleep(.01)
                owner.in_flight -= 1
                if owner.offline: raise httpx.ConnectError('offline')
                return httpx.Response(owner.refresh_status, json=owner.refresh_payload, headers={'Retry-After': '30'})
            async def request(self, method, url, **kwargs):
                owner.requests.append((method, url, kwargs['headers']['Authorization']))
                if owner.fail_api: raise httpx.ReadTimeout('timeout')
                status = owner.api_statuses.pop(0) if len(owner.api_statuses) > 1 else owner.api_statuses[0]
                return httpx.Response(status)
        return Client()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('SPOTIFY_SESSION_FILE', str(tmp_path / '.spotify-session'))
    monkeypatch.setenv('SPOTIFY_CLIENT_SECRET', 'test-only-secret')
    spotify = Spotify()
    monkeypatch.setattr(auth_module.httpx, 'AsyncClient', spotify.client)
    auth = SpotifyAuthManager(lambda: ('test-client', 'test-only-secret'))
    old = make_session({'access_token': 'initial', 'refresh_token': 'durable-refresh'})
    asyncio.run(auth.register(old))
    return auth, spotify, old


async def publish(state): pass


def new_player(auth, has_clients=lambda: False):
    return SpotifyPlayer(auth.request, publish, has_clients, unseal, auth)


def test_restore_on_start_without_browser_or_esp32(setup):
    auth, spotify, _ = setup
    handle = auth.client_session()
    async def run():
        for _ in range(4):
            fresh = SpotifyAuthManager(auth.credentials)
            player = new_player(fresh)
            await player.start()
            assert player.state['connected'] and not player.state['error']
            assert await fresh.authorize(handle) == fresh.session
            await player.stop()
    asyncio.run(run())
    assert spotify.refreshes == ['durable-refresh'] * 4
    assert len(spotify.requests) == 4


def test_no_stored_session_requires_authorization_without_opening_login(setup, caplog):
    auth, spotify, _ = setup
    auth.store.path.unlink()
    async def run():
        fresh = SpotifyAuthManager(auth.credentials)
        player = new_player(fresh)
        with caplog.at_level('INFO'):
            await player.start()
        assert player.state['error'] == 'authorization_required'
        assert not fresh.client_session()
        await player.stop()
    asyncio.run(run())
    assert 'No stored session. Authorization required.' in caplog.text
    assert not spotify.refreshes and not spotify.requests


@pytest.mark.parametrize('replacement', ['missing', None, '', 'rotated-refresh'])
def test_expiry_refresh_preserves_or_rotates_refresh_token(setup, replacement):
    auth, spotify, old = setup
    asyncio.run(auth._commit({**auth.tokens, 'expires_at': 0}))
    if replacement != 'missing': spotify.refresh_payload['refresh_token'] = replacement
    response, session = asyncio.run(auth.request(PLAYER, old))
    expected = 'rotated-refresh' if replacement == 'rotated-refresh' else 'durable-refresh'
    assert response.status_code == 204
    assert unseal(session, 'session')['refresh_token'] == expected
    assert unseal(auth.store.load(), 'session')['refresh_token'] == expected


def test_rotation_saved_before_failed_playback_request_and_stale_browser(setup):
    auth, spotify, old = setup
    handle = auth.client_session()
    asyncio.run(auth._commit({**auth.tokens, 'expires_at': 0}))
    spotify.refresh_payload['refresh_token'] = 'rotated-refresh'
    spotify.fail_api = True
    with pytest.raises(HTTPException) as error: asyncio.run(auth.request(PLAYER, old))
    assert error.value.status_code == 502
    fresh = SpotifyAuthManager(auth.credentials)
    assert asyncio.run(fresh.load())
    assert fresh.tokens['refresh_token'] == 'rotated-refresh'
    assert asyncio.run(fresh.authorize(old)) == fresh.session
    assert asyncio.run(fresh.authorize(handle)) == fresh.session
    assert unseal(fresh.store.load(), 'session')['refresh_token'] == 'rotated-refresh'
    assert 'access_token' not in unseal(handle, 'session')
    assert 'refresh_token' not in unseal(handle, 'session')


def test_parallel_expired_requests_make_one_refresh(setup):
    auth, spotify, old = setup
    asyncio.run(auth._commit({**auth.tokens, 'expires_at': 0}))
    async def run():
        return await asyncio.gather(*(auth.request(PLAYER, old) for _ in range(12)))
    assert len(asyncio.run(run())) == 12
    assert spotify.refreshes == ['durable-refresh']
    assert spotify.max_refreshes_in_flight == 1


def test_api_401_refreshes_once_and_retries_once(setup):
    auth, spotify, old = setup
    spotify.api_statuses = [401, 204]
    response, _ = asyncio.run(auth.request(PLAYER + '/pause', old, 'PUT'))
    assert response.status_code == 204
    assert len(spotify.refreshes) == 1 and len(spotify.requests) == 2
    assert all(call[:2] == ('PUT', PLAYER + '/pause') for call in spotify.requests)


def test_repeated_api_401_has_no_infinite_retry_or_deleted_authorization(setup):
    auth, spotify, old = setup
    spotify.api_statuses = [401]
    with pytest.raises(HTTPException) as error: asyncio.run(auth.request(PLAYER, old))
    assert error.value.status_code == 502
    assert len(spotify.refreshes) == 1 and len(spotify.requests) == 2
    assert auth.session and not auth.invalid


def test_revoked_authorization_does_not_crash_or_revive_from_browser(setup):
    auth, spotify, old = setup
    spotify.refresh_status = 400
    spotify.refresh_payload = {'error': 'invalid_grant'}
    async def run():
        for _ in range(2):
            fresh = SpotifyAuthManager(auth.credentials)
            player = new_player(fresh)
            await player.start()
            assert not player.session and player.state['error'] == 'authorization_required'
            with pytest.raises(HTTPException) as error: await fresh.authorize(old)
            assert error.value.status_code == 401
            await player.stop()
    asyncio.run(run())
    assert len(spotify.refreshes) == 1


@pytest.mark.parametrize('failure', ['offline', '503', 'invalid_client', '429', 'malformed'])
def test_transient_restore_failure_preserves_authorization(setup, failure):
    auth, spotify, old = setup
    if failure == 'offline': spotify.offline = True
    elif failure == '503': spotify.refresh_status = 503
    elif failure == 'invalid_client':
        spotify.refresh_status = 401
        spotify.refresh_payload = {'error': 'invalid_client'}
    elif failure == '429': spotify.refresh_status = 429
    elif failure == 'malformed': spotify.refresh_payload = []
    before = auth.store.load()
    fresh = SpotifyAuthManager(auth.credentials)
    assert asyncio.run(fresh.load())
    assert asyncio.run(fresh.restore()) is False
    assert not fresh.invalid and asyncio.run(fresh.authorize(old))
    assert fresh.store.load() == before
    spotify.offline, spotify.refresh_status = False, 200
    spotify.refresh_payload = {'access_token': 'recovered'}
    fresh.retry_at = 0
    assert asyncio.run(fresh.restore()) is True


def test_401_then_refresh_503_is_not_authorization_required(setup):
    auth, spotify, old = setup
    spotify.api_statuses = [401]
    spotify.refresh_status = 503
    with pytest.raises(HTTPException) as error: asyncio.run(auth.request(PLAYER, old))
    assert error.value.status_code == 502 and not auth.invalid
    assert unseal(auth.store.load(), 'session')['refresh_token'] == 'durable-refresh'


def test_disabled_setting_survives_restart(setup):
    auth, spotify, _ = setup
    asyncio.run(auth.set_enabled(False))
    async def run():
        fresh = SpotifyAuthManager(auth.credentials)
        player = new_player(fresh)
        await player.start()
        assert not player.enabled and not player.state['enabled']
        assert fresh.session
        await player.stop()
    asyncio.run(run())
    assert len(spotify.refreshes) == 1 and not spotify.requests


def test_os_data_path_is_independent_of_working_directory(tmp_path, monkeypatch):
    monkeypatch.delenv('SPOTIFY_SESSION_FILE', raising=False)
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'userdata'))
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'userdata'))
    first = SpotifyTokenStore().path
    monkeypatch.chdir(tmp_path)
    assert SpotifyTokenStore().path == first
    assert first.is_absolute()


def test_migrates_old_encrypted_disk_format(setup):
    auth, spotify, old = setup
    auth.store.save(old)
    fresh = SpotifyAuthManager(auth.credentials)
    assert asyncio.run(fresh.load()) and asyncio.run(fresh.restore())
    assert asyncio.run(fresh.authorize(old)) and fresh.tokens['session_id']
    assert unseal(fresh.store.load(), 'session')['refresh_token'] == 'durable-refresh'


def test_session_age_does_not_force_login_but_oauth_state_expires(setup):
    payload = {'purpose': 'session', 'access_token': 'old', 'refresh_token': 'durable-refresh', 'expires_at': 0}
    old = cipher().encrypt_at_time(json.dumps(payload).encode(), int(time.time()) - 60 * 86400).decode()
    assert unseal(old, 'session')['refresh_token']
    state = cipher().encrypt_at_time(json.dumps({'purpose': 'oauth-state'}).encode(), int(time.time()) - 601).decode()
    with pytest.raises(HTTPException): unseal(state, 'oauth-state', ttl=600)


def test_corrupt_store_startup_safe_and_reconnection_possible(setup):
    auth, spotify, old = setup
    auth.store.path.write_text('invalid ciphertext', encoding='utf-8')
    async def run():
        fresh = SpotifyAuthManager(auth.credentials)
        player = new_player(fresh)
        await player.start()
        assert player.state['error'] == 'authorization_required'
        await player.stop()
        await fresh.register(old)
        assert fresh.session and fresh.persisted and not fresh.invalid
    asyncio.run(run())
    assert not spotify.refreshes


def test_unwritable_store_is_visible_and_not_reported_as_durable(setup, monkeypatch):
    auth, spotify, old = setup
    def fail(*args): raise PermissionError('test-only')
    monkeypatch.setattr(auth.store, 'save', fail)
    with pytest.raises(HTTPException) as error: asyncio.run(auth.register(old))
    assert error.value.status_code == 503 and not auth.persisted
    assert auth.tokens['refresh_token'] == 'durable-refresh'


def test_rotated_token_not_rolled_back_on_disk_failure(setup, monkeypatch):
    auth, spotify, old = setup
    asyncio.run(auth._commit({**auth.tokens, 'expires_at': 0}))
    spotify.refresh_payload['refresh_token'] = 'rotated-refresh'
    def fail(*args): raise PermissionError('test-only')
    monkeypatch.setattr(auth.store, 'save', fail)
    asyncio.run(auth.request(PLAYER, old))
    assert auth.tokens['refresh_token'] == 'rotated-refresh' and not auth.persisted


def test_logs_do_not_contain_secrets(setup, caplog):
    auth, spotify, _ = setup
    fresh = SpotifyAuthManager(auth.credentials)
    with caplog.at_level('INFO'):
        assert asyncio.run(fresh.load()) and asyncio.run(fresh.restore())
    for expected in ['Stored session found', 'Restoring session...', 'Access token refreshed', 'Session restored successfully']:
        assert expected in caplog.text
    for secret in ['durable-refresh', 'test-only-secret', '"fresh"']:
        assert secret not in caplog.text


def test_fresh_process_restores_same_authorization_from_another_cwd(setup, tmp_path):
    auth, _, _ = setup
    api = str(Path(__file__).resolve().parents[1] / 'api')
    code = '''
import asyncio, os, sys
sys.path.insert(0, sys.argv[1])
import httpx, spotify_auth
from spotify_auth import SpotifyAuthManager
class Client:
    def __init__(self, **kwargs): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def post(self, *args, **kwargs):
        assert kwargs['data']['refresh_token'] == 'durable-refresh'
        return httpx.Response(200, json={'access_token':'process-fresh'})
spotify_auth.httpx.AsyncClient = Client
auth = SpotifyAuthManager(lambda: ('test', 'test-only-secret'))
assert asyncio.run(auth.load()) and asyncio.run(auth.restore())
assert auth.tokens['access_token'] == 'process-fresh'
print('fresh process restored without OAuth')
'''
    for _ in range(3):
        result = subprocess.run([sys.executable, '-c', code, api], cwd=tmp_path,
                                env=os.environ.copy(), text=True, capture_output=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert 'without OAuth' in result.stdout
