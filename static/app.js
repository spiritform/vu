const $ = (id) => document.getElementById(id);

const state = {
  items: [],
  visible: [],  // filtered view
  lbIndex: -1,
  filterHearts: false,
  selection: new Set(),   // set of item objects
  lastSelected: null,     // anchor for shift-range select
  compareB: null,         // second item when in compare view
  compareA_view: null,    // first item when in compare view
};

// ---------- API ----------
async function api(method, path, body) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (body) opts.body = JSON.stringify(body);
  const r = await fetch(path, opts);
  if (!r.ok) {
    const t = await r.text();
    throw new Error(t || r.statusText);
  }
  return r.json();
}

// ---------- Toast ----------
let toastTimer;
function toast(msg, kind = '') {
  const el = $('toast');
  el.textContent = msg;
  el.className = 'toast ' + kind;
  el.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add('hidden'), 2400);
}

// ---------- Scan ----------
// Non-destructive rescan — used by auto-scan to pull in newly-added files
// without resetting selection, lightbox, or filter.
async function refreshScan() {
  const folder = $('folder').value.trim();
  if (!folder) return;
  const recursive = $('recursive').checked;
  const res = await api('POST', '/api/scan', { folder, recursive });

  const oldSelectedPaths = new Set([...state.selection].map(it => it.path));
  const oldLbPath = state.lbIndex >= 0 && state.visible[state.lbIndex]
    ? state.visible[state.lbIndex].path : null;

  state.items = res.items;
  state.scannedRoot = (res.root || '').replace(/\\/g, '/').toLowerCase();

  applyFilter();

  // restore selection by path identity
  state.selection.clear();
  for (const it of state.items) {
    if (oldSelectedPaths.has(it.path)) state.selection.add(it);
  }
  refreshTileSelectedClasses();

  // restore lightbox index if the same item still exists
  if (oldLbPath) {
    const idx = state.visible.findIndex(it => it.path === oldLbPath);
    if (idx >= 0) state.lbIndex = idx;
    else closeLightbox();
  }

  updateCount();
}

let autoScanTimer = null;
function setAutoScan(on) {
  if (autoScanTimer) { clearInterval(autoScanTimer); autoScanTimer = null; }
  localStorage.setItem('autoScan', on ? '1' : '');
  if (!on) return;
  autoScanTimer = setInterval(async () => {
    if (!state.scannedRoot) return;
    // skip while overlays are open to avoid interrupting the user
    if (!$('lightbox').classList.contains('hidden')) return;
    if (!$('compare').classList.contains('hidden')) return;
    if (!$('exportModal').classList.contains('hidden')) return;
    try { await refreshScan(); } catch (_) {}
  }, 2500);
}

async function scan() {
  const folder = $('folder').value.trim();
  if (!folder) { toast('paste a folder path first', 'error'); return; }
  const recursive = $('recursive').checked;
  try {
    const res = await api('POST', '/api/scan', { folder, recursive });
    // Fully reset view state — treat every scan as a fresh session
    if (!$('lightbox').classList.contains('hidden')) closeLightbox();
    if (!$('compare').classList.contains('hidden')) closeCompare();
    if (!$('exportModal').classList.contains('hidden')) closeExport();
    state.items = res.items;
    state.visible = [];
    state.scannedRoot = (res.root || '').replace(/\\/g, '/').toLowerCase();
    state.selection.clear();
    state.lastSelected = null;
    state.lbIndex = -1;
    state.filterHearts = false;
    $('filterBtn').classList.remove('active');
    localStorage.setItem('lastFolder', folder);
    localStorage.setItem('recursive', recursive ? '1' : '');
    applyFilter();
    updateCount();
    if (!res.items.length) toast('no media found in folder');
  } catch (e) {
    toast(e.message, 'error');
  }
}

// ---------- Filter / render ----------
function applyFilter() {
  state.visible = state.filterHearts
    ? state.items.filter(x => x.hearted)
    : state.items;
  render();
}

