// Command palette, save sheet, issues, history, toasts - everything that floats.

import {
  store, esc, linesOf, changes, stations, platformsOf, allLines, upstream,
  derivedPlatform,
} from './store.js';

// --------------------------------------------------------------------- toasts
export function toast(msg, kind = 'info', action = null, ms = null) {
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
  setTimeout(dismiss, ms ?? (action ? 7000 : 3600));
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
// Modes: 'jump' (lines and stations), 'station' (pick a station, for a
// transfer) and 'line' (pick a line, for `replaces`).
let palItems = [], palIndex = 0, palPick = null, palMode = 'jump';

const PLACEHOLDER = {
  jump: 'Jump to a station, line, or stop id…',
  station: 'Pick a station to link…',
  line: 'Pick a line…',
};

export function openPalette(mode = 'jump', onPick = null) {
  palPick = onPick;
  palMode = mode;
  const input = document.getElementById('pal-input');
  input.placeholder = PLACEHOLDER[mode] || PLACEHOLDER.jump;
  input.value = '';
  buildPalette('');
  showModal('palette', () => { palPick = null; });
  setTimeout(() => input.focus(), 30);
}

function buildPalette(q) {
  const query = q.trim().toLowerCase();
  const out = [];

  if (palMode !== 'station') {
    for (const ln of allLines()) {
      const hay = `${ln.id} ${ln.name} ${ln.shortName} ${ln.mode}`.toLowerCase();
      if (query && !hay.includes(query)) continue;
      const n = new Set((ln.directions || []).flatMap(d => d.stations)).size;
      out.push({
        kind: 'line', id: ln.id,
        lead: ln.shortName || upstream(ln.id), leadBg: ln.color, leadFg: ln.textColor,
        t1: ln.name, t2: `${ln.mode} · ${n} stations${ln.hidden ? ' · hidden' : ''}`,
      });
    }
  }

  if (palMode !== 'line') {
    for (const [sid, st] of Object.entries(stations())) {
      const ps = platformsOf(st);
      const codes = ps.map(p => p.id).join(' ');
      const names = ps.map(p => `${derivedPlatform(p.id)?.stopName || ''} ${p.name || ''}`).join(' ');
      const hay = `${sid} ${st.name} ${codes} ${names}`.toLowerCase();
      if (query && !hay.includes(query)) continue;
      const ls = linesOf(sid);
      out.push({
        kind: 'station', id: sid,
        lead: ls.length ? (ls[0].shortName || upstream(ls[0].id)) : '·',
        leadBg: ls[0]?.color || 'rgba(255,255,255,.08)', leadFg: ls[0]?.textColor,
        t1: st.name + (st.verified && !store.publicMap ? ' ✓' : ''),
        t2: `${sid} · ${ps.map(p => upstream(p.id)).join(' ')}`,
        lines: ls.slice(0, 8).map(l => l.color),
      });
    }
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
      <div class="lead" style="background:${esc(it.leadBg || 'rgba(255,255,255,.08)')}${it.leadFg ? `;color:${esc(it.leadFg)}` : ''}">${esc(it.lead)}</div>
      <div class="txt">
        <div class="t1">${esc(it.t1)}</div>
        <div class="t2">${esc(it.t2)}</div>
      </div>
      <div class="rline">${(it.lines || []).map(c => `<span style="background:${esc(c)}"></span>`).join('')}</div>
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
  const pick = palPick;
  palPick = null;
  hideModal();
  if (pick) { pick(it.id); return; }
  if (it.kind === 'line') paletteHandlers.onLine(it.id);
  else paletteHandlers.onStation(it.id);
}

export function wirePalette() {
  const input = document.getElementById('pal-input');
  input.addEventListener('input', () => buildPalette(input.value));
  input.addEventListener('keydown', e => {
    if (e.key === 'ArrowDown') { e.preventDefault(); palIndex = Math.min(palIndex + 1, palItems.length - 1); renderPalette(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); palIndex = Math.max(palIndex - 1, 0); renderPalette(); }
    else if (e.key === 'Enter') { e.preventDefault(); choose(palIndex); }
    else if (e.key === 'Escape') { e.preventDefault(); hideModal(); }
  });
}

// ---------------------------------------------------------------- issues
/** Items shown per issue code before "and N more". The real data has a
 *  warning per unassigned 511 stop, which is thousands until the review queue
 *  has been worked through. */
const PER_CODE = 60;

let onIssue = () => {};
export function setIssueHandler(fn) { onIssue = fn; }

/**
 * Issues grouped by `code`, errors first, each group a <details> (errors open).
 * Every row whose issue names a station, platform or line is clickable.
 */
export function issuesHtml(validation, { warnings = true } = {}) {
  if (!validation) return '';
  const groups = new Map();
  const all = [...validation.errors, ...(warnings ? validation.warnings : [])];
  all.forEach((it, i) => {
    const k = `${it.level}|${it.code}`;
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(i);
  });
  issueList = all;
  return [...groups.entries()].map(([k, idx]) => {
    const [level, code] = k.split('|');
    const rows = idx.slice(0, PER_CODE).map(i => {
      const it = all[i];
      const where = it.station || (it.platform && upstream(it.platform)) || (it.line && upstream(it.line)) || '';
      const target = it.station || it.platform || it.line;
      return `<div class="issue ${level === 'error' ? 'error' : 'warn'} ${target ? 'go' : ''}" data-issue="${i}">
        ${where ? `<b>${esc(where)}</b>` : ''}<span>${esc(it.message)}</span></div>`;
    }).join('');
    const more = idx.length > PER_CODE ? `<div class="empty">…and ${idx.length - PER_CODE} more</div>` : '';
    return `<details class="issue-group" ${level === 'error' ? 'open' : ''}>
      <summary><span class="dot ${level === 'error' ? 'bad' : 'warn'}"></span>
        <code>${esc(code)}</code><em>${idx.length}</em></summary>
      ${rows}${more}</details>`;
  }).join('');
}

let issueList = [];
/** Clicks on issue rows inside `host` select what the issue names. */
export function wireIssues(host) {
  host.querySelectorAll('[data-issue]').forEach(el => {
    const it = issueList[Number(el.dataset.issue)];
    if (!it || !(it.station || it.platform || it.line)) return;
    el.onclick = () => { hideModal(); onIssue(it); };
  });
}

export function renderIssues() {
  const v = store.validation;
  const body = document.getElementById('issues-body');
  const n = v ? v.errors.length + v.warnings.length : 0;
  document.getElementById('issues-sub').textContent = !v
    ? 'No validation on this page.'
    : n ? `${v.errors.length} error${v.errors.length === 1 ? '' : 's'} (block a save), ${v.warnings.length} warning${v.warnings.length === 1 ? '' : 's'}`
    : 'Nothing to report.';
  body.innerHTML = issuesHtml(v) || '<div class="empty">No issues.</div>';
  wireIssues(body);
}

// ---------------------------------------------------------------- save sheet
export function renderSheet() {
  const list = changes();
  const v = store.validation || { errors: [], warnings: [] };
  const { errors } = v;

  document.getElementById('sheet-title').textContent =
    `${list.length} change${list.length === 1 ? '' : 's'}`;
  const branch = store.repo?.branch ? ` on ${store.repo.branch}` : '';
  document.getElementById('sheet-sub').textContent = errors.length
    ? 'Fix the errors below before saving.'
    : `Commits to sf-transit${branch}, on top of ${String(store.version || '').slice(0, 7)}.`;

  const issues = errors.length ? issuesHtml({ errors, warnings: [] }, { warnings: false }) : '';
  const body = document.getElementById('sheet-body');
  body.innerHTML = `
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
  wireIssues(body);

  document.getElementById('sheet-commit').disabled = errors.length > 0 || list.length === 0;
}

function suggestMessage(list) {
  if (!list.length) return '';
  const things = new Set(list.map(c => c.t.split(' · ')[0]));
  if (list.length === 1) return `Edit ${list[0].t.split(' · ')[0]}`;
  if (things.size === 1) return `Update ${[...things][0]}`;
  if (things.size <= 3) return `Update ${[...things].join(', ')}`;
  return `Update ${things.size} stations and lines`;
}

// ------------------------------------------------------------------ history
/** `GET api/history` returns commits as `{sha, subject, author, date, url}`;
 *  a bare list or `{commits: [...]}` both work. The GitHub page is the diff. */
export function renderHistory(res) {
  const commits = Array.isArray(res) ? res : res?.commits || [];
  document.getElementById('hist-body').innerHTML = commits.length
    ? commits.map(c => {
      const sha = String(c.sha || c.short || '');
      const subject = String(c.subject || c.message || '').split('\n')[0];
      const link = /^https:\/\//.test(c.url || '') ? c.url : null;
      return `
      <div class="change">
        ${link
          ? `<a class="k edit mono sha" href="${esc(link)}" target="_blank" rel="noopener" title="Open on GitHub">${esc(sha.slice(0, 7))}</a>`
          : `<span class="k edit mono sha">${esc(sha.slice(0, 7))}</span>`}
        <div class="body">
          <div class="t">${link ? `<a href="${esc(link)}" target="_blank" rel="noopener">${esc(subject)}</a>` : esc(subject)}</div>
          <div class="d">${esc(c.author || '')}${c.date ? ` · ${esc(new Date(c.date).toLocaleString())}` : ''}</div>
        </div>
      </div>`;
    }).join('')
    : '<div class="empty">No commits touch the curation yet.</div>';
}
