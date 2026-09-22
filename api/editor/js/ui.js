// Command palette, save sheet, history, toasts - everything that floats.

import { store, esc, linesOf, changes, stationById, platformsOf } from './store.js';

// --------------------------------------------------------------------- toasts
export function toast(msg, kind = 'info', action = null) {
  const host = document.getElementById('toasts');
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.innerHTML = `<span class="ic"></span><span>${msg}</span>`;
  if (action) {
    const b = document.createElement('button');
    b.textContent = action.label;
    b.onclick = () => { action.run(); dismiss(); };
    el.appendChild(b);
  }
  host.appendChild(el);
  const dismiss = () => {
    if (!el.isConnected) return;
    el.classList.add('out');
    setTimeout(() => el.remove(), 260);
  };
  setTimeout(dismiss, action ? 7000 : 3600);
  return dismiss;
}

// ---------------------------------------------------------------- modal plumbing
const scrim = () => document.getElementById('scrim');
let openModal = null;

export function showModal(id, onClose) {
  if (openModal) hideModal();
  const el = document.getElementById(id);
  el.classList.add('show');
  scrim().classList.add('show');
  openModal = { el, onClose };
}

export function hideModal() {
  if (!openModal) return;
  openModal.el.classList.remove('show');
  scrim().classList.remove('show');
  openModal.onClose?.();
  openModal = null;
}

export const isModalOpen = () => !!openModal;

scrim().addEventListener('click', hideModal);

// ------------------------------------------------------------------- palette
let palItems = [], palIndex = 0, palPick = null;

export function openPalette(mode = 'jump', onPick = null) {
  palPick = onPick;
  const input = document.getElementById('pal-input');
  input.placeholder = mode === 'station'
    ? 'Pick a station to link…'
    : 'Jump to a station, line, or stop code…';
  input.value = '';
  buildPalette('', mode);
  showModal('palette', () => { palPick = null; });
  setTimeout(() => input.focus(), 30);
}

function buildPalette(q, mode) {
  const query = q.trim().toLowerCase();
  const out = [];

  if (mode !== 'station') {
    for (const ln of store.doc.lines) {
      const hay = `${ln.id} ${ln.name} ${ln.shortName}`.toLowerCase();
      if (!query || hay.includes(query)) {
        out.push({
          kind: 'line', id: ln.id,
          lead: ln.shortName || ln.id, leadBg: ln.color,
          t1: ln.name, t2: `${ln.stationIds.length} stations`,
        });
      }
    }
  }

  for (const st of store.doc.stations) {
    const ps = platformsOf(st);
    const codes = ps.map(p => String(p.id)).join(' ');
    const names = ps.map(p => `${p.stopName || ''} ${p.name || ''}`).join(' ');
    const hay = `${st.id} ${st.name} ${codes} ${names}`.toLowerCase();
    if (query && !hay.includes(query)) continue;
    const ls = linesOf(st.id);
    out.push({
      kind: 'station', id: st.id,
      lead: ls.length ? (ls[0].shortName || ls[0].id) : '·',
      leadBg: ls[0]?.color || 'rgba(255,255,255,.08)',
      t1: st.name, t2: `${st.id} · ${ps.map(p => p.id).join(' ')}`,
      lines: ls.map(l => l.color),
    });
  }

  palItems = out.slice(0, 120);
  palIndex = 0;
  renderPalette();
  document.getElementById('pal-count').textContent =
    `${out.length} result${out.length === 1 ? '' : 's'}`;
}

function renderPalette() {
  const list = document.getElementById('pal-list');
  list.innerHTML = palItems.map((it, i) => `
    <div class="pal-item ${i === palIndex ? 'on' : ''}" data-i="${i}">
      <div class="lead" style="background:${it.leadBg}">${esc(it.lead)}</div>
      <div class="txt">
        <div class="t1">${esc(it.t1)}</div>
        <div class="t2">${esc(it.t2)}</div>
      </div>
      <div class="rline">${(it.lines || []).map(c => `<span style="background:${c}"></span>`).join('')}</div>
    </div>`).join('') || '<div class="empty" style="padding:14px">Nothing matches.</div>';

  list.querySelectorAll('.pal-item').forEach(el => {
    el.onmouseenter = () => { palIndex = Number(el.dataset.i); highlight(); };
    el.onclick = () => choose(Number(el.dataset.i));
  });
  list.querySelector('.pal-item.on')?.scrollIntoView({ block: 'nearest' });
}

