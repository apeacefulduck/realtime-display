"""Persistent household checklists; no default headings and no browser authority."""
import asyncio
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from fastapi import HTTPException


def identifier(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(400, 'invalid_id') from None


def validate(action, data):
    data = dict(data)
    allowed = {'create_note': {'title'}, 'update_note': {'title'},
               'create_item': {'text'}, 'update_item': {'text', 'completed'},
               'reorder': {'ids'}, 'import': {'items'}}.get(action)
    if allowed is not None and (not data or set(data) - allowed):
        raise HTTPException(400, 'invalid_fields')
    for field, maximum in [('title', 240), ('text', 2000)]:
        if field in data:
            value = data[field]
            if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum or any(ord(c) < 32 for c in value):
                raise HTTPException(400, 'invalid_' + field)
            data[field] = value.strip()
    if action.startswith('create_') and ('title' if action == 'create_note' else 'text') not in data:
        raise HTTPException(400, 'missing_text')
    if action == 'update_note' and 'title' not in data:
        raise HTTPException(400, 'missing_title')
    if 'completed' in data and type(data['completed']) is not bool:
        raise HTTPException(400, 'completed_must_be_boolean')
    if action == 'reorder':
        if not isinstance(data.get('ids'), list) or len(data['ids']) > 10000:
            raise HTTPException(400, 'invalid_order')
        data['ids'] = [identifier(value) for value in data['ids']]
        if len(set(data['ids'])) != len(data['ids']):
            raise HTTPException(400, 'invalid_order')
    if action == 'import':
        if not isinstance(data.get('items'), list) or len(data['items']) > 100:
            raise HTTPException(400, 'invalid_legacy_items')
        data['items'] = [validate('create_item', {'text': value})['text'] for value in data['items']]
        data['fingerprint'] = hashlib.sha256(json.dumps(data['items'], ensure_ascii=False).encode()).hexdigest()
    return data


class NotesStore:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.url = os.getenv('HOMEFLOW_SUPABASE_URL', '').rstrip('/')
        self.key = os.getenv('HOMEFLOW_SUPABASE_SECRET_KEY', '')
        self.db = Path(os.getenv('HOMEFLOW_NOTES_DB', str(Path(__file__).parent / '.homeflow-notes.sqlite3')))

    async def call(self, action, note_id=None, data=None):
        data = validate(action, data or {})
        note_id = identifier(note_id) if note_id else None
        async with self.lock:
            if self.url:
                return await self._remote(action, note_id, data)
            if os.getenv('RENDER'):
                raise HTTPException(503, 'notes_store_not_configured')
            try:
                return await asyncio.to_thread(self._local, action, note_id, data)
            except sqlite3.Error:
                raise HTTPException(503, 'notes_store_unavailable') from None

    async def _remote(self, action, note_id, data):
        if not self.url.startswith('https://') or not self.key:
            raise HTTPException(503, 'notes_store_not_configured')
        headers = {'apikey': self.key, 'Cache-Control': 'no-store'}
        if not self.key.startswith('sb_secret_'):
            headers['Authorization'] = 'Bearer ' + self.key
        try:
            async with httpx.AsyncClient(timeout=15, headers=headers) as client:
                response = await client.post(self.url + '/rest/v1/rpc/homeflow_notes_op',
                    json={'p_action': action, 'p_id': note_id, 'p_data': data})
                response.raise_for_status()
                result = response.json()
                if not isinstance(result, dict):
                    raise ValueError('Invalid response')
        except (httpx.HTTPError, ValueError):
            raise HTTPException(503, 'notes_store_unavailable') from None
        return self._result(result)

    @staticmethod
    def _result(result):
        if result.get('error'):
            raise HTTPException(404 if result['error'] == 'not_found' else 409, result['error'])
        return result

    def _local(self, action, note_id, data):
        self.db.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db, timeout=15) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS notes (id TEXT PRIMARY KEY, title TEXT NOT NULL,
                    position INTEGER NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS note_items (id TEXT PRIMARY KEY, note_id TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
                    text TEXT NOT NULL, completed INTEGER NOT NULL DEFAULT 0, position INTEGER NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE INDEX IF NOT EXISTS note_items_order ON note_items(note_id, position, id);
                CREATE TABLE IF NOT EXISTS notes_imports (fingerprint TEXT PRIMARY KEY, note_id TEXT);
            ''')
            db.execute('BEGIN IMMEDIATE')
            row = lambda table, value: db.execute('SELECT * FROM ' + table + ' WHERE id=?', (value,)).fetchone()
            next_pos = lambda table, parent=None: db.execute('SELECT COALESCE(MAX(position),-1)+1 FROM ' + table + (' WHERE note_id=?' if parent else ''), (parent,) if parent else ()).fetchone()[0]
            if action == 'list':
                records = db.execute('SELECT n.*, count(i.id) item_count, coalesce(sum(i.completed),0) completed_count FROM notes n LEFT JOIN note_items i ON i.note_id=n.id GROUP BY n.id ORDER BY n.position,n.id').fetchall()
                offset, limit = data.get('offset', 0), data.get('limit', 100)
                offset = min(offset, max(0, len(records)-1)) // limit * limit
                return {'notes': [dict(r) for r in records[offset:offset+limit]], 'total': len(records), 'offset': offset, 'limit': limit}
            if action == 'import':
                previous = db.execute('SELECT note_id FROM notes_imports WHERE fingerprint=?', (data['fingerprint'],)).fetchone()
                if previous or not data['items']:
                    return {'note_id': previous[0] if previous else None, 'imported': False}
                note_id = str(uuid4())
                db.execute('INSERT INTO notes(id,title,position) VALUES(?,?,?)', (note_id, 'Alınacaklar', next_pos('notes')))
                for index, text in enumerate(data['items']):
                    db.execute('INSERT INTO note_items(id,note_id,text,position) VALUES(?,?,?,?)', (str(uuid4()), note_id, text, index))
                db.execute('INSERT INTO notes_imports VALUES(?,?)', (data['fingerprint'], note_id))
                return {'note_id': note_id, 'imported': True}
            table = 'note_items' if action in {'update_item', 'delete_item'} else 'notes'
            existing = row(table, note_id) if note_id else None
            if action != 'create_note' and not existing:
                raise HTTPException(404, 'not_found')
            if action == 'items':
                records = db.execute('SELECT * FROM note_items WHERE note_id=? ORDER BY position,id', (note_id,)).fetchall()
                offset, limit = data.get('offset', 0), data.get('limit', 100)
                offset = min(offset, max(0, len(records)-1)) // limit * limit
                return {'note': dict(existing), 'items': [{**dict(r), 'completed': bool(r['completed'])} for r in records[offset:offset+limit]], 'total': len(records), 'offset': offset, 'limit': limit}
            if action in {'create_note', 'create_item'}:
                table = 'notes' if action == 'create_note' else 'note_items'
                value_id = str(uuid4())
                if table == 'notes':
                    db.execute('INSERT INTO notes(id,title,position) VALUES(?,?,?)', (value_id, data['title'], next_pos(table)))
                else:
                    db.execute('INSERT INTO note_items(id,note_id,text,position) VALUES(?,?,?,?)', (value_id, note_id, data['text'], next_pos(table, note_id)))
                result = dict(row(table, value_id))
            elif action in {'update_note', 'update_item'}:
                assignments = ','.join(field + '=?' for field in data)
                db.execute('UPDATE ' + table + ' SET ' + assignments + ',updated_at=CURRENT_TIMESTAMP WHERE id=?', (*data.values(), note_id))
                result = dict(row(table, note_id))
            elif action in {'delete_note', 'delete_item'}:
                db.execute('DELETE FROM ' + table + ' WHERE id=?', (note_id,))
                result = {'id': note_id, 'note_id': existing['note_id'] if table == 'note_items' else note_id, 'deleted': True}
            elif action == 'reorder':
                current = {r[0] for r in db.execute('SELECT id FROM note_items WHERE note_id=?', (note_id,))}
                if current != set(data['ids']):
                    raise HTTPException(409, 'order_conflict')
                for position, item_id in enumerate(data['ids']):
                    db.execute('UPDATE note_items SET position=?,updated_at=CURRENT_TIMESTAMP WHERE id=?', (position, item_id))
                result = {'note_id': note_id, 'reordered': True}
            else:
                raise HTTPException(400, 'invalid_action')
            if 'completed' in result:
                result['completed'] = bool(result['completed'])
            parent = result.get('note_id')
            if parent:
                db.execute('UPDATE notes SET updated_at=CURRENT_TIMESTAMP WHERE id=?', (parent,))
            return result
