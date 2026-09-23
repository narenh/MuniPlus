import {
  store, load, adopt, subscribe, undo, redo, canUndo, canRedo, validateNow,
  select, setActiveLine, toggleLayer, stationById, lineById, allLines, stations,
  isDirty, canon, changes, esc, platformsOf, upstream, ownerOf, platformPos,
  knownModes, modeOn, setModeOn, setUnverifiedOnly, verifiedCount, nextUnverified,
  derivedStation, detailText,
} from './store.js';
import { api } from './api.js';
import {
  initMap, refresh, applyLayerToggles, flyToStation, flyTo, fitLine, fitAll,
  resetNorth, onPick, hint, mapCentre, spotlight,
} from './map.js';
import { initStrip, renderStrip } from './strip.js';
import { initVehicles } from './vehicles.js';
import { initInspector, renderInspector, closeAdd, markVerified } from './inspector.js';
import {
  toast, showModal, hideModal, isModalOpen, openPalette, wirePalette,
  setPaletteHandlers, renderSheet, renderHistory, renderIssues, setIssueHandler,
} from './ui.js';

const $ = id => document.getElementById(id);
/** Drafts are keyed by the sf-transit version they started from, so a draft is
 *  only ever re-applied to the curation it was made against. */
const DRAFT_PREFIX = 'muniplus.editor.draft.';
const DRAFTS_KEPT = 3;
/** A draft nobody restored in two weeks was abandoned. */
const DRAFT_MAX_AGE = 14 * 24 * 3600 * 1000;
const RAIL_KEY = 'muniplus.editor.rail.collapsed';
const nf = new Intl.NumberFormat('en-US');

/**
 * The map tool buttons, top to bottom. A layer tool toggles `store.layers[key]`
 * (whose MapLibre layers are listed in map.js's LAYER_IDS); an action tool just
 * runs. The letter is its key. R for the live vehicles ("realtime"), because V
 * already marks a station verified.
 */
const LAYER_TOOLS = [
  { key: 'platforms', letter: 'P', title: 'Show platform poles',
    icon: '<circle cx="9" cy="9" r="2.6" fill="currentColor"/><circle cx="9" cy="9" r="6.4" stroke="currentColor" stroke-width="1.5" opacity=".55"/>' },
  { key: 'labels', letter: 'L', title: 'Show station labels',
    icon: '<path d="M3 5h12M3 9h12M3 13h7" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>' },
  { key: 'transfers', letter: 'T', title: 'Show transfer links',
    icon: '<path d="M4 6h7a3 3 0 010 6H6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-dasharray="1 2.6"/><circle cx="4" cy="6" r="2" fill="currentColor"/><circle cx="6" cy="12" r="2" fill="currentColor"/>' },
  { key: 'vehicles', letter: 'R', title: 'Show live vehicles',
    icon: '<rect x="3.2" y="3" width="11.6" height="10" rx="2.6" stroke="currentColor" stroke-width="1.6"/><path d="M3.6 8.6h10.8" stroke="currentColor" stroke-width="1.4"/><circle cx="6.2" cy="15" r="1.3" fill="currentColor"/><circle cx="11.8" cy="15" r="1.3" fill="currentColor"/>' },
  { action: resetNorth, letter: 'N', title: 'Reset bearing and pitch',
    icon: '<path d="M9 2.4l3.4 9.4L9 9.9l-3.4 1.9L9 2.4z" fill="currentColor"/><path d="M5.6 14.4h6.8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" opacity=".6"/>' },
];

const MODE_LABEL = { metro: 'Metro', streetcar: 'Streetcar', cableway: 'Cableway', bus: 'Bus' };
const modeLabel = m => MODE_LABEL[m] || m.charAt(0).toUpperCase() + m.slice(1);

// ============================================================ shapes
/** The lines' real paths. Not awaited: the map draws through the platforms
 *  first, and a failure here leaves it that way rather than blocking boot. */
