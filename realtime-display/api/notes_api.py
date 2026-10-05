"""REST CRUD and compact WebSocket views share the same durable store."""
import asyncio

from fastapi import APIRouter, HTTPException, Query, Request
from notes_store import NotesStore, identifier

router = APIRouter()
store = NotesStore()
views = {}  # websocket -> (note UUID or None, storage page offset)
publish_change = None
device_authorized = None
view_lock = asyncio.Lock()


async def body(request):
    try:
        data = await request.json()
    except (ValueError, UnicodeError):
        raise HTTPException(400, 'invalid_json') from None
    if not isinstance(data, dict):
        raise HTTPException(400, 'invalid_json_object')
    return data


async def mutate(action, value_id=None, data=None):
    result = await store.call(action, value_id, data)
    note_id = result.get('note_id') or (result.get('id') if action.endswith('note') else value_id)
    if action == 'import' and not result.get('imported'):
        return result
    # Serialize subscription snapshots with navigation so an old push cannot
    # replace the new view after the user backs out or opens another heading.
    async with view_lock:
        for socket, view in tuple(views.items()):
            try:
                await send_view(socket, *view)
            except Exception:
                views.pop(socket, None)
    if publish_change:
        await publish_change({'type': 'notes_changed', 'note_id': note_id})
    return result


@router.get('/notes')
async def notes(offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=100)):
    return await store.call('list', data={'offset': offset, 'limit': limit})


@router.get('/notes/{note_id}/items')
async def items(note_id: str, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=100)):
    return await store.call('items', note_id, {'offset': offset, 'limit': limit})


@router.post('/notes', status_code=201)
async def create_note(request: Request):
    return await mutate('create_note', data=await body(request))


@router.patch('/notes/{note_id}')
async def update_note(note_id: str, request: Request):
    return await mutate('update_note', note_id, await body(request))


@router.delete('/notes/{note_id}')
async def delete_note(note_id: str):
    return await mutate('delete_note', note_id)


@router.post('/notes/{note_id}/items', status_code=201)
async def create_item(note_id: str, request: Request):
    return await mutate('create_item', note_id, await body(request))


@router.patch('/note-items/{item_id}')
async def update_item(item_id: str, request: Request):
    return await mutate('update_item', item_id, await body(request))


@router.delete('/note-items/{item_id}')
async def delete_item(item_id: str):
    return await mutate('delete_item', item_id)


@router.put('/notes/{note_id}/items/order')
async def reorder_items(note_id: str, request: Request):
    return await mutate('reorder', note_id, await body(request))


@router.post('/notes/import-shopping')
async def import_shopping(request: Request):
    return await mutate('import', data=await body(request))


async def send_view(socket, note_id=None, offset=0, request_id=0, reset_view=False):
    try:
        result = await store.call('items' if note_id else 'list', note_id, {'offset': offset, 'limit': 16})
    except HTTPException as exc:
        if exc.status_code == 404 and note_id:
            await send_view(socket, None, 0, request_id, reset_view=True)
            return
        await socket.send_json({'type': 'notes_error', 'request_id': request_id, 'error': exc.detail})
        return
    views[socket] = (note_id, result['offset'])
    if note_id:
        result = {**result, 'note': {'id': note_id, 'title': result['note']['title'][:96]},
                  'items': [{'id': r['id'], 'text': r['text'][:160], 'completed': r['completed']} for r in result['items']]}
    else:
        result['notes'] = [{'id': r['id'], 'title': r['title'][:96], 'item_count': r['item_count'], 'completed_count': r['completed_count']} for r in result['notes']]
    await socket.send_json({'type': 'note_items' if note_id else 'notes', 'request_id': request_id, 'reset_view': reset_view, **result})


async def handle_socket(socket, payload):
    kind = payload.get('type', '')
    request_id = payload.get('request_id', 0)
    if type(request_id) is not int or not 0 <= request_id <= 4294967295:
        request_id = 0
    try:
        if kind in {'notes_list', 'notes_open'}:
            offset = payload.get('offset', 0)
            if type(offset) is not int or not 0 <= offset <= 10000000:
                raise HTTPException(400, 'invalid_offset')
            note_id = identifier(payload.get('note_id')) if kind == 'notes_open' else None
            async with view_lock:
                await send_view(socket, note_id, offset, request_id)
        elif kind == 'notes_set':
            if not device_authorized or not device_authorized(socket):
                raise HTTPException(403, 'device_not_paired')
            item_id = identifier(payload.get('item_id'))
            if type(payload.get('completed')) is not bool:
                raise HTTPException(400, 'completed_must_be_boolean')
            # Explicit desired state makes retries idempotent.
            await mutate('update_item', item_id, {'completed': payload['completed']})
            await socket.send_json({'type': 'notes_result', 'request_id': request_id, 'ok': True})
    except HTTPException as exc:
        await socket.send_json({'type': 'notes_error', 'request_id': request_id, 'error': exc.detail})