function updateCount() {
  const total = state.items.length;
  const hearts = state.items.filter(x => x.hearted).length;
  $('countLabel').textContent = total ? `${total} items` : '—';
  $('heartCount').textContent = hearts;
  updateSelectionUI();
}

function updateSelectionUI() {
  const n = state.selection.size;
  $('selChip').classList.toggle('hidden', n === 0);
  $('selCount').textContent = `${n} selected`;
  $('compareBtn').classList.toggle('hidden', n !== 2);
  $('exportBtn').textContent = n > 0 ? `export (${n})` : 'export';
}

function fileUrl(path) {
  return `/api/file?path=${encodeURIComponent(path)}`;
}
function thumbUrl(path) {
  return `/api/thumb?path=${encodeURIComponent(path)}`;
}

function render() {
  const grid = $('grid');
  const empty = $('empty');
  grid.innerHTML = '';

  if (!state.visible.length) {
    empty.classList.remove('hidden');
    empty.querySelector('.empty-text').textContent =
      state.items.length
        ? (state.filterHearts ? 'no hearted items yet' : 'no items')
        : 'paste a folder path above and hit scan';
    return;
  }
  empty.classList.add('hidden');

  const frag = document.createDocumentFragment();
  state.visible.forEach((item, i) => {
    const tile = document.createElement('div');
    let cls = 'tile';
    if (item.hearted) cls += ' hearted';
    if (state.selection.has(item)) cls += ' selected';
    tile.className = cls;
    tile.dataset.index = i;
    tile.title = 'click to select · ctrl+click to add · shift+click for range · double-click to view';

    const media = document.createElement('img');
    media.loading = 'lazy';
    media.src = thumbUrl(item.path);
    tile.appendChild(media);
    // grid tiles are not draggable — drag-out is reserved for the full-screen view
    // (img already has pointer-events: none in CSS to bypass browser auto-drag too)

    const actions = document.createElement('div');
    actions.className = 'tile-actions';

    const badge = document.createElement('span');
    badge.className = 'badge';
    badge.title = item.kind;
    if (item.kind === 'video') {
      badge.innerHTML = '<svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>';
    } else {
      badge.innerHTML = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="9" cy="10" r="1.5" fill="currentColor" stroke="none"/><path d="M21 16l-5-5-8 8"/></svg>';
    }
    actions.appendChild(badge);

    const del = document.createElement('button');
    del.className = 'tile-btn delete';
    del.title = 'remove from viewer';
    del.innerHTML = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12"/><path d="M18 6l-12 12"/></svg>';
    del.addEventListener('click', (e) => {
      e.stopPropagation();
      hideItem(item);
    });
    actions.appendChild(del);

    const heart = document.createElement('button');
    heart.className = 'tile-btn heart heart-btn' + (item.hearted ? ' on' : '');
    heart.dataset.path = item.path;
    heart.textContent = '♥';
    heart.title = 'heart';
    heart.addEventListener('click', (e) => {
      e.stopPropagation();
      toggleHeart(item, heart);
    });
    actions.appendChild(heart);

    tile.appendChild(actions);

    const name = document.createElement('div');
    name.className = 'filename';
    name.textContent = item.name;
    tile.appendChild(name);

    tile.addEventListener('click', (e) => toggleSelect(item, tile, {
      range: e.shiftKey,
      toggle: e.ctrlKey || e.metaKey,
    }));
    tile.addEventListener('dblclick', () => openLightbox(i));
    frag.appendChild(tile);
  });
  grid.appendChild(frag);
}