async function loadShapes() {
  try {
    store.shapes = (await api.shapes()).shapes;
    refresh('lines');
  } catch (err) {
    console.warn('No line shapes; drawing through platforms instead.', err);
  }
}

// ============================================================ boot
(async function boot() {
  try {
    $('boot-msg').textContent = 'Reading the network…';
    const state = await api.state();
    load(state);
    if (store.publicMap) dressAsMap();

    $('boot-msg').textContent = 'Drawing the network…';
    await initMap('map');

    loadShapes();
    buildRail();
    buildFilters();
    buildTools();
    initVehicles();
    initStrip({
      onPick: () => renderAll(),
      onLineDetails: () => { store.lineInspector = true; select(null, null); },
    });
    initInspector({
      onNeedStationPicker: cb => openPalette('station', cb),
      onNeedLinePicker: cb => openPalette('line', cb),
    });
    wirePalette();
    wireChrome();
    wireKeys();

    setPaletteHandlers({
      onLine: id => { setActiveLine(id); fitLine(id); },
      onStation: id => { select(id, null); flyToStation(id); },
    });
    setIssueHandler(goToIssue);

    onPick(() => renderAll());
    subscribe(what => {
      if (what === 'selection' || what === 'line') spotlight(null);
      if (what !== 'render') renderAll();
    });

    store.onBlocked = () => toast(`Read-only — ${esc(store.readOnlyReason || 'edits cannot be saved here')}`, 'info');

    if (!store.readOnly) restoreDraft();
    renderAll();
    fitAll();

    if (store.readOnly && store.validation) {
      // The public /map is read-only by design and needs no announcement; an
      // editor that cannot write does.
      toast(`Read-only: ${esc(store.readOnlyReason || 'edits cannot be saved')}`, 'info');
    }

    $('boot').classList.add('gone');
    setTimeout(() => $('boot').remove(), 600);
  } catch (err) {
    console.error(err);
    $('boot-msg').innerHTML =
      `<span style="color:#fb7185;max-width:560px;display:block;line-height:1.6">
         Could not start — ${esc(err.message)}</span>`;
  }
})();

/**
 * /map/ is the same page with the editing taken out. Read-only already hides
 * every control that edits; this also hides everything that is only about
 * curating (history, counts, validation, verification), and the "Read-only"
 * chip, because here being read-only is the point rather than a warning.
 */
function dressAsMap() {
  document.documentElement.classList.add('public-map');
  document.title = 'Muni+ Map';
  $('wordmark').textContent = 'Muni+ Map';
}

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
    renderFilters();
    renderTools();
    renderChrome();
    saveDraft();
  });
}

