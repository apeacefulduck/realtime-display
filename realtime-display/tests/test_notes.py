import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'api'))
import notes_api as notes
from notes_store import NotesStore


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('HOMEFLOW_NOTES_DB', str(tmp_path / 'notes.db'))
    monkeypatch.delenv('HOMEFLOW_SUPABASE_URL', raising=False)
    monkeypatch.delenv('RENDER', raising=False)
    monkeypatch.setattr(notes, 'store', NotesStore())
    monkeypatch.setattr(notes, 'views', {})
    monkeypatch.setattr(notes, 'view_lock', asyncio.Lock())
    monkeypatch.setattr(notes, 'publish_change', None)
    app = FastAPI(); app.include_router(notes.router)
    return TestClient(app)


def create(client, title='Kendi başlığım'):
    response = client.post('/notes', json={'title': title}); assert response.status_code == 201
    return response.json()['id']


def item(client, note_id, text='Görev'):
    response = client.post(f'/notes/{note_id}/items', json={'text': text}); assert response.status_code == 201
    return response.json()['id']


def test_crud_counts_and_cascade(client):
    assert client.get('/notes').json()['notes'] == []
    n = create(client); n2 = create(client, 'Başka bir başlık')
    i = item(client, n); item(client, n2, 'Başka görev')
    assert client.patch(f'/notes/{n}', json={'title': 'Tamamen kullanıcı adı'}).json()['title'] == 'Tamamen kullanıcı adı'
    updated = client.patch(f'/note-items/{i}', json={'text': 'Yeni metin çğıöşü', 'completed': True}).json()
    assert updated['completed'] is True and updated['note_id'] == n
    assert client.get('/notes').json()['notes'][0]['completed_count'] == 1
    assert client.get(f'/notes/{n2}/items').json()['items'][0]['text'] == 'Başka görev'
    assert client.delete(f'/notes/{n}').status_code == 200
    assert client.patch(f'/note-items/{i}', json={'completed': False}).status_code == 404
    assert client.get(f'/notes/{n2}/items').json()['total'] == 1
    i2 = client.get(f'/notes/{n2}/items').json()['items'][0]['id']
    assert client.delete(f'/note-items/{i2}').status_code == 200
    assert client.get(f'/notes/{n2}/items').json()['items'] == []


@pytest.mark.parametrize('payload', [{'title': ''}, {'title': ' '}, {'title': None}, {'title': 12}, {'title': 'a'*241}, {'title': 'a\nb'}, {'title': 'ok', 'position': 1}, {}])
def test_invalid_title(client, payload):
    assert client.post('/notes', json=payload).status_code == 400


@pytest.mark.parametrize('payload', [{'completed': 'false'}, {'completed': 1}, {'completed': None}, {'text': ''}, {'text': 'a'*2001}, {'note_id': 'x'}, {}])
def test_invalid_items(client, payload):
    i = item(client, create(client))
    assert client.patch(f'/note-items/{i}', json=payload).status_code == 400


def test_validation_pagination_and_large_lists(client):
    n = create(client, 'Uzun '*48); ids = [item(client, n, f'Madde {i}') for i in range(40)]
    page = client.get(f'/notes/{n}/items?offset=16&limit=16').json()
    assert page['total'] == 40 and page['offset'] == 16 and len(page['items']) == 16
    assert page['items'][0]['id'] == ids[16]
    assert client.get(f'/notes/{n}/items?offset=1000&limit=16').json()['offset'] == 32
    assert client.get('/notes?limit=0').status_code == 422
    assert client.get('/notes?offset=-1').status_code == 422
    assert client.patch('/notes/not-a-uuid', json={'title': 'X'}).status_code == 400
    assert client.post('/notes', content='[]', headers={'Content-Type': 'application/json'}).status_code == 400
    assert client.post('/notes', content='{').status_code == 400


def test_order_atomic_conflict(client):
    n = create(client); ids = [item(client, n, str(i)) for i in range(4)]
    assert client.put(f'/notes/{n}/items/order', json={'ids': ids[::-1]}).status_code == 200
    assert [r['id'] for r in client.get(f'/notes/{n}/items').json()['items']] == ids[::-1]
    assert client.put(f'/notes/{n}/items/order', json={'ids': ids[1:]}).status_code == 409
    assert client.put(f'/notes/{n}/items/order', json={'ids': [ids[0]]*4}).status_code == 400
    foreign = item(client, create(client, 'Farklı'))
    assert client.put(f'/notes/{n}/items/order', json={'ids': [foreign]+ids[1:]}).status_code == 409
    assert [r['id'] for r in client.get(f'/notes/{n}/items').json()['items']] == ids[::-1]


