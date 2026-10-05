let noteGroups = [], noteItems = [], selectedNote = null;
let notesBusy = false, notesLoaded = false, notesRefreshing = false, notesRefreshAgain = false;
let noteEditorAction = null, noteDeleteId = null, draggedItem = null;
let noteViewGeneration = 0;

async function notesRequest(path, method = 'GET', data) {
  const response = await fetch(apiBaseUrl + path, {
    method, headers: data === undefined ? {} : {'Content-Type': 'application/json'},
    body: data === undefined ? undefined : JSON.stringify(data),
    signal: AbortSignal.timeout(30000), cache: 'no-store'
  });
  let result;
  try { result = await response.json(); } catch { result = {}; }
  if (!response.ok) {
    const error = new Error(response.status === 404 ? 'Bu başlık veya madde artık mevcut değil.' : response.status === 409 ? 'Liste değişti. Güncel sıralama yüklendi; tekrar deneyin.' : response.status === 400 ? 'Geçerli bir metin girin.' : 'Notlara ulaşılamıyor. Lütfen tekrar deneyin.');
    error.status = response.status;
    throw error;
  }
  return result;
}

async function notesPages(path, key) {
  let records = [], offset = 0, page;
  do {
    page = await notesRequest(`${path}?offset=${offset}&limit=100`);
    // IDs can shift between pages when another client edits the list.
    for (const row of page[key]) if (!records.some(value => value.id === row.id)) records.push(row);
    offset = page.offset + page[key].length;
  } while (offset < page.total && page[key].length && offset > page.offset);
  return {...page, [key]: records};
}

async function migrateLegacyNotes() {
  const legacy = loadSaved('shopping', null);
  if (!Array.isArray(legacy) || !legacy.length || loadSaved('notesShoppingMigrated', false)) return;
  const items = legacy.filter(value => typeof value === 'string' && value.trim());
  if (!items.length) return;
  await notesRequest('/notes/import-shopping', 'POST', {items});
  // Retain the original localStorage snapshot as a rollback backup.
  save('notesShoppingMigrated', true);
}

async function refreshNotes() {
  if (notesRefreshing) { notesRefreshAgain = true; return; }
  notesRefreshing = true;
  const generation = noteViewGeneration;
  const currentId = selectedNote?.id;
  try {
    await migrateLegacyNotes();
    const result = await notesPages('/notes', 'notes');
    noteGroups = result.notes;
    notesLoaded = true;
    if (currentId && generation === noteViewGeneration) {
      const group = noteGroups.find(note => note.id === currentId);
      if (!group) { selectedNote = null; noteItems = []; noteViewGeneration++; }
      else {
        const detail = await notesPages(`/notes/${currentId}/items`, 'items');
        if (generation === noteViewGeneration && selectedNote?.id === currentId) {
          selectedNote = {...group, ...detail.note}; noteItems = detail.items;
        }
      }
    }
    received('notes');
    ui('notesSync').textContent = 'Sunucuyla güncel';
    feedback('notesFeedback', ''); ui('retryNotes').hidden = true;
  } catch (error) {
    feedback('notesFeedback', error.message || 'Notlar yüklenemedi. Tekrar deneyin.', true);
    ui('retryNotes').hidden = false;
    ui('notesSync').textContent = notesLoaded ? 'Son alınan veri · bağlantı bekleniyor' : 'Notlar yüklenemedi';
    if (error.status === 404 && currentId === selectedNote?.id) {
      selectedNote = null; noteItems = []; noteViewGeneration++;
      notesRefreshAgain = true;
    }
  } finally {
    notesRefreshing = false; renderNotes();
    if (notesRefreshAgain) { notesRefreshAgain = false; refreshNotes(); }
  }
}

async function openNote(id) {
  selectedNote = noteGroups.find(note => note.id === id);
  if (!selectedNote) return;
  noteItems = []; const generation = ++noteViewGeneration;
  renderNotes(); ui('notesSync').textContent = 'Maddeler yükleniyor…';
  try {
    const result = await notesPages(`/notes/${id}/items`, 'items');
    if (generation !== noteViewGeneration) return;
    selectedNote = {...selectedNote, ...result.note}; noteItems = result.items;
    ui('notesSync').textContent = 'Sunucuyla güncel'; feedback('notesFeedback', '');
  } catch (error) {
    if (generation === noteViewGeneration) {
      feedback('notesFeedback', error.message, true); ui('retryNotes').hidden = false;
      if (error.status === 404) { selectedNote = null; noteViewGeneration++; }
    }
  }
  if (generation === noteViewGeneration || !selectedNote) renderNotes();
}