function renderChrome() {
  document.body.classList.toggle('read-only', store.readOnly);

  const all = Object.values(stations());
  $('n-stations').textContent = nf.format(all.length);
  $('n-platforms').textContent = nf.format(all.reduce((n, x) => n + platformsOf(x).length, 0));
  $('n-lines').textContent = nf.format(allLines().length);
  const { n, of } = verifiedCount();
  $('n-verified').textContent = `${nf.format(n)} / ${nf.format(of)}`;
  $('stat-verified').title = `${of ? Math.round(100 * n / of) : 0}% of stations verified`;
  $('stat-verified').style.setProperty('--p', of ? `${(100 * n / of).toFixed(1)}%` : '0%');

  const list = changes();
  $('save-count').textContent = list.length;
  $('btn-save').disabled = store.readOnly || list.length === 0;
  $('btn-undo').disabled = store.readOnly || !canUndo();
  $('btn-redo').disabled = store.readOnly || !canRedo();
  $('btn-save').title = 'Save (⌘S)';

  // Issues. Null validation is /map, which has none to show.
  const v = store.validation;
  $('issues-chip').hidden = !v;
  if (v) {
    const dot = $('issues-dot'), txt = $('issues-text');
    const { errors, warnings } = v;
    dot.className = 'dot ' + (store.checkError || errors.length ? 'bad' : warnings.length ? 'warn' : 'live')
      + (store.checking ? ' pending' : '');
    txt.textContent = store.checkError ? 'Check failed'
      : errors.length ? `${errors.length} error${errors.length === 1 ? '' : 's'}`
      : warnings.length ? `${nf.format(warnings.length)} warning${warnings.length === 1 ? '' : 's'}`
      : 'Valid';
    $('issues-chip').title = store.checkError
      ? `The server could not check this edit: ${store.checkError}`
      : store.checking ? 'Checking…' : 'Validation issues';
  }

  // Branch.
  const chip = $('branch-chip'), r = store.repo;
  const drift = r ? [r.ahead ? `↑${r.ahead}` : '', r.behind ? `↓${r.behind}` : ''].filter(Boolean).join(' ') : '';
  if (store.readOnly) {
    $('branch-name').textContent = 'Read-only';
    chip.title = store.readOnlyReason || 'Read-only';
  } else if (r) {
    $('branch-name').textContent = r.branch + (drift ? ` ${drift}` : '');
    chip.title = `sf-transit ${r.branch} at ${String(r.head).slice(0, 7)}`
      + (r.ahead ? `, ${r.ahead} commit${r.ahead === 1 ? '' : 's'} not pushed` : '')
      + (r.behind ? `, ${r.behind} behind` : '')
      + (r.dirty ? ', uncommitted files in the checkout' : '')
      + ' — click for history';
  } else {
    $('branch-name').textContent = 'no repo';
    chip.title = 'No repository status';
  }
  chip.querySelector('.dot').className = 'dot ' + (store.readOnly ? 'warn' : r?.behind || r?.dirty ? 'warn' : 'live');
}

// ============================================================ line rail
// Grouped by mode, in the server's line order. The bus group is most of the
// ~69 lines, so it is a compact two-column grid and starts collapsed; a
// collapsed group still shows its focused line.
let collapsed = null;

function loadCollapsed() {
  try { collapsed = new Set(JSON.parse(localStorage.getItem(RAIL_KEY) || 'null') || ['bus']); }
  catch { collapsed = new Set(['bus']); }
}

function buildRail() {
  loadCollapsed();
  const host = $('rail-lines');
  host.innerHTML = '';
  const groups = new Map();
  for (const ln of allLines()) {
    if (!groups.has(ln.mode)) groups.set(ln.mode, []);
    groups.get(ln.mode).push(ln);
  }
  for (const [mode, lines] of groups) {
    const g = document.createElement('div');
    g.className = 'rail-group' + (lines.length > 12 ? ' compact' : '');
    g.dataset.mode = mode;
    const head = document.createElement('button');
    head.className = 'rail-head';
    head.innerHTML = `<span>${esc(modeLabel(mode))}</span><em>${lines.length}</em>`;
    head.onclick = () => {
      if (collapsed.has(mode)) collapsed.delete(mode); else collapsed.add(mode);
      try { localStorage.setItem(RAIL_KEY, JSON.stringify([...collapsed])); } catch { /* a convenience */ }
      renderRail();
    };
    g.appendChild(head);
    const items = document.createElement('div');
    items.className = 'rail-items';
    for (const ln of lines) items.appendChild(bullet(ln));
    g.appendChild(items);
    host.appendChild(g);
  }
  $('bullet-all').onclick = () => { setActiveLine(''); fitAll(); };
  $('strip-fit').onclick = () => store.activeLine && fitLine(store.activeLine);
}