// ---------- Heart ----------
async function toggleHeart(item, btnEl) {
  const newVal = !item.hearted;
  try {
    await api('POST', '/api/heart', { path: item.path, hearted: newVal });
    item.hearted = newVal;
    // sync every heart button for this item (tile + compare panes)
    document.querySelectorAll(`.heart-btn[data-path="${CSS.escape(item.path)}"]`)
      .forEach(el => el.classList.toggle('on', newVal));
    if (btnEl) {
      btnEl.classList.add('heart-pop');
      setTimeout(() => btnEl.classList.remove('heart-pop'), 300);
    }
    const tile = document.querySelector(`.tile[data-index="${state.visible.indexOf(item)}"]`);
    if (tile) tile.classList.toggle('hearted', newVal);
    updateCount();
    if (state.filterHearts && !newVal) {
      applyFilter();
      if (state.lbIndex >= 0) closeLightbox();
    }
    // lightbox button sync
    if (state.lbIndex >= 0 && state.visible[state.lbIndex] === item) {
      $('lbHeart').classList.toggle('on', newVal);
    }
  } catch (e) {
    toast(e.message, 'error');
  }
}

// ---------- Selection ----------
function refreshTileSelectedClasses() {
  document.querySelectorAll('.tile').forEach((t, idx) => {
    t.classList.toggle('selected', state.selection.has(state.visible[idx]));
  });
}

function toggleSelect(item, tileEl, opts = {}) {
  // Shift-click: replace selection with range from anchor to current
  if (opts.range && state.lastSelected && state.lastSelected !== item) {
    const ia = state.visible.indexOf(state.lastSelected);
    const ib = state.visible.indexOf(item);
    if (ia >= 0 && ib >= 0) {
      const [lo, hi] = ia < ib ? [ia, ib] : [ib, ia];
      state.selection.clear();
      for (let i = lo; i <= hi; i++) state.selection.add(state.visible[i]);
      refreshTileSelectedClasses();
      updateSelectionUI();
      return;
    }
  }
  // Ctrl/Cmd+click: toggle this one in/out
  if (opts.toggle) {
    if (state.selection.has(item)) {
      state.selection.delete(item);
      if (tileEl) tileEl.classList.remove('selected');
    } else {
      state.selection.add(item);
      if (tileEl) tileEl.classList.add('selected');
    }
    state.lastSelected = item;
    updateSelectionUI();
    return;
  }
  // Plain click: select only this one (or deselect if it's the only selected)
  const onlyThis = state.selection.size === 1 && state.selection.has(item);
  state.selection.clear();
  if (!onlyThis) state.selection.add(item);
  state.lastSelected = item;
  refreshTileSelectedClasses();
  updateSelectionUI();
}

function clearSelection() {
  if (!state.selection.size) return;
  state.selection.clear();
  state.lastSelected = null;
  document.querySelectorAll('.tile.selected').forEach(t => t.classList.remove('selected'));
  updateSelectionUI();
}

function doCompare() {
  const sel = [...state.selection];
  if (sel.length !== 2) {
    toast('select exactly 2 to compare', 'error');
    return;
  }
  openCompare(sel[0], sel[1]);
}

function renderPane(paneEl, item) {
  paneEl.innerHTML = '';
  if (!item) return;

  const stage = document.createElement('div');
  stage.className = 'cmp-stage-inner';
  paneEl.appendChild(stage);

  let el;
  if (item.kind === 'video') {
    el = document.createElement('video');
    el.src = fileUrl(item.path);
    el.controls = true;
    el.muted = true;
    el.loop = true;
    el.autoplay = true;
  } else {
    el = document.createElement('img');
    el.src = fileUrl(item.path);
  }
  stage.appendChild(el);

  const cmpActions = document.createElement('div');
  cmpActions.className = 'cmp-actions';

  const del = document.createElement('button');
  del.className = 'cmp-btn cmp-del';
  del.title = 'remove from viewer';
  del.innerHTML = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12"/><path d="M18 6l-12 12"/></svg>';
  del.addEventListener('click', (e) => {
    e.stopPropagation();
    hideItem(item);
  });
  cmpActions.appendChild(del);

  const heart = document.createElement('button');
  heart.className = 'cmp-btn cmp-heart heart-btn' + (item.hearted ? ' on' : '');
  heart.dataset.path = item.path;
  heart.textContent = '♥';
  heart.title = 'heart';
  heart.addEventListener('click', (e) => {
    e.stopPropagation();
    toggleHeart(item, heart);
  });
  cmpActions.appendChild(heart);

  stage.appendChild(cmpActions);

  const caption = document.createElement('div');
  caption.className = 'cmp-filename';
  caption.textContent = item.name;
  paneEl.appendChild(caption);

  const fit = () => fitPane(paneEl, stage, el, caption);
  if (item.kind === 'video') {
    el.addEventListener('loadedmetadata', fit);
  } else {
    if (el.complete && el.naturalWidth) fit(); else el.addEventListener('load', fit);
  }
  paneEl._fit = fit;
}