function notesElement(tag, className, text) {
  const element = document.createElement(tag);
  element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function noteAction(label, action, id, text = label) {
  const button = notesElement('button', 'note-action', text);
  button.type = 'button'; button.title = label; button.setAttribute('aria-label', label);
  button.dataset.action = action; button.dataset.id = id; button.disabled = notesBusy;
  return button;
}

function renderNotes() {
  ui('notesOverview').hidden = Boolean(selectedNote);
  ui('noteDetail').hidden = !selectedNote;
  ui('notesGroups').replaceChildren(); ui('noteItems').replaceChildren();
  if (!noteGroups.length) {
    const empty = notesElement('div', 'empty-state');
    empty.append(notesElement('strong', '', notesLoaded ? 'Henüz bir not başlığı oluşturmadın.' : 'Notlar yükleniyor…'));
    if (notesLoaded) {
      const create = noteAction('İlk başlığı oluştur', 'create', '', '+ İlk başlığı oluştur');
      create.className = 'secondary-button'; empty.append(create);
    }
    ui('notesGroups').append(empty);
  }
  for (const note of noteGroups) {
    const card = notesElement('article', 'note-card');
    const open = notesElement('button', 'note-open'); open.type = 'button';
    open.dataset.action = 'open'; open.dataset.id = note.id;
    open.append(notesElement('span', 'note-card-title', note.title), notesElement('span', 'caption', `${note.item_count} madde · ${note.completed_count} tamamlandı`));
    const actions = notesElement('div', 'note-card-actions');
    actions.append(noteAction('Başlığı düzenle', 'rename', note.id, 'Düzenle'), noteAction('Başlığı sil', 'delete-note', note.id, 'Sil'));
    const progress = document.createElement('progress'); progress.max = note.item_count || 1; progress.value = note.completed_count;
    progress.setAttribute('aria-label', `${note.title}: tamamlanan maddeler`);
    card.append(open, actions, progress); ui('notesGroups').append(card);
  }
  if (selectedNote) {
    ui('noteTitle').textContent = selectedNote.title;
    ui('noteProgress').textContent = `${noteItems.length} madde · ${noteItems.filter(item => item.completed).length} tamamlandı`;
    if (!noteItems.length) ui('noteItems').append(notesElement('li', 'empty-state', 'Bu başlıkta henüz madde yok.'));
    for (const [index, item] of noteItems.entries()) {
      const row = notesElement('li', 'note-row' + (item.completed ? ' completed' : '')); row.dataset.id = item.id;
      const grip = noteAction('Sürükleyerek sırala', 'grip', item.id, '⠿'); grip.classList.add('drag-handle'); grip.draggable = true;
      const label = notesElement('label', 'item-check-label');
      const check = document.createElement('input'); check.type = 'checkbox'; check.checked = item.completed;
      check.dataset.action = 'toggle'; check.dataset.id = item.id; check.disabled = notesBusy;
      check.setAttribute('aria-label', `${item.text}: ${item.completed ? 'tamamlanmadı yap' : 'tamamlandı yap'}`);
      label.append(check, notesElement('span', 'row-name', item.text));
      const actions = notesElement('div', 'item-actions');
      const up = noteAction('Yukarı taşı', 'up', item.id, '↑'); up.disabled ||= index === 0;
      const down = noteAction('Aşağı taşı', 'down', item.id, '↓'); down.disabled ||= index === noteItems.length - 1;
      actions.append(up, down, noteAction('Maddeyi düzenle', 'edit-item', item.id, 'Düzenle'), noteAction('Maddeyi sil', 'delete-item', item.id, 'Sil'));
      row.append(grip, label, actions); ui('noteItems').append(row);
    }
  }
  ui('newNote').disabled = notesBusy; ui('renameNote').disabled = notesBusy;
  ui('sendButton').disabled = notesBusy; ui('itemInput').disabled = notesBusy;
  renderNotesPreview();
}

function renderNotesPreview() {
  const preview = ui('notesPreview'); preview.replaceChildren();
  const rows = selectedNote ? noteItems : noteGroups;
  if (!rows.length) preview.append(notesElement('li', 'empty', selectedNote ? 'Bu liste boş.' : 'Henüz not yok.'));
  for (const row of rows.slice(0, 4)) preview.append(notesElement('li', '', selectedNote ? `${row.completed ? '☑' : '☐'} ${row.text}` : row.title));
  if (previewPanel === 'notes') {
    ui('previewTitle').textContent = selectedNote ? selectedNote.title : 'NOTLAR';
    ui('itemCount').textContent = String(rows.length);
  }
}

async function changeNotes(path, method, data) {
  if (notesBusy) return false;
  notesBusy = true; renderNotes();
  try { await notesRequest(path, method, data); return true; }
  catch (error) { feedback('notesFeedback', error.message, true); notify(error.message); return false; }
  finally { notesBusy = false; renderNotes(); refreshNotes(); }
}

function editNote(kind, id) {
  const item = kind === 'edit-item' ? noteItems.find(value => value.id === id) : noteGroups.find(value => value.id === id);
  noteEditorAction = {kind, id};
  ui('noteEditorTitle').textContent = kind === 'create' ? 'Yeni Başlık' : kind === 'edit-item' ? 'Maddeyi düzenle' : 'Başlığı düzenle';
  ui('editorLabel').textContent = kind === 'edit-item' ? 'Madde' : 'Başlık adı';
  ui('noteEditorInput').maxLength = kind === 'edit-item' ? 2000 : 240;
  ui('noteEditorInput').value = kind === 'create' ? '' : kind === 'edit-item' ? item?.text || '' : item?.title || '';
  ui('editorError').hidden = true; ui('noteEditor').showModal(); ui('noteEditorInput').focus();
}

async function reorderNoteItems(ids) {
  if (selectedNote) await changeNotes(`/notes/${selectedNote.id}/items/order`, 'PUT', {ids});
}

function initNotes() {
  ui('newNote').addEventListener('click', () => editNote('create'));
  ui('renameNote').addEventListener('click', () => editNote('rename', selectedNote.id));
  ui('notesBack').addEventListener('click', () => { selectedNote = null; noteItems = []; noteViewGeneration++; renderNotes(); refreshNotes(); });
  ui('retryNotes').addEventListener('click', refreshNotes);
  ui('cancelEditor').addEventListener('click', () => ui('noteEditor').close());
  ui('cancelNoteDelete').addEventListener('click', () => ui('noteDeleteDialog').close());
  ui('noteEditorForm').addEventListener('submit', async event => {
    event.preventDefault(); const {kind, id} = noteEditorAction;
    const value = ui('noteEditorInput').value.trim(); if (!value) return;
    ui('saveEditor').disabled = true;
    const ok = await changeNotes(kind === 'create' ? '/notes' : kind === 'edit-item' ? `/note-items/${id}` : `/notes/${id}`, kind === 'create' ? 'POST' : 'PATCH', kind === 'edit-item' ? {text: value} : {title: value});
    ui('saveEditor').disabled = false;
    if (ok) { ui('noteEditor').close(); notify('Kaydedildi.'); }
    else { ui('editorError').textContent = 'Kaydedilemedi. Metniniz korunuyor; tekrar deneyin.'; ui('editorError').hidden = false; }
  });
  ui('confirmNoteDelete').addEventListener('click', async () => {
    const id = noteDeleteId; ui('confirmNoteDelete').disabled = true;
    if (await changeNotes(`/notes/${id}`, 'DELETE')) { ui('noteDeleteDialog').close(); notify('Başlık silindi.'); }
    ui('confirmNoteDelete').disabled = false;
  });
  ui('itemComposer').addEventListener('submit', async event => {
    event.preventDefault(); const text = ui('itemInput').value.trim(); const id = selectedNote?.id;
    if (!text || !id) return;
    if (await changeNotes(`/notes/${id}/items`, 'POST', {text})) { ui('itemInput').value = ''; ui('itemInput').focus(); }
  });
  ui('notesGroups').addEventListener('click', event => {
    const target = event.target.closest('button[data-action]'); if (!target || notesBusy) return;
    const {action, id} = target.dataset;
    if (action === 'open') openNote(id);
    else if (action === 'delete-note') {
      noteDeleteId = id; const note = noteGroups.find(value => value.id === id);
      ui('noteDeleteDescription').textContent = `“${note.title}” başlığı ve tüm maddeleri silinir. Bu işlem geri alınamaz.`;
      ui('noteDeleteDialog').showModal();
    } else editNote(action, id);
  });
  ui('noteItems').addEventListener('change', event => {
    const target = event.target;
    if (target.dataset.action === 'toggle') changeNotes(`/note-items/${target.dataset.id}`, 'PATCH', {completed: target.checked});
  });
  ui('noteItems').addEventListener('click', event => {
    const target = event.target.closest('button[data-action]'); if (!target || notesBusy) return;
    const {action, id} = target.dataset;
    if (action === 'edit-item') editNote(action, id);
    if (action === 'delete-item') changeNotes(`/note-items/${id}`, 'DELETE');
    if (action === 'up' || action === 'down') {
      const ids = noteItems.map(item => item.id), index = ids.indexOf(id), next = index + (action === 'up' ? -1 : 1);
      if (next >= 0 && next < ids.length) { [ids[index], ids[next]] = [ids[next], ids[index]]; reorderNoteItems(ids); }
    }
  });
  ui('noteItems').addEventListener('dragstart', event => {
    const grip = event.target.closest('.drag-handle'); if (!grip || notesBusy) { event.preventDefault(); return; }
    draggedItem = grip.dataset.id; event.dataTransfer.setData('text/plain', draggedItem); event.dataTransfer.effectAllowed = 'move';
  });
  ui('noteItems').addEventListener('dragover', event => { if (draggedItem && !notesBusy) event.preventDefault(); });
  ui('noteItems').addEventListener('drop', event => {
    event.preventDefault(); const row = event.target.closest('.note-row');
    const ids = noteItems.map(item => item.id), from = ids.indexOf(draggedItem), to = ids.indexOf(row?.dataset.id);
    if (!notesBusy && from >= 0 && to >= 0 && from !== to) { ids.splice(to, 0, ...ids.splice(from, 1)); reorderNoteItems(ids); }
    draggedItem = null;
  });
  ui('noteItems').addEventListener('dragend', () => { draggedItem = null; });
  renderNotes(); refreshNotes();
}