function bullet(ln) {
  const b = document.createElement('button');
  const label = ln.shortName || upstream(ln.id);
  b.className = `bullet len${Math.min(label.length, 4)}`;
  b.dataset.line = ln.id;
  b.textContent = label;
  b.onclick = () => {
    const next = store.activeLine === ln.id ? '' : ln.id;
    setActiveLine(next);
    if (next) fitLine(next); else fitAll();
  };
  // One tooltip element for the whole rail: a tip inside the rail would be
  // clipped by its scrolling.
  b.onmouseenter = () => {
    const l = lineById(ln.id);
    if (!l) return;
    const n = new Set((l.directions || []).flatMap(d => d.stations)).size;
    const tip = $('rail-tip');
    tip.innerHTML = `${esc(l.name)}<em>${n} stations${l.hidden ? ' · hidden' : ''}</em>`;
    const r = b.getBoundingClientRect();
    tip.style.top = `${r.top + r.height / 2}px`;
    tip.style.left = `${r.right + 12}px`;
    tip.classList.add('show');
  };
  b.onmouseleave = () => $('rail-tip').classList.remove('show');
  return b;
}

function renderRail() {
  const active = store.activeLine;
  $('bullet-all').classList.toggle('on', !active);
  document.querySelectorAll('#rail-lines .rail-group').forEach(g => {
    const mode = g.dataset.mode;
    const shut = collapsed.has(mode);
    g.hidden = !modeOn(mode) && !g.querySelector(`[data-line="${CSS.escape(active)}"]`);
    g.classList.toggle('collapsed', shut);
  });
  document.querySelectorAll('#rail-lines .bullet').forEach(b => {
    const ln = lineById(b.dataset.line);
    if (!ln) { b.hidden = true; return; }
    const on = b.dataset.line === active;
    b.hidden = b.closest('.rail-group').classList.contains('collapsed') && !on;
    b.classList.toggle('on', on);
    b.classList.toggle('dim', !!active && !on);
    b.classList.toggle('hidden-line', !!ln.hidden);
    b.style.setProperty('--c', ln.color || '#7c8598');
    if (ln.textColor) b.style.setProperty('--fg', ln.textColor); else b.style.removeProperty('--fg');
  });
}

// ============================================================ filters
function buildFilters() {
  const host = $('filters');
  host.innerHTML = knownModes().map(m =>
    `<button class="fchip" data-mode="${esc(m)}" title="Show ${esc(modeLabel(m).toLowerCase())} stations and lines">
      ${esc(modeLabel(m))}<em></em></button>`).join('')
    + (store.publicMap ? '' : `<span class="fsep"></span>
       <button class="fchip" id="f-unverified" title="Only stations nobody has verified (U jumps to the nearest)">Unverified only</button>`);
  host.querySelectorAll('[data-mode]').forEach(b => {
    b.onclick = e => {
      const m = b.dataset.mode;
      // Alt-click shows this mode alone, the quick way to work one mode through.
      if (e.altKey) { for (const k of knownModes()) setModeOn(k, k === m); return; }
      setModeOn(m, !modeOn(m));
    };
  });
  const unverified = $('f-unverified');
  if (unverified) unverified.onclick = () => setUnverifiedOnly(!store.unverifiedOnly);
}

function renderFilters() {
  const count = new Map();
  for (const sid of Object.keys(stations())) {
    for (const m of derivedStation(sid)?.modes || []) count.set(m, (count.get(m) || 0) + 1);
  }
  document.querySelectorAll('#filters [data-mode]').forEach(b => {
    b.classList.toggle('on', modeOn(b.dataset.mode));
    b.querySelector('em').textContent = nf.format(count.get(b.dataset.mode) || 0);
  });
  $('f-unverified')?.classList.toggle('on', store.unverifiedOnly);
}

function buildTools() {
  const host = $('map-tools');
  host.innerHTML = '';
  for (const t of LAYER_TOOLS) {
    const b = document.createElement('button');
    b.className = 'tool';
    b.title = `${t.title} (${t.letter})`;
    b.innerHTML = `<svg width="16" height="16" viewBox="0 0 18 18" fill="none">${t.icon}</svg>`;
    if (t.key) b.dataset.layer = t.key;
    b.onclick = () => (t.key ? toggleLayer(t.key) : t.action());
    host.appendChild(b);
  }
}