def test_import_persists_once_and_never_creates_empty_default(client):
    assert client.post('/notes/import-shopping', json={'items': []}).json()['imported'] is False
    assert client.get('/notes').json()['total'] == 0
    result = client.post('/notes/import-shopping', json={'items': ['Süt', 'Ekmek']}).json()
    assert result['imported'] is True
    assert client.post('/notes/import-shopping', json={'items': ['Süt', 'Ekmek']}).json()['imported'] is False
    # New store instance / API restart: dedupe marker and content survive.
    notes.store = NotesStore()
    assert client.get('/notes').json()['total'] == 1
    assert client.get(f"/notes/{result['note_id']}/items").json()['total'] == 2
    client.delete('/notes/' + result['note_id'])
    assert client.post('/notes/import-shopping', json={'items': ['Süt', 'Ekmek']}).json()['imported'] is False
    assert client.get('/notes').json()['total'] == 0


class Socket:
    def __init__(self, paired=True): self.messages = []; self.paired = paired
    async def send_json(self, payload): self.messages.append(payload)


def test_websocket_subscription_device_write_web_read_and_deletion(client, monkeypatch):
    n = create(client); i = item(client, n, 'x'*2000)
    sock = Socket(); monkeypatch.setattr(notes, 'device_authorized', lambda s: s.paired)
    async def scenario():
        await notes.handle_socket(sock, {'type': 'notes_list', 'request_id': 1})
        assert len(sock.messages[-1]['notes']) == 1 and 'items' not in sock.messages[-1]
        await notes.handle_socket(sock, {'type': 'notes_open', 'note_id': n, 'request_id': 2})
        assert len(sock.messages[-1]['items'][0]['text']) == 160
        await notes.handle_socket(sock, {'type': 'notes_set', 'item_id': i, 'completed': True, 'request_id': 3})
        assert sock.messages[-1]['ok'] is True
        assert sock.messages[-2]['items'][0]['completed'] is True
        await notes.mutate('delete_note', n)
        assert sock.messages[-1]['type'] == 'notes' and sock.messages[-1]['reset_view']
        assert notes.views[sock] == (None, 0)
    asyncio.run(scenario())
    assert client.get('/notes').json()['total'] == 0


def test_web_change_pushes_and_unpaired_or_invalid_write_is_rejected(client, monkeypatch):
    n = create(client); i = item(client, n); sock = Socket(False)
    monkeypatch.setattr(notes, 'device_authorized', lambda s: s.paired)
    async def scenario():
        await notes.handle_socket(sock, {'type': 'notes_open', 'note_id': n})
        await notes.mutate('update_item', i, {'completed': True})
        assert sock.messages[-1]['items'][0]['completed'] is True
        await notes.handle_socket(sock, {'type': 'notes_set', 'item_id': i, 'completed': False, 'request_id': 7})
        assert sock.messages[-1]['error'] == 'device_not_paired'
        await notes.handle_socket(sock, {'type': 'notes_list', 'offset': -1})
        assert sock.messages[-1]['error'] == 'invalid_offset'
    asyncio.run(scenario())
    assert client.get(f'/notes/{n}/items').json()['items'][0]['completed'] is True


def test_remote_key_headers_errors_and_no_ephemeral_render_fallback(client, monkeypatch):
    monkeypatch.setenv('HOMEFLOW_SUPABASE_URL', 'https://example.supabase.co')
    monkeypatch.setenv('HOMEFLOW_SUPABASE_SECRET_KEY', 'sb_secret_test')
    store = NotesStore(); seen = []
    def handler(req):
        seen.append(req); return httpx.Response(200, json={'notes': [], 'total': 0, 'offset': 0, 'limit': 100})
    original = httpx.AsyncClient
    with patch('notes_store.httpx.AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handler), **kw)):
        assert asyncio.run(store.call('list'))['total'] == 0
    assert seen[0].headers['apikey'] == 'sb_secret_test' and 'authorization' not in seen[0].headers
    with patch('notes_store.httpx.AsyncClient', lambda **kw: original(transport=httpx.MockTransport(lambda r: httpx.Response(503)), **kw)):
        with pytest.raises(HTTPException) as exc: asyncio.run(store.call('list'))
        assert exc.value.status_code == 503
    monkeypatch.delenv('HOMEFLOW_SUPABASE_URL'); monkeypatch.setenv('RENDER', 'true')
    with pytest.raises(HTTPException) as exc: asyncio.run(NotesStore().call('list'))
    assert exc.value.detail == 'notes_store_not_configured'
