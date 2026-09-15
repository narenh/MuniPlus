import {
  store, load, subscribe, edit, undo, redo, canUndo, canRedo,
  select, setActiveLine, toggleLayer, stationById, lineById,
  isDirty, changes, esc, platformsOf,
} from './store.js';
import { api } from './api.js';
import {
  initMap, refresh, applyLayerToggles, flyToStation, fitLine, fitAll,
  resetNorth, onPick, hint, map,
} from './map.js';
import { initStrip, renderStrip } from './strip.js';
import { initInspector, renderInspector } from './inspector.js';
import {
  toast, showModal, hideModal, isModalOpen, openPalette, wirePalette,
  setPaletteHandlers, renderSheet, renderHistory,
} from './ui.js';

const $ = id => document.getElementById(id);
const DRAFT_KEY = 'muniplus.atlas.draft.v1';

// ============================================================ boot
(async function boot() {
  try {
    $('boot-msg').textContent = 'Reading data.json…';
    const state = await api.state();
    load(state);

    $('boot-msg').textContent = 'Drawing the network…';
    await initMap('map');

    buildRail();
    initStrip({ onPick: sid => { select(sid, null); renderAll(); }, onAdd: addStationToLine });
    initInspector({ onNeedStationPicker: cb => openPalette('station', cb) });
    wirePalette();
    wireChrome();
    wireKeys();

    setPaletteHandlers({
      onLine: id => { setActiveLine(id); fitLine(id); },
      onStation: id => { select(id, null); renderAll(); flyToStation(id); },
    });

    onPick(() => renderAll());
    subscribe(what => { if (what !== 'render') renderAll(); });

    store.onBlocked = () => toast(
      `Read-only — ${esc(store.readOnlyReason || 'this file cannot be written')}`, 'info');

    if (!store.readOnly) restoreDraft();
    renderAll();
    fitAll();

    if (store.readOnly) {
      toast(`Read-only: ${esc(store.readOnlyReason || 'no writable repo')}`, 'info');
    }

    $('boot').classList.add('gone');
    setTimeout(() => $('boot').remove(), 600);

    checkDrift(false);
  } catch (err) {
    console.error(err);
    $('boot-msg').innerHTML =
      `<span style="color:#fb7185;max-width:560px;display:block;line-height:1.6">
         Could not start — ${esc(err.message)}</span>`;
  }
})();

// ============================================================ render
let raf = 0;
function renderAll() {
  cancelAnimationFrame(raf);
  raf = requestAnimationFrame(() => {
    refresh('all');
    applyLayerToggles();
    renderStrip();
    renderInspector();
    renderRail();
    renderChrome();
    saveDraft();
  });
}

function renderChrome() {
  const s = store.doc;
  $('n-stations').textContent = s.stations.length;
  $('n-platforms').textContent = s.stations.reduce((n, x) => n + platformsOf(x).length, 0);
  $('n-exits').textContent = s.stations.reduce((n, x) => n + (x.exits || []).length, 0);
  $('n-lines').textContent = s.lines.length;

  const list = changes();
  $('save-count').textContent = list.length;
  $('btn-save').disabled = store.readOnly || list.length === 0;
  $('btn-undo').disabled = store.readOnly || !canUndo();
  $('btn-redo').disabled = store.readOnly || !canRedo();

  document.body.classList.toggle('read-only', store.readOnly);
  $('btn-save').title = store.readOnly ? `Read-only — ${store.readOnlyReason}` : 'Save (⌘S)';

  const { errors, warnings } = store.validation;
  const dot = $('issues-dot'), txt = $('issues-text');
  dot.className = 'dot ' + (errors.length ? 'bad' : warnings.length ? 'warn' : 'live');
  txt.textContent = errors.length
    ? `${errors.length} error${errors.length === 1 ? '' : 's'}`
    : warnings.length ? `${warnings.length} warning${warnings.length === 1 ? '' : 's'}`
    : 'Valid';

  const m = store.meta;
  $('branch-name').textContent = m.readOnly
    ? 'Read-only'
    : (m.git ? (m.branch || 'detached') : 'no git');
  $('branch-chip').querySelector('.dot').className = 'dot ' + (m.readOnly ? 'warn' : 'live');
  $('branch-chip').title = m.readOnly
    ? `Read-only — ${m.readOnlyReason}\nServing ${m.file}`
    : `${m.file} on ${m.branch} — click for history`;

  for (const [k, id] of Object.entries({
    platforms: 'tool-platforms', labels: 'tool-labels',
    transfers: 'tool-transfers', drift: 'tool-drift',
  })) $(id).classList.toggle('on', store.layers[k]);
}