function renderTools() {
  document.querySelectorAll('#map-tools [data-layer]').forEach(b =>
    b.classList.toggle('on', !!store.layers[b.dataset.layer]));
}

// ============================================================ chrome wiring
function wireChrome() {
  $('btn-undo').onclick = () => { const l = undo(); if (l) toast(`Undid “${esc(l)}”`, 'info'); };
  $('btn-redo').onclick = () => { const l = redo(); if (l) toast(`Redid “${esc(l)}”`, 'info'); };
  $('btn-search').onclick = () => openPalette('jump');
  $('btn-save').onclick = openSave;
  $('issues-chip').onclick = () => { renderIssues(); showModal('issues'); };
  $('issues-close').onclick = hideModal;

  $('branch-chip').onclick = async () => {
    if (!store.repo) {
      toast(esc(store.readOnlyReason || 'No repository history here'), 'info');
      return;
    }
    try {
      renderHistory(await api.history());
      showModal('history');
    } catch (err) {
      toast(`Could not read the history: ${esc(err.message)}`, 'err');
    }
  };
  $('hist-close').onclick = hideModal;

  $('sheet-cancel').onclick = hideModal;
  $('sheet-commit').onclick = doSave;

  window.addEventListener('beforeunload', e => {
    if (!store.readOnly && isDirty()) { flushDraft(); e.preventDefault(); e.returnValue = ''; }
  });
}

function wireKeys() {
  window.addEventListener('keydown', e => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName);
    const mod = e.metaKey || e.ctrlKey;

    if (mod && e.key.toLowerCase() === 'k') { e.preventDefault(); openPalette('jump'); return; }
    if (mod && e.key.toLowerCase() === 's') {
      e.preventDefault();
      if (!store.readOnly && !$('btn-save').disabled) openSave();
      return;
    }
    if (mod && e.key.toLowerCase() === 'z') {
      if (typing || store.readOnly) return;
      e.preventDefault();
      const l = e.shiftKey ? redo() : undo();
      if (l) toast(`${e.shiftKey ? 'Redid' : 'Undid'} “${esc(l)}”`, 'info');
      return;
    }
    if (e.key === 'Escape') {
      if (isModalOpen()) { hideModal(); return; }
      if (closeAdd()) { renderAll(); return; }
      if (store.selStation) { select(null, null); return; }
      if (store.lineInspector) { store.lineInspector = false; renderAll(); }
      return;
    }
    if (typing || mod || e.altKey || isModalOpen()) return;

    const k = e.key.toLowerCase();
    if (k === 'f' && store.activeLine) fitLine(store.activeLine);
    if (k === 'a') { setActiveLine(''); fitAll(); }
    if (k === 'p') toggleLayer('platforms');
    if (k === 'l') toggleLayer('labels');
    if (k === 't') toggleLayer('transfers');
    if (k === 'r') toggleLayer('vehicles');
    if (k === 'n') resetNorth();
    if (k === 'v' && store.selStation && !store.readOnly) markVerified(store.selStation);
    if (k === 'u' && !store.publicMap) jumpToNextUnverified();
    if (k === 'd' && store.activeLine) {
      const n = lineById(store.activeLine)?.directions?.length || 0;
      if (n > 1) { store.activeDir = (store.activeDir + 1) % n; renderAll(); }
    }

    // walk the focused line's highlighted direction
    if ((e.key === 'ArrowDown' || e.key === 'ArrowUp') && store.activeLine) {
      const dir = lineById(store.activeLine)?.directions?.[store.activeDir];
      if (!dir?.stations.length) return;
      e.preventDefault();
      const seq = dir.stations;
      const i = seq.indexOf(store.selStation);
      const next = e.key === 'ArrowDown'
        ? Math.min((i < 0 ? -1 : i) + 1, seq.length - 1)
        : Math.max((i < 0 ? seq.length : i) - 1, 0);
      const sid = seq[next];
      select(sid, null); flyToStation(sid);
    }
  });
}