function fitPane(paneEl, stageEl, mediaEl, captionEl) {
  const pr = paneEl.getBoundingClientRect();
  const capH = captionEl.offsetHeight;
  const gap = 6;
  const availH = Math.max(0, pr.height - capH - gap);
  const availW = pr.width;

  let nw, nh;
  if (mediaEl.tagName === 'VIDEO') { nw = mediaEl.videoWidth; nh = mediaEl.videoHeight; }
  else { nw = mediaEl.naturalWidth; nh = mediaEl.naturalHeight; }
  if (!nw || !nh || availH <= 0 || availW <= 0) return;

  const scale = Math.min(availW / nw, availH / nh);
  const dw = Math.floor(nw * scale);
  const dh = Math.floor(nh * scale);

  stageEl.style.width = dw + 'px';
  stageEl.style.height = dh + 'px';
  captionEl.style.width = dw + 'px';
}

window.addEventListener('resize', () => {
  ['cmpPaneA', 'cmpPaneB'].forEach(id => {
    const p = $(id);
    if (p && p._fit) p._fit();
  });
});

function openCompare(a, b) {
  state.compareB = b;
  state.compareA_view = a;
  renderPane($('cmpPaneA'), a);
  renderPane($('cmpPaneB'), b);
  $('compare').classList.remove('hidden');
}

function closeCompare() {
  $('compare').classList.add('hidden');
  ['cmpPaneA', 'cmpPaneB'].forEach(id => {
    const p = $(id);
    p.querySelectorAll('video').forEach(v => v.pause());
    p.innerHTML = '';
    p._placeHeart = null;
  });
  state.compareB = null;
  state.compareA_view = null;
}

function swapCompare() {
  if (!state.compareA_view || !state.compareB) return;
  const a = state.compareB;
  const b = state.compareA_view;
  openCompare(a, b);
}

// ---------- Lightbox ----------
function openLightbox(index) {
  state.lbIndex = index;
  renderLightbox();
  $('lightbox').classList.remove('hidden');
}

function closeLightbox() {
  state.lbIndex = -1;
  $('lightbox').classList.add('hidden');
  const stage = $('lbStage');
  // pause any playing video
  stage.querySelectorAll('video').forEach(v => v.pause());
  stage.innerHTML = '';
}

function renderLightbox() {
  const item = state.visible[state.lbIndex];
  if (!item) return closeLightbox();

  $('lbName').textContent = item.name;
  $('lbIndex').textContent = `${state.lbIndex + 1} / ${state.visible.length}`;
  $('lbHeart').classList.toggle('on', item.hearted);

  const stage = $('lbStage');
  stage.innerHTML = '';
  let el;
  if (item.kind === 'video') {
    el = document.createElement('video');
    el.src = fileUrl(item.path);
    el.controls = true;
    el.autoplay = true;
  } else {
    el = document.createElement('img');
    el.src = fileUrl(item.path);
  }
  stage.appendChild(el);
}

function lbStep(delta) {
  if (state.lbIndex < 0) return;
  let next = state.lbIndex + delta;
  if (next < 0) next = state.visible.length - 1;
  if (next >= state.visible.length) next = 0;
  state.lbIndex = next;
  renderLightbox();
}

async function hideItem(item) {
  if (!item) return;
  try {
    await api('POST', '/api/hide', { path: item.path });
    state.items = state.items.filter(x => x !== item);
    state.visible = state.visible.filter(x => x !== item);
    state.selection.delete(item);
    // close compare if the hidden item was one of the two being compared
    if (state.compareA_view === item || state.compareB === item) {
      closeCompare();
    }
    updateCount();
    if (state.lbIndex >= 0) {
      if (!state.visible.length) {
        closeLightbox();
      } else {
        if (state.lbIndex >= state.visible.length) state.lbIndex = 0;
        renderLightbox();
      }
    }
    render();
    toast('hidden from viewer');
  } catch (e) {
    toast(e.message, 'error');
  }
}

