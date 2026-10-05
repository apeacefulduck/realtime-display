"""External persistence behavior without real secrets or network calls."""
import asyncio
import json
import sys
import threading
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'api'))
import spotify_token_store as stores
import spotify_player
from spotify_auth import SpotifyAuthManager
from spotify_player import SpotifyPlayer
from spotify_session import make_session, unseal


@pytest.fixture
def remote(monkeypatch):
    monkeypatch.setenv('HOMEFLOW_SESSION_STORE', 'supabase')
    monkeypatch.setenv('HOMEFLOW_SUPABASE_URL', 'https://test.supabase.co')
    monkeypatch.setenv('HOMEFLOW_SUPABASE_SECRET_KEY', 'sb_secret_test-only')
    monkeypatch.setenv('SPOTIFY_CLIENT_SECRET', 'test-only-secret')
    state = {'rows': {}, 'calls': [], 'status': 200}
    def handle(request):
        state['calls'].append(request)
        assert request.url.path == '/rest/v1/homeflow_sessions'
        assert request.headers['apikey'] == 'sb_secret_test-only'
        assert 'Authorization' not in request.headers
        if state['status'] != 200:
            return httpx.Response(state['status'], json={'message': 'test-only'})
        if request.method == 'POST':
            record = json.loads(request.content)
            assert request.url.params['on_conflict'] == 'slot'
            assert 'merge-duplicates' in request.headers['Prefer']
            state['rows'][record['slot']] = record['ciphertext']
            return httpx.Response(201)
        assert request.url.params['slot'] == 'eq.spotify'
        value = state['rows'].get('spotify')
        return httpx.Response(200, json=[{'ciphertext': value}] if value else [])
    client = httpx.Client
    monkeypatch.setattr(stores.httpx, 'Client', lambda **kwargs: client(transport=httpx.MockTransport(handle), **kwargs))
    return state


def test_external_store_upserts_ciphertext_and_survives_new_service(remote):
    async def run():
        first = SpotifyAuthManager(lambda: ('test-client', 'test-only-secret'))
        await first.register(make_session({'access_token': 'initial', 'refresh_token': 'durable-refresh'}))
        handle = first.client_session()
        await first._commit({**first.tokens, 'refresh_token': 'rotated-refresh'})
        assert len(remote['rows']) == 1
        ciphertext = remote['rows']['spotify']
        assert 'durable-refresh' not in ciphertext and 'rotated-refresh' not in ciphertext
        fresh = SpotifyAuthManager(first.credentials)
        assert await fresh.load()
        assert fresh.tokens['refresh_token'] == 'rotated-refresh'
        assert await fresh.authorize(handle)
        assert unseal(handle, 'session').keys() == {'purpose', 'session_id'}
    asyncio.run(run())


@pytest.mark.parametrize('status', [401, 403, 429, 500, 503])
def test_external_store_error_is_not_an_empty_session(remote, status):
    remote['status'] = status
    store = stores.session_store()
    with pytest.raises(OSError): store.load()
    with pytest.raises(OSError): store.save('encrypted-test')


def test_missing_remote_config_never_falls_back_to_ephemeral_file(remote, monkeypatch):
    monkeypatch.delenv('HOMEFLOW_SUPABASE_SECRET_KEY')
    store = stores.session_store()
    assert isinstance(store, stores.SupabaseTokenStore)
    with pytest.raises(OSError): store.load()
    with pytest.raises(OSError): store.save('encrypted-test')


def test_store_outage_then_recovery_restores_without_oauth(remote, monkeypatch):
    async def publish(state): pass
    async def run():
        first = SpotifyAuthManager(lambda: ('test-client', 'test-only-secret'))
        original = make_session({'access_token': 'initial', 'refresh_token': 'durable-refresh'})
        await first.register(original)
        stored = remote['rows']['spotify']
        remote['status'] = 503
        fresh = SpotifyAuthManager(first.credentials)
        player = SpotifyPlayer(fresh.request, publish, lambda: False, unseal, fresh)
        await player.start()
        assert player.state['error'] == 'session_store_unavailable'
        with pytest.raises(HTTPException) as error: await fresh.authorize(original)
        assert error.value.status_code == 503
        assert remote['rows']['spotify'] == stored
        class Client:
            def __init__(self, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def post(self, url, **kwargs):
                return httpx.Response(200, json={'access_token': 'restored'})
            async def request(self, *args, **kwargs): return httpx.Response(204)
        monkeypatch.setattr(stores.httpx, 'AsyncClient', Client)
        monkeypatch.setattr(spotify_player, 'POLL_SECONDS', .02)
        remote['status'] = 200
        player.has_clients = lambda: True
        await asyncio.sleep(.09)
        await player.stop()
        assert player.state['connected'] and not player.state['error']
        assert fresh.tokens['refresh_token'] == 'durable-refresh'
        assert fresh.tokens['access_token'] == 'restored'
    asyncio.run(run())


def test_storage_io_leaves_event_loop_available(remote):
    async def run():
        store = stores.session_store()
        save = store.save
        started, release = threading.Event(), threading.Event()
        def blocked_save(ciphertext):
            started.set()
            assert release.wait(timeout=3)
            save(ciphertext)
        store.save = blocked_save
        auth = SpotifyAuthManager(lambda: ('test-client', 'test-only-secret'), store)
        register = asyncio.create_task(auth.register(make_session({'access_token': 'initial', 'refresh_token': 'durable-refresh'})))
        while not started.is_set(): await asyncio.sleep(.01)
        # This runs while storage is waiting in a thread; websocket/ping tasks
        # can be scheduled normally instead of waiting for the network timeout.
        assert not register.done()
        release.set()
        await register
        assert auth.persisted
    asyncio.run(run())