function jumpToNextUnverified() {
  const at = mapCentre();
  if (!at) return;
  const sid = nextUnverified(at, store.selStation);
  if (!sid) { hint('No unverified station matches the filters'); return; }
  select(sid, null);
  flyToStation(sid);
}

/** Select whatever an issue names: its station (and platform), else the
 *  platform where it is, else the line. */
function goToIssue(it) {
  if (it.station && stationById(it.station)) {
    select(it.station, it.platform || null);
    const at = it.platform && platformPos(it.platform);
    if (at) flyTo(at, 17); else flyToStation(it.station);
    return;
  }
  if (it.platform) {
    const sid = ownerOf(it.platform);
    const at = platformPos(it.platform);
    select(sid, sid ? it.platform : null);
    if (at) flyTo(at, 17);
    if (!sid) {
      spotlight(it.platform);
      const name = at ? store.derived.platforms[it.platform]?.stopName : null;
      hint(`${upstream(it.platform)}${name ? ` (${name})` : ''} is in no station${at ? '' : ', and 511 has no coordinate for it'}`, 4000);
    }
    return;
  }
  if (it.line) {
    if (!lineById(it.line)) { hint(`511 does not list ${it.line}`); return; }
    setActiveLine(it.line);
    store.lineInspector = true;
    select(null, null);
    fitLine(it.line);
  }
}

// ============================================================ saving
async function openSave() {
  if (store.readOnly) return;
  // The sheet must describe the curation as it is now, not as of the last
  // answer from the server.
  await validateNow();
  renderSheet();
  showModal('sheet');
  setTimeout(() => $('sheet-msg')?.focus(), 40);
}

let saving = false;
async function doSave() {
  if (saving || store.readOnly) return;
  const msg = $('sheet-msg')?.value.trim() || 'Edit the curation';
  const btn = $('sheet-commit');
  const was = btn.textContent;
  saving = true;
  btn.disabled = true; btn.textContent = 'Saving…';

  const sent = JSON.parse(JSON.stringify(store.curation));
  const from = store.version;
  try {
    const res = await api.save(sent, msg, from);
    // Edits made while the save was in flight stay unsaved against what was sent.
    store.base = sent;
    store.version = res.version;
    if (res.validation) store.validation = res.validation;
    removeDraft(from);
    hideModal();
    renderAll();

    if (res.commit) {
      toast(`Committed ${esc(String(res.commit).slice(0, 7))}${res.pushed ? ' and pushed' : ', not pushed'}`, res.pushed ? 'ok' : 'info');
      if (res.pushError) toast(`Push failed: ${esc(res.pushError)}`, 'err', null, 9000);
    } else {
      toast('Nothing to commit — sf-transit already matched', 'info');
    }
    refreshRepo();
  } catch (err) {
    if (err.status === 409) {
      flushDraft();
      hideModal();
      toast(`Someone else changed the data since you loaded it, and your edits do not apply on top.
        ${err.body?.message ? `<br><small>${esc(err.body.message)}</small>` : ''}
        <br>Your edits are kept in this browser.`, 'err',
        { label: 'Reload', run: () => { flushDraft(); location.reload(); } }, 30000);
    } else if (err.status === 422) {
      if (err.body?.validation?.errors?.length) {
        store.validation = err.body.validation;
        renderSheet();
        toast('The server refused the save: fix the errors listed.', 'err');
      } else {
        toast(`The server refused the save: ${esc(err.body?.detail ? detailText(err.body.detail) : err.message)}`, 'err', null, 9000);
      }
    } else {
      toast(`Save failed: ${esc(err.message)}`, 'err', null, 9000);
    }
  } finally {
    saving = false;
    btn.textContent = was;
    btn.disabled = changes().length === 0 || (store.validation?.errors.length ?? 0) > 0;
  }
}