async function lbDelete() {
  hideItem(state.visible[state.lbIndex]);
}

// ---------- Export ----------
function openExport() {
  const hasSel = state.selection.size > 0;
  const hasHearts = state.items.some(x => x.hearted);
  if (!hasSel && !hasHearts) {
    toast('select some items or heart some items to export', 'error');
    return;
  }
  $('exportModal').classList.remove('hidden');
  $('exportName').focus();
  $('exportName').select();
}
function closeExport() { $('exportModal').classList.add('hidden'); }

async function doExport(zip) {
  const subfolder = $('exportName').value.trim() || 'selects';
  const move = $('exportMove').checked;
  const paths = state.selection.size
    ? [...state.selection].map(it => it.path)
    : null;
  try {
    const res = await api('POST', '/api/export', { subfolder, move, zip, paths });
    const verb = move ? 'moved' : 'copied';
    const target = zip ? `${subfolder}.zip` : `${subfolder}/`;
    toast(`${verb} ${res.exported} → ${target}`, 'success');
    closeExport();
    if (move) await scan();
  } catch (e) {
    toast(e.message, 'error');
  }
}

// ---------- Keyboard ----------
document.addEventListener('keydown', (e) => {
  const inInput = /^(input|textarea)$/i.test(document.activeElement?.tagName || '');
  // Compare view open
  if (!$('compare').classList.contains('hidden')) {
    if (e.key === 'Escape') { e.preventDefault(); closeCompare(); return; }
    if (e.key === 's' || e.key === 'S') { e.preventDefault(); swapCompare(); return; }
    return;
  }
  // Global when lightbox open
  if (!$('lightbox').classList.contains('hidden')) {
    if (e.key === 'Escape') { e.preventDefault(); closeLightbox(); return; }
    if (e.key === 'ArrowLeft') { e.preventDefault(); lbStep(-1); return; }
    if (e.key === 'ArrowRight') { e.preventDefault(); lbStep(1); return; }
    if (e.key === 'h' || e.key === 'H') {
      e.preventDefault();
      const item = state.visible[state.lbIndex];
      if (item) toggleHeart(item, $('lbHeart'));
      return;
    }
    if (e.key === 'Delete' || e.key === 'Backspace') {
      e.preventDefault(); lbDelete(); return;
    }
    return;
  }
  // Modal open
  if (!$('exportModal').classList.contains('hidden')) {
    if (e.key === 'Escape') closeExport();
    if (e.key === 'Enter' && document.activeElement?.id === 'exportName') doExport(false);
    return;
  }
  if (inInput) {
    if (e.key === 'Enter' && document.activeElement?.id === 'folder') scan();
    return;
  }
  if (e.key === 'f' || e.key === 'F') {
    state.filterHearts = !state.filterHearts;
    $('filterBtn').classList.toggle('active', state.filterHearts);
    applyFilter();
    return;
  }
  if ((e.key === 'Delete' || e.key === 'Backspace') && state.selection.size) {
    e.preventDefault();
    // copy before iterating, since hideItem mutates state.selection
    const toHide = [...state.selection];
    toHide.forEach(item => hideItem(item));
    return;
  }
  if (e.key === 'h' || e.key === 'H') {
    if (!state.selection.size) return;
    [...state.selection].forEach(item => toggleHeart(item, null));
    return;
  }
  if ((e.key === 'c' || e.key === 'C') && state.selection.size === 2) {
    e.preventDefault();
    doCompare();
    return;
  }
  if (e.key === 'Escape' && state.selection.size) {
    clearSelection();
    return;
  }
});