// ============================================================ line rail
function buildRail() {
  const host = $('rail-lines');
  host.innerHTML = '';
  for (const ln of store.doc.lines) {
    const b = document.createElement('button');
    b.className = 'bullet';
    b.dataset.line = ln.id;
    b.style.setProperty('--c', ln.color);
    b.innerHTML = `${esc(ln.shortName || ln.id)}
      <span class="tip">${esc(ln.name)}<em>${ln.stationIds.length} stops</em></span>`;
    b.onclick = () => {
      const next = store.activeLine === ln.id ? '' : ln.id;
      setActiveLine(next);
      if (next) fitLine(next); else fitAll();
    };
    host.appendChild(b);
  }
  $('bullet-all').onclick = () => { setActiveLine(''); fitAll(); };
  $('strip-fit').onclick = () => store.activeLine && fitLine(store.activeLine);
}

function renderRail() {
  const active = store.activeLine;
  $('bullet-all').classList.toggle('on', !active);
  document.querySelectorAll('#rail-lines .bullet').forEach(b => {
    const on = b.dataset.line === active;
    b.classList.toggle('on', on);
    b.classList.toggle('dim', !!active && !on);
    const ln = lineById(b.dataset.line);
    if (ln) b.querySelector('.tip').innerHTML =
      `${esc(ln.name)}<em>${ln.stationIds.length} stops</em>`;
  });
}

// ============================================================ chrome wiring
function wireChrome() {
  $('btn-undo').onclick = () => { const l = undo(); if (l) toast(`Undid “${esc(l)}”`, 'info'); };
  $('btn-redo').onclick = () => { const l = redo(); if (l) toast(`Redid “${esc(l)}”`, 'info'); };
  $('btn-search').onclick = () => openPalette('jump');
  $('btn-save').onclick = openSave;

  $('tool-platforms').onclick = () => toggleLayer('platforms');
  $('tool-labels').onclick = () => toggleLayer('labels');
  $('tool-transfers').onclick = () => toggleLayer('transfers');
  $('tool-drift').onclick = () => {
    toggleLayer('drift');
    if (store.layers.drift && !store.drift) checkDrift(true);
  };
  $('tool-north').onclick = resetNorth;

  $('drift-chip').onclick = () => checkDrift(true);

  $('branch-chip').onclick = async () => {
    if (!store.meta.git) {
      toast(`Read-only — ${esc(store.readOnlyReason || 'no git repository')}`, 'info');
      return;
    }
    const { commits } = await api.history();
    renderHistory(commits);
    showModal('history');
  };
  $('hist-close').onclick = hideModal;

  $('sheet-cancel').onclick = hideModal;
  $('sheet-commit').onclick = () => doSave(true);
  $('sheet-write').onclick = () => doSave(false);

  window.addEventListener('beforeunload', e => {
    if (!store.readOnly && isDirty()) { e.preventDefault(); e.returnValue = ''; }
  });
}

function wireKeys() {
  window.addEventListener('keydown', e => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName);
    const mod = e.metaKey || e.ctrlKey;

    if (mod && e.key.toLowerCase() === 'k') { e.preventDefault(); openPalette('jump'); return; }
    if (mod && e.key.toLowerCase() === 's') {
      e.preventDefault();
      if (store.readOnly) toast(`Read-only — ${esc(store.readOnlyReason || '')}`, 'info');
      else if (!$('btn-save').disabled) openSave();
      return;
    }
    if (mod && e.key.toLowerCase() === 'z') {
      if (typing) return;
      e.preventDefault();
      const l = e.shiftKey ? redo() : undo();
      if (l) toast(`${e.shiftKey ? 'Redid' : 'Undid'} “${esc(l)}”`, 'info');
      return;
    }
    if (e.key === 'Escape') {
      if (isModalOpen()) { hideModal(); return; }
      if (store.selStation) { select(null, null); renderAll(); }
      return;
    }
    if (typing) return;

    if (e.key === 'f' && store.activeLine) fitLine(store.activeLine);
    if (e.key === 'a') { setActiveLine(''); fitAll(); }
    if (e.key === 'p') toggleLayer('platforms');
    if (e.key === 'l') toggleLayer('labels');
    if (e.key === 't') toggleLayer('transfers');
    if (e.key === 'n') resetNorth();

    // step through the active line's stops
    if ((e.key === 'ArrowDown' || e.key === 'ArrowUp') && store.activeLine) {
      const ln = lineById(store.activeLine);
      if (!ln) return;
      e.preventDefault();
      const i = ln.stationIds.indexOf(store.selStation);
      const next = e.key === 'ArrowDown'
        ? Math.min((i < 0 ? -1 : i) + 1, ln.stationIds.length - 1)
        : Math.max((i < 0 ? 1 : i) - 1, 0);
      const sid = ln.stationIds[next];
      select(sid, null); renderAll(); flyToStation(sid);
    }
  });
}