/** ahead/behind change with a save, and SaveResponse does not carry them. */
async function refreshRepo() {
  try {
    const s = await api.state();
    if (s.version === store.version) { store.repo = s.repo; renderChrome(); }
  } catch { /* the chip is informational */ }
}

// ============================================================ draft recovery
const draftKey = v => DRAFT_PREFIX + v;

function readDrafts() {
  const out = [];
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (!k?.startsWith(DRAFT_PREFIX)) continue;
      let d = null;
      try { d = JSON.parse(localStorage.getItem(k)); } catch { /* unreadable */ }
      // Anything else under the prefix is a draft from the editor before it moved
      // to sf-transit, whose shape this one cannot apply.
      if (d?.curation?.stations?.stations && d.base && d.version) out.push({ key: k, ...d });
      else out.push({ key: k, stale: true });
    }
  } catch { /* storage unavailable */ }
  return out;
}

function removeDraft(version) {
  try { localStorage.removeItem(draftKey(version)); } catch { /* ignore */ }
}

function writeDraft() {
  if (store.readOnly || !store.curation) return;
  try {
    if (!isDirty()) { removeDraft(store.version); return; }
    const put = () => localStorage.setItem(draftKey(store.version), JSON.stringify({
      at: Date.now(), version: store.version, base: store.base, curation: store.curation,
    }));
    // Keep the newest few, so a draft left behind by a 409 survives a reload
    // without the full curation piling up in storage.
    const others = readDrafts().filter(d => d.key !== draftKey(store.version))
      .sort((a, b) => (b.at || 0) - (a.at || 0));
    for (const d of others.slice(DRAFTS_KEPT - 1)) localStorage.removeItem(d.key);
    try { put(); } catch {
      for (const d of others) localStorage.removeItem(d.key);
      put();
    }
  } catch { /* quota or private mode - drafts are a convenience, not a guarantee */ }
}

let draftTimer = 0;
function saveDraft() {
  clearTimeout(draftTimer);
  draftTimer = setTimeout(writeDraft, 700);
}
function flushDraft() { clearTimeout(draftTimer); writeDraft(); }

function restoreDraft() {
  const drafts = readDrafts();
  const expired = d => d.stale || Date.now() - (d.at || 0) > DRAFT_MAX_AGE;
  for (const d of drafts.filter(expired)) { try { localStorage.removeItem(d.key); } catch { /* ignore */ } }
  const usable = drafts.filter(d => !expired(d)).sort((a, b) => (b.at || 0) - (a.at || 0));

  const mine = usable.find(d => d.version === store.version);
  if (mine && canon(mine.curation) !== canon(store.curation)) {
    const when = new Date(mine.at).toLocaleTimeString();
    const base = JSON.parse(JSON.stringify(store.base));
    adopt({ curation: mine.curation });
    toast(`Unsaved edits from ${when} were restored`, 'info', {
      label: 'Discard',
      run: () => { removeDraft(mine.version); adopt({ curation: base, base }); },
    });
    return;
  }

  // A draft made against an older version (a save refused with 409, or edits
  // left open while someone else saved) is not applied by itself. Restoring it
  // keeps its own base and version, so the change list shows only its edits and
  // the save lets the server rebase them, or refuse again.
  const old = usable.find(d => d.version !== store.version);
  if (old) {
    const when = new Date(old.at).toLocaleString();
    const latest = { curation: store.curation, base: store.base, version: store.version };
    toast(`Unsaved edits from ${when}, made against an older version (${esc(String(old.version).slice(0, 7))}), are kept in this browser.`,
      'info', {
        label: 'Restore',
        run: () => {
          adopt({ curation: old.curation, base: old.base, version: old.version });
          // If the server cannot rebase them, this is the way back to the latest.
          toast('Restored. If saving them is refused again, discard them and redo the edits.', 'info', {
            label: 'Discard',
            run: () => { removeDraft(old.version); adopt(JSON.parse(JSON.stringify(latest))); },
          }, 15000);
        },
      }, 20000);
  }
}
