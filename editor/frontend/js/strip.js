// The line strip: a vertical diagram of the focused line, one list per
// direction, each headed by its headsign. It is read-only. What it shows is the
// server's `derived.lines[id].directions`, the most-run 511 pattern per
// direction (PLAN.md, "line diagrams"), so stop order is 511's to say and there
// is nothing here to reorder.

import {
  store, select, stationById, linesOf, lineById, esc, upstream, ownerOf, transfersOf, inkOn, badgeShape,
} from './store.js';
import { flyToStation } from './map.js';

let onPickStation = () => {};
let onDetails = () => {};

/** Other lines shown as pips on a row. A downtown stop can have twenty. */
const MAX_PIPS = 6;

export function initStrip({ onPick, onLineDetails }) {
  onPickStation = onPick || (() => {});
  onDetails = onLineDetails || (() => {});
  document.getElementById('strip-details').onclick = () => onDetails();
}

/**
 * A direction's stops, grouped into consecutive runs owned by the same
 * station: `[{ sid, pids }]`. The server's `stations` list drops the same
 * repeats, so this is that list with the stops each station contributed.
 */
export function runsOf(dir) {
  const out = [];
  for (const pid of dir.stops) {
    const sid = ownerOf(pid);
    const last = out[out.length - 1];
    if (last && last.sid === sid) last.pids.push(pid);
    else out.push({ sid, pids: [pid] });
  }
  return out;
}

export function renderStrip() {
  const strip = document.getElementById('strip');
  const ln = lineById(store.activeLine);

  if (!ln) { strip.classList.add('closed'); return; }
  strip.classList.remove('closed');
  const color = ln.color || '#6ea8fe';
  strip.style.setProperty('--c', color);

  const badge = document.getElementById('strip-badge');
  badge.textContent = ln.shortName || upstream(ln.id);
  badge.style.setProperty('--c', color);
  badge.style.color = inkOn(color);
  badge.classList.remove('circle', 'pill');
  badge.classList.add(badgeShape(ln));
  document.getElementById('strip-name').textContent = ln.name;

  const dirs = ln.directions || [];
  const nst = new Set(dirs.flatMap(d => d.stations)).size;
  document.getElementById('strip-sub').textContent = [
    ln.mode,
    `${nst} station${nst === 1 ? '' : 's'}`,
    ln.hidden ? 'hidden' : '',
    ln.replaces?.length ? `replaces ${ln.replaces.map(upstream).join(', ')}` : '',
  ].filter(Boolean).join(' · ');

  const list = document.getElementById('strip-list');
  list.innerHTML = '';

  if (!dirs.length) {
    list.innerHTML = '<div class="empty" style="padding:14px 16px">511 lists no trips for this line, or none of its stops belongs to a station.</div>';
    return;
  }
  if (store.activeDir >= dirs.length) store.activeDir = 0;

  dirs.forEach((dir, di) => {
    const head = document.createElement('div');
    head.className = 'dir-head' + (di === store.activeDir ? ' on' : '');
    const stops = runsOf(dir);
    head.innerHTML = `
      <span class="dir-arrow">→</span>
      <span class="dir-sign">${esc(dir.headsign || `Direction ${dir.direction}`)}</span>
      <span class="dir-count">${stops.length}</span>`;
    head.title = `Direction ${dir.direction}. ↑/↓ walk the highlighted direction.`;
    head.onclick = () => { store.activeDir = di; renderStrip(); };
    list.appendChild(head);

    const box = document.createElement('div');
    box.className = 'dir-list';
    stops.forEach(({ sid, pids }) => box.appendChild(stopRow(ln, di, sid, pids)));
    list.appendChild(box);
  });

  list.querySelector('.dir-head.on + .dir-list .stop.sel')?.scrollIntoView({ block: 'nearest' });
}

function stopRow(ln, di, sid, pids) {
  const st = stationById(sid);
  const row = document.createElement('div');
  row.className = 'stop';
  row.dataset.sid = sid || '';
  if (sid && store.selStation === sid) row.classList.add('sel');

  const codes = pids.map(p => `<span class="tag mono">${esc(upstream(p))}</span>`).join('');
  if (!st) {
    // Only possible between an edit and the validate answer that redraws this.
    row.innerHTML = `<div class="node"><i></i></div>
      <div class="stop-main"><div class="stop-name" style="color:var(--ink-faint)">No station</div>
      <div class="stop-meta">${codes}</div></div>`;
    return row;
  }

  const others = linesOf(sid).filter(l => l.id !== ln.id);
  const nodeCls = ['node', others.length ? 'interchange' : ''].filter(Boolean).join(' ');
  const pips = others.slice(0, MAX_PIPS).map(l =>
    `<span class="pip" style="background:${esc(l.color || '#7c8598')}" title="${esc(l.name)}"></span>`).join('');
  const more = others.length > MAX_PIPS ? `<span class="tag">+${others.length - MAX_PIPS}</span>` : '';

  row.innerHTML = `
    <div class="${nodeCls}"><i></i></div>
    <div class="stop-main">
      <div class="stop-name">${esc(st.name)}</div>
      <div class="stop-meta">
        ${codes}
        ${pips}${more}
        ${transfersOf(sid).length ? '<span class="tag">↔</span>' : ''}
      </div>
    </div>
    <div class="stop-side">${st.verified
      ? `<span class="vmark" title="Verified ${esc(st.verified)}">✓</span>`
      : '<span class="vmark off" title="Not verified">•</span>'}</div>`;

  row.addEventListener('click', () => {
    store.activeDir = di;
    select(sid, pids.length === 1 ? pids[0] : null);
    onPickStation(sid);
    flyToStation(sid);
  });
  return row;
}