function highlight() {
  document.querySelectorAll('#pal-list .pal-item').forEach((el, i) =>
    el.classList.toggle('on', i === palIndex));
}

let paletteHandlers = { onLine: () => {}, onStation: () => {} };
export function setPaletteHandlers(h) { paletteHandlers = { ...paletteHandlers, ...h }; }

function choose(i) {
  const it = palItems[i];
  if (!it) return;
  hideModal();
  if (palPick) { palPick(it.id); palPick = null; return; }
  if (it.kind === 'line') paletteHandlers.onLine(it.id);
  else paletteHandlers.onStation(it.id);
}

export function wirePalette() {
  const input = document.getElementById('pal-input');
  input.addEventListener('input', () =>
    buildPalette(input.value, palPick ? 'station' : 'jump'));
  input.addEventListener('keydown', e => {
    if (e.key === 'ArrowDown') { e.preventDefault(); palIndex = Math.min(palIndex + 1, palItems.length - 1); renderPalette(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); palIndex = Math.max(palIndex - 1, 0); renderPalette(); }
    else if (e.key === 'Enter') { e.preventDefault(); choose(palIndex); }
    else if (e.key === 'Escape') { e.preventDefault(); hideModal(); }
  });
}

// ---------------------------------------------------------------- save sheet
export function renderSheet() {
  const list = changes();
  const { errors, warnings } = store.validation;

  document.getElementById('sheet-title').textContent =
    `${list.length} change${list.length === 1 ? '' : 's'}`;
  document.getElementById('sheet-sub').textContent = errors.length
    ? 'Fix the errors below before committing.'
    : `Writing to ${store.meta.file || 'appdata/data.json'} on ${store.meta.branch || 'this branch'}.`;

  const issues = [
    ...errors.map(e => `<div class="issue error"><b>${esc(e.path)}</b><span>${esc(e.msg)}</span></div>`),
    ...warnings.map(w => `<div class="issue warn"><b>${esc(w.path)}</b><span>${esc(w.msg)}</span></div>`),
  ].join('');

  document.getElementById('sheet-body').innerHTML = `
    ${issues ? `<div style="margin-bottom:14px">${issues}</div>` : ''}
    ${list.length ? list.map(c => `
      <div class="change">
        <span class="k ${c.k}">${c.k}</span>
        <div class="body"><div class="t">${esc(c.t)}</div><div class="d">${c.d}</div></div>
      </div>`).join('') : '<div class="empty">Nothing has changed yet.</div>'}
    <div class="field" style="margin-top:16px">
      <label class="micro">Commit message</label>
      <textarea class="inp" id="sheet-msg" spellcheck="true">${esc(suggestMessage(list))}</textarea>
    </div>`;

  document.getElementById('sheet-commit').disabled = errors.length > 0 || list.length === 0;
  document.getElementById('sheet-write').disabled = errors.length > 0 || list.length === 0;
}

function suggestMessage(list) {
  if (!list.length) return '';
  const stations = new Set(list.map(c => c.t.split(' · ')[0]));
  if (list.length === 1) return `Edit ${list[0].t.split(' · ')[0]} in data.json`;
  if (stations.size === 1) return `Update ${[...stations][0]} in data.json`;
  return `Update ${stations.size} stations in data.json`;
}

// ------------------------------------------------------------------ history
export function renderHistory(commits) {
  document.getElementById('hist-body').innerHTML = commits.length
    ? commits.map(c => `
      <div class="change">
        <span class="k edit mono" style="min-width:66px">${esc(c.short)}</span>
        <div class="body">
          <div class="t">${esc(c.subject)}</div>
          <div class="d">${esc(c.author)} · ${new Date(c.date).toLocaleString()}</div>
        </div>
      </div>`).join('')
    : '<div class="empty">No commits touch this file yet.</div>';
}
