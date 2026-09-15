// The route strip: a vertical line diagram of the active line whose stops can
// be dragged into a new order. This is the thing that should feel like editing
// a route rather than a list.

import { store, edit, select, stationById, linesOf, lineById, esc, platformsOf } from './store.js';
import { flyToStation } from './map.js';

let onPickStation = () => {};
let onRequestAdd = () => {};

export function initStrip({ onPick, onAdd }) {
  onPickStation = onPick || (() => {});
  onRequestAdd = onAdd || (() => {});

  document.getElementById('strip-reverse').onclick = () => {
    const ln = lineById(store.activeLine);
    if (!ln) return;
    edit(`Reverse ${ln.id}`, d => {
      d.lines.find(l => l.id === ln.id).stationIds.reverse();
    });
  };
  document.getElementById('strip-add').onclick = () => onRequestAdd();
}

export function renderStrip() {
  const strip = document.getElementById('strip');
  const ln = lineById(store.activeLine);

  if (!ln) { strip.classList.add('closed'); return; }
  strip.classList.remove('closed');
  strip.style.setProperty('--c', ln.color);

  document.getElementById('strip-badge').textContent = ln.shortName || ln.id;
  document.getElementById('strip-badge').style.setProperty('--c', ln.color);
  document.getElementById('strip-name').textContent = ln.name;

  const nplat = ln.stationIds.reduce((n, id) => n + platformsOf(stationById(id)).length, 0);
  document.getElementById('strip-sub').textContent =
    `${ln.stationIds.length} stations · ${nplat} platforms`;

  const list = document.getElementById('strip-list');
  list.innerHTML = '';

  ln.stationIds.forEach((sid, i) => {
    const st = stationById(sid);
    const row = document.createElement('div');
    row.className = 'stop';
    row.dataset.index = String(i);
    row.dataset.sid = sid;
    if (store.selStation === sid) row.classList.add('sel');

    if (!st) {
      row.innerHTML = `<div class="node"><i></i></div>
        <div class="stop-main"><div class="stop-name" style="color:var(--bad)">Missing station</div>
        <div class="stop-meta"><span class="tag mono">${esc(sid)}</span></div></div><div class="stop-side"></div>`;
      list.appendChild(row);
      return;
    }

    const others = linesOf(sid).filter(l => l.id !== ln.id);
    const terminal = i === 0 || i === ln.stationIds.length - 1;
    const nodeCls = ['node',
      st.kind === 'underground' ? 'underground' : '',
      others.length ? 'interchange' : '',
      terminal ? 'terminal' : ''].filter(Boolean).join(' ');

    row.innerHTML = `
      <div class="${nodeCls}"><i></i></div>
      <div class="stop-main">
        <div class="stop-name">${esc(st.name)}</div>
        <div class="stop-meta">
          ${st.kind === 'underground' ? `<span class="tag">${(st.exits || []).length} EXIT</span>` : ''}
          <span class="tag">${platformsOf(st).length} PLAT</span>
          ${others.map(l => `<span class="pip" style="background:${l.color}" title="${esc(l.name)}"></span>`).join('')}
          ${(st.transfers || []).length ? '<span class="tag">↔</span>' : ''}
        </div>
      </div>
      <div class="stop-side">
        <button class="icon-btn grip" title="Drag to reorder" data-act="grip">
          <svg width="13" height="13" viewBox="0 0 16 16" fill="currentColor"><circle cx="6" cy="4" r="1.3"/><circle cx="10" cy="4" r="1.3"/><circle cx="6" cy="8" r="1.3"/><circle cx="10" cy="8" r="1.3"/><circle cx="6" cy="12" r="1.3"/><circle cx="10" cy="12" r="1.3"/></svg>
        </button>
        <button class="icon-btn danger" title="Remove from this line" data-act="remove">
          <svg width="13" height="13" viewBox="0 0 16 16" fill="none"><path d="M4 8h8" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>
        </button>
      </div>`;

    row.addEventListener('click', ev => {
      const act = ev.target.closest('[data-act]')?.dataset.act;
      if (act === 'remove') {
        ev.stopPropagation();
        edit(`Remove ${st.name} from ${ln.id}`, d => {
          const L = d.lines.find(l => l.id === ln.id);
          L.stationIds = L.stationIds.filter(x => x !== sid);
          const S = d.stations.find(x => x.id === sid);
          if (S) S.lines = (S.lines || []).filter(x => x !== ln.id);
        });
        return;
      }
      if (act === 'grip') return;
      select(sid, null);
      onPickStation(sid);
      flyToStation(sid);
    });

    attachDrag(row, list, ln);
    list.appendChild(row);
  });
}

// ------------------------------------------------------------------- drag
function attachDrag(row, list, ln) {
  const grip = row.querySelector('[data-act="grip"]');
  if (!grip) return;

  grip.addEventListener('pointerdown', ev => {
    ev.preventDefault();
    ev.stopPropagation();
    grip.setPointerCapture(ev.pointerId);

    const from = Number(row.dataset.index);
    let to = from;
    row.classList.add('drag-src');

    const clear = () => list.querySelectorAll('.stop')
      .forEach(r => r.classList.remove('drag-over-top', 'drag-over-bottom'));

    const move = e => {
      const rows = [...list.querySelectorAll('.stop')];
      clear();
      const target = rows.find(r => {
        const b = r.getBoundingClientRect();
        return e.clientY >= b.top && e.clientY <= b.bottom;
      });
      if (!target || target === row) return;
      const b = target.getBoundingClientRect();
      const after = e.clientY > b.top + b.height / 2;
      target.classList.add(after ? 'drag-over-bottom' : 'drag-over-top');
      const ti = Number(target.dataset.index);
      to = after ? ti + 1 : ti;
    };

    const up = () => {
      grip.releasePointerCapture(ev.pointerId);
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      clear();
      row.classList.remove('drag-src');

      let dest = to;
      if (dest > from) dest -= 1;
      if (dest === from || dest < 0) return;

      const name = stationById(row.dataset.sid)?.name || row.dataset.sid;
      edit(`Move ${name} in ${ln.id}`, d => {
        const L = d.lines.find(l => l.id === ln.id);
        const [moved] = L.stationIds.splice(from, 1);
        L.stationIds.splice(dest, 0, moved);
      });
    };

    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  });
}