// ============================================================ line editing
function addStationToLine() {
  const ln = lineById(store.activeLine);
  if (!ln) return;
  openPalette('station', sid => {
    if (!sid || ln.stationIds.includes(sid)) {
      if (sid) hint('That station is already on this line');
      return;
    }
    const name = stationById(sid)?.name || sid;
    edit(`Add ${name} to ${ln.id}`, d => {
      const L = d.lines.find(l => l.id === ln.id);
      L.stationIds.push(sid);
      const S = d.stations.find(x => x.id === sid);
      if (S && !(S.lines || []).includes(ln.id)) (S.lines || (S.lines = [])).push(ln.id);
    });
    select(sid, null);
    hint(`${name} added at the end — drag it into position`);
  });
}

// ============================================================ drift
async function checkDrift(force) {
  const dot = $('drift-dot'), txt = $('drift-text');
  txt.textContent = 'Checking…';
  dot.className = 'dot';
  try {
    const d = await api.drift(force);
    store.drift = new Map(d.platforms.map(p => [String(p.platform), p]));
    store.driftAt = d.fetchedAt;
    const bad = d.platforms.filter(p => p.kind !== 'ok');
    dot.className = 'dot ' + (bad.length ? 'warn' : 'live');
    txt.textContent = bad.length
      ? `${bad.length} drifted`
      : `Feed matches`;
    $('drift-chip').title = `${d.feedStops} stops in the feed · checked ${new Date(d.fetchedAt).toLocaleTimeString()}`;
    renderAll();
    if (force) {
      toast(bad.length
        ? `${bad.length} platform${bad.length === 1 ? '' : 's'} differ from the SFMTA feed`
        : 'Every platform matches the SFMTA feed', bad.length ? 'info' : 'ok');
    }
  } catch (err) {
    dot.className = 'dot bad';
    txt.textContent = 'Feed offline';
    if (force) toast(esc(err.message), 'err');
  }
}

// ============================================================ saving
function openSave() {
  renderSheet();
  showModal('sheet');
  setTimeout(() => $('sheet-msg')?.focus(), 40);
}

async function doSave(commit) {
  const msg = $('sheet-msg')?.value.trim() || 'Update data.json from the editor';
  const btn = commit ? $('sheet-commit') : $('sheet-write');
  const was = btn.textContent;
  btn.disabled = true; btn.textContent = commit ? 'Committing…' : 'Writing…';

  try {
    const res = commit
      ? await api.commit(store.doc, msg, store.meta.autoPush)
      : await api.write(store.doc);

    store.base = JSON.parse(JSON.stringify(store.doc));
    store.meta = res.meta || store.meta;
    localStorage.removeItem(DRAFT_KEY);
    hideModal();
    renderAll();

    if (commit) {
      if (res.commit) {
        toast(`Committed ${res.commit.short}${res.pushed ? ' and pushed' : ''}`, 'ok');
        if (res.pushError) toast(`Push failed: ${esc(res.pushError)}`, 'err');
      } else {
        toast('Nothing to commit — the file already matched', 'info');
      }
    } else {
      toast('Written to the working tree, not committed', 'ok');
    }
  } catch (err) {
    const v = err.body?.validation;
    if (v?.errors?.length) {
      toast(`Rejected: ${esc(v.errors[0].path)} — ${esc(v.errors[0].msg)}`, 'err');
    } else {
      toast(esc(err.message), 'err');
    }
  } finally {
    btn.disabled = false; btn.textContent = was;
  }
}

// ============================================================ draft recovery
let draftTimer = 0;
function saveDraft() {
  clearTimeout(draftTimer);
  draftTimer = setTimeout(() => {
    try {
      if (isDirty()) localStorage.setItem(DRAFT_KEY, JSON.stringify({
        at: Date.now(), head: store.meta.head?.sha, doc: store.doc,
      }));
      else localStorage.removeItem(DRAFT_KEY);
    } catch { /* quota or private mode - drafts are a convenience, not a guarantee */ }
  }, 700);
}

function restoreDraft() {
  let saved;
  try { saved = JSON.parse(localStorage.getItem(DRAFT_KEY) || 'null'); } catch { return; }
  if (!saved?.doc) return;

  if (saved.head && store.meta.head?.sha && saved.head !== store.meta.head.sha) {
    localStorage.removeItem(DRAFT_KEY);
    toast('Discarded an old draft — the file has been committed since', 'info');
    return;
  }
  if (JSON.stringify(saved.doc) === JSON.stringify(store.doc)) return;

  const when = new Date(saved.at).toLocaleTimeString();
  toast(`Unsaved edits from ${when} were restored`, 'info', {
    label: 'Discard',
    run: () => {
      localStorage.removeItem(DRAFT_KEY);
      store.doc = JSON.parse(JSON.stringify(store.base));
      store._undo = []; store._redo = [];
      renderAll();
    },
  });
  store.doc = saved.doc;
}