// ---------- Wire up ----------
$('scanBtn').addEventListener('click', scan);
$('autoScan').addEventListener('change', (e) => setAutoScan(e.target.checked));
$('filterBtn').addEventListener('click', () => {
  state.filterHearts = !state.filterHearts;
  $('filterBtn').classList.toggle('active', state.filterHearts);
  applyFilter();
});
$('exportBtn').addEventListener('click', openExport);
$('compareBtn').addEventListener('click', doCompare);
$('clearSelBtn').addEventListener('click', clearSelection);
$('exportCancel').addEventListener('click', closeExport);
$('exportGo').addEventListener('click', () => doExport(false));
$('exportZip').addEventListener('click', () => doExport(true));
$('lbClose').addEventListener('click', closeLightbox);
$('lbPrev').addEventListener('click', () => lbStep(-1));
$('lbNext').addEventListener('click', () => lbStep(1));
$('lbHeart').addEventListener('click', () => {
  const item = state.visible[state.lbIndex];
  if (item) toggleHeart(item, $('lbHeart'));
});
$('lbDelete').addEventListener('click', lbDelete);
$('lightbox').addEventListener('click', (e) => {
  if (e.target.id === 'lightbox' || e.target.id === 'lbStage') closeLightbox();
});
$('exportModal').addEventListener('click', (e) => {
  if (e.target.id === 'exportModal') closeExport();
});
$('cmpClose').addEventListener('click', closeCompare);
$('cmpSwap').addEventListener('click', swapCompare);
$('compare').addEventListener('click', (e) => {
  if (e.target.id === 'compare') closeCompare();
});

// Prevent accidental text selection when shift-clicking tiles
document.addEventListener('mousedown', (e) => {
  if (e.shiftKey && e.target.closest('.tile')) e.preventDefault();
});

// Ctrl+scroll to resize tiles
const TILE_MIN = 100;
const TILE_MAX = 480;
let tileSize = parseInt(localStorage.getItem('tileSize'), 10);
if (!Number.isFinite(tileSize) || tileSize < TILE_MIN || tileSize > TILE_MAX) tileSize = 220;
function applyTileSize() {
  document.documentElement.style.setProperty('--tile-size', tileSize + 'px');
}
applyTileSize();
document.addEventListener('wheel', (e) => {
  if (!e.ctrlKey) return;
  e.preventDefault();
  const step = 20;
  const delta = e.deltaY < 0 ? step : -step;
  const next = Math.min(TILE_MAX, Math.max(TILE_MIN, tileSize + delta));
  if (next === tileSize) return;
  tileSize = next;
  applyTileSize();
  localStorage.setItem('tileSize', String(tileSize));
}, { passive: false });

// Hook used by the host (tray/hotkey) to jump into a folder
window.openFolder = function(folder) {
  $('folder').value = folder;
  scan();
};

// Hook used by the host when files/folders are dropped onto the window
window.openDropped = async function(paths) {
  if (!paths || !paths.length) return;
  try {
    const res = await api('POST', '/api/drop', { paths });
    if (!res.folder) return;
    const droppedRoot = res.folder.replace(/\\/g, '/').toLowerCase();
    // If they're dropping files we already have loaded (e.g. drag-out of full-screen
    // view dropped back on VU itself), don't blow away the current state.
    if (state.scannedRoot && state.scannedRoot === droppedRoot) return;
    $('folder').value = res.folder;
    await scan();
  } catch (e) {
    toast(e.message, 'error');
  }
};

// Restore last folder (or use ?folder= URL param if present)
const params = new URLSearchParams(window.location.search);
const urlFolder = params.get('folder');
if (urlFolder) {
  $('folder').value = urlFolder;
  if (localStorage.getItem('recursive')) $('recursive').checked = true;
  // defer scan so listeners are wired
  setTimeout(() => scan(), 0);
} else {
  const last = localStorage.getItem('lastFolder');
  if (last) $('folder').value = last;
  if (localStorage.getItem('recursive')) $('recursive').checked = true;
}
if (localStorage.getItem('autoScan')) {
  $('autoScan').checked = true;
  setAutoScan(true);
}
