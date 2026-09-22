// Right-hand inspector: the selected station, its flat list of platforms and
// its transfers. Coordinates, stop names and the lines serving a platform are
// shown but never editable: they come from 511, not from a person.

import {
  store, edit, select, stationById, linesOf, esc, metresBetween,
  platformsOf, hasCoord,
} from './store.js';
import { flyToStation, refresh, hint, map, highlightLink } from './map.js';

const HEADINGS = ['northbound', 'southbound', 'eastbound', 'westbound'];
const AGENCIES = ['bart', 'caltrain'];
const ARROW = { northbound: 0, eastbound: 90, southbound: 180, westbound: 270 };

let pickStationFor = null;

export function initInspector({ onNeedStationPicker }) {
  pickStationFor = onNeedStationPicker;
  document.getElementById('insp-close').onclick = () => { select(null, null); renderInspector(); };
}

export function renderInspector() {
  const el = document.getElementById('inspector');
  const st = stationById(store.selStation);

  if (!st) {
    el.classList.add('closed');
    document.body.classList.add('no-inspector');
    document.body.classList.remove('inspector-open');
    return;
  }
  el.classList.remove('closed');
  document.body.classList.remove('no-inspector');
  document.body.classList.add('inspector-open');

  const ls = linesOf(st.id);
  el.style.setProperty('--accent', ls[0]?.color || '#6ea8fe');

  document.getElementById('insp-name').textContent = st.name;
  const n = platformsOf(st).length;
  document.getElementById('insp-id').textContent = `${st.id} · ${n} platform${n === 1 ? '' : 's'}`;

  const box = document.getElementById('insp-lines');
  box.innerHTML = '';
  for (const ln of store.doc.lines) {
    const on = (st.lines || []).includes(ln.id);
    const b = document.createElement('button');
    b.className = 'mini-bullet' + (on ? '' : ' off');
    b.style.background = ln.color;
    b.textContent = ln.shortName || ln.id;
    b.title = on ? `${ln.name} — derived from the platforms below` : ln.name;
    b.disabled = true;                       // station.lines is derived
    box.appendChild(b);
  }

  document.getElementById('insp-body').innerHTML =
    sectionStation(st) +
    sectionPlatforms(st) +
    sectionTransfers(st) +
    sectionDanger(st);
  wire(st);
}

// ------------------------------------------------------------------ station
function sectionStation(st) {
  const coord = hasCoord(st)
    ? `<code>${st.latitude}, ${st.longitude}</code>`
    : `<span style="color:var(--warn)">no platforms with coordinates</span>`;
  return `
  <div class="sect">
    <div class="sect-head"><div class="micro">Station</div></div>
    <div class="field">
      <label class="micro">Display name</label>
      <input class="inp" id="f-name" value="${esc(st.name)}">
    </div>
    <div class="field">
      <label class="micro">Identifier</label>
      <input class="inp mono" id="f-id" value="${esc(st.id)}">
    </div>
    <div class="field">
      <label class="micro">Coordinate <span style="text-transform:none;letter-spacing:0;color:var(--ink-faint)">— derived from platforms, not editable</span></label>
      <div class="derived mono">${coord}</div>
    </div>
  </div>`;
}

// ---------------------------------------------------------------- platforms
function sectionPlatforms(st) {
  return `
  <div class="sect">
    <div class="sect-head">
      <div class="micro">Platforms · ${platformsOf(st).length}</div>
      <button class="icon-btn" data-act="add-plat" title="Add a platform">
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M8 3.5v9M3.5 8h9" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>
      </button>
    </div>
    ${platformCards(st) || '<div class="empty">No platforms.</div>'}
  </div>`;
}

function platformCards(st) {
  return platformsOf(st).map(p => {
    const sel = store.selPlatform === String(p.id);
    const placed = Number.isFinite(p.latitude) && Number.isFinite(p.longitude);

    // Which lines serve a platform is 511's to say, so these are a readout.
    const lines = store.doc.lines.filter(ln => (p.lines || []).includes(ln.id)).map(ln =>
      `<button class="mini-bullet" style="background:${ln.color}" disabled
        title="${esc(ln.name)}">${esc(ln.shortName || ln.id)}</button>`).join('')
      || '<span class="empty">none</span>';

    return `
    <div class="plat ${sel ? 'sel' : ''}" data-code="${esc(p.id)}">
      <div class="plat-head">
        <div class="compass" title="${esc(p.heading)}">
          <svg width="13" height="13" viewBox="0 0 16 16" fill="none" style="transform:rotate(${ARROW[p.heading] ?? 0}deg)">
            <path d="M8 2.2l3.6 10-3.6-2.6-3.6 2.6L8 2.2z" fill="currentColor"/></svg>
        </div>
        <div style="min-width:0">
          <div class="plat-code">${esc(p.id)}</div>
          <div class="plat-sub">${esc(p.stopName || p.name || st.name)}</div>
        </div>
        <div class="plat-actions">
          <button class="icon-btn" data-act="locate" data-code="${esc(p.id)}" title="Show on the map">
            <svg width="13" height="13" viewBox="0 0 16 16" fill="none"><path d="M8 14.5S13 10 13 6.4A5 5 0 003 6.4C3 10 8 14.5 8 14.5z" stroke="currentColor" stroke-width="1.5"/><circle cx="8" cy="6.3" r="1.8" fill="currentColor"/></svg>
          </button>
          <button class="icon-btn danger" data-act="del-plat" data-code="${esc(p.id)}" title="Remove this platform">
            <svg width="13" height="13" viewBox="0 0 16 16" fill="none"><path d="M3.5 4.5h9M6.5 4.5V3h3v1.5M5 4.5l.6 8h4.8l.6-8" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>
          </button>
        </div>
      </div>

      <div class="field">
        <label class="micro">Stop code</label>
        <input class="inp mono" data-pf="id" data-code="${esc(p.id)}" value="${esc(p.id)}">
      </div>
      <div class="field">
        <label class="micro">Label <span style="text-transform:none;letter-spacing:0;color:var(--ink-faint)">— signage, e.g. Platform 1</span></label>
        <input class="inp" data-pf="name" data-code="${esc(p.id)}" value="${esc(p.name ?? '')}" placeholder="none">
      </div>
      <div class="field">
        <label class="micro">Heading</label>
        <select class="inp" data-pf="heading" data-code="${esc(p.id)}">
          ${HEADINGS.map(h => `<option value="${h}" ${p.heading === h ? 'selected' : ''}>${h}</option>`).join('')}
        </select>
      </div>
      <div class="field">
        <label class="micro">Lines</label>
        <div class="insp-lines">${lines}</div>
      </div>
      <div class="field" style="margin-bottom:0">
        <label class="micro">Coordinate <span style="text-transform:none;letter-spacing:0;color:var(--ink-faint)">— from 511, not editable</span></label>
        <div class="derived mono">${placed
          ? `<code>${p.latitude}, ${p.longitude}</code>`
          : '<span style="color:var(--warn)">none</span>'}</div>
      </div>
    </div>`;
  }).join('');
}

// ---------------------------------------------------------------- transfers
const TMODE = {
  street: '<svg width="13" height="9" viewBox="0 0 16 10" fill="none"><path d="M1 5h12" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-dasharray="0.1 2.6"/><path d="M10.5 1.8L14 5l-3.5 3.2" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  indoor: '<svg width="13" height="9" viewBox="0 0 16 10" fill="none"><path d="M1 5h12" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/><path d="M10.5 1.8L14 5l-3.5 3.2" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>',
};

function sectionTransfers(st) {
  const inbound = store.doc.stations
    .filter(s => s.id !== st.id && (s.transfers || []).some(t => t.to === st.id))
    .map(s => s.id);

  const rows = (st.transfers || []).map(t => {
    const to = stationById(t.to);
    const mutual = inbound.includes(t.to);
    const away = to && hasCoord(to) && hasCoord(st) ? `${metresBetween(st, to)} m` : '—';
    return `
    <span class="tchip link ${mutual ? 'both' : 'out'}" data-act="hover-link" data-id="${esc(t.to)}">
      <i class="dir">${TMODE[t.mode] || TMODE.street}</i>
      <span class="nm">${esc(to ? to.name : t.to)}</span>
      <em>${t.mode === 'indoor' ? 'indoor' : away}</em>
      <button class="x" data-act="cycle-mode" data-id="${esc(t.to)}" title="Switch to ${t.mode === 'indoor' ? 'street' : 'indoor'}">
        <svg width="10" height="10" viewBox="0 0 12 12" fill="none"><path d="M2 4.5h6.5M6.5 2.5L8.8 4.5 6.5 6.5M10 7.5H3.5M5.5 5.5L3.2 7.5 5.5 9.5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>
      </button>
      <button class="x" data-act="untransfer" data-id="${esc(t.to)}" title="Remove">
        <svg width="9" height="9" viewBox="0 0 10 10" fill="none"><path d="M2 2l6 6M8 2l-6 6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
      </button>
    </span>`;
  }).join('');

  const inOnly = inbound.filter(id => !(st.transfers || []).some(t => t.to === id)).map(id => `
    <span class="tchip link in" data-act="hover-link" data-id="${esc(id)}">
      <i class="dir">${TMODE.street}</i>
      <span class="nm">${esc(stationById(id)?.name || id)}</span>
      <em>links here</em>
      <button class="x mk" data-act="reciprocate" data-id="${esc(id)}" title="Link back">
        <svg width="10" height="10" viewBox="0 0 12 12" fill="none"><path d="M6 2.5v7M2.5 6h7" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>
      </button>
    </span>`).join('');

  const agencies = AGENCIES.map(ag => {
    const on = (st.transferAgencies || []).includes(ag);
    return `<button class="tchip ${on ? 'agency-on' : 'add'}" data-act="agency" data-ag="${ag}">${ag.toUpperCase()}</button>`;
  }).join('');

  return `
  <div class="sect">
    <div class="sect-head"><div class="micro">Transfers</div></div>
    ${rows ? `<div class="field"><div class="chips">${rows}</div></div>` : ''}
    ${inOnly ? `<div class="field"><label class="micro">One way in <span style="text-transform:none;letter-spacing:0;color:var(--ink-faint)">— they link here; this station does not link back</span></label><div class="chips">${inOnly}</div></div>` : ''}
    ${!rows && !inOnly ? '<div class="empty" style="margin-bottom:10px">No transfer links.</div>' : ''}
    <div class="field">
      <button class="tchip add" data-act="add-transfer" style="width:100%;justify-content:center;height:29px">
        <svg width="10" height="10" viewBox="0 0 12 12" fill="none"><path d="M6 2.5v7M2.5 6h7" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
        Link a station
      </button>
    </div>
    <div class="field" style="margin-top:12px">
      <label class="micro">Other agencies</label>
      <div class="chips">${agencies}</div>
    </div>
  </div>`;
}

function sectionDanger(st) {
  return `
  <div class="sect">
    <div class="sect-head"><div class="micro">Danger zone</div></div>
    <button class="btn" id="del-station" style="width:100%;justify-content:center;color:var(--bad);border-color:rgba(251,113,133,.3)">
      Delete “${esc(st.name)}”
    </button>
  </div>`;
}


// ==========================================================================
// Wiring. Every handler commits through edit(), so one gesture is one undo and
// derived fields are recomputed by the store.
// ==========================================================================
function wire(st) {
  const body = document.getElementById('insp-body');
  const sid = st.id;
  const commit = (label, fn) => edit(label, d => fn(d.stations.find(s => s.id === sid), d));

  const plat = (s2, code) => platformsOf(s2).find(x => String(x.id) === String(code));

  // ---------------------------------------------------------------- station
  const name = body.querySelector('#f-name');
  name.onchange = () => {
    const v = name.value.trim();
    if (!v || v === st.name) { name.value = st.name; return; }
    commit(`Rename ${sid}`, s => { s.name = v; });
  };

  const idf = body.querySelector('#f-id');
  idf.onchange = () => {
    const v = idf.value.trim();
    if (!v || v === sid) { idf.value = sid; return; }
    if (store.doc.stations.some(s => s.id === v)) { idf.classList.add('bad'); hint(`"${v}" already exists`); return; }
    edit(`Rename id ${sid} → ${v}`, d => {
      d.stations.find(s => s.id === sid).id = v;
      for (const l of d.lines) l.stationIds = l.stationIds.map(x => x === sid ? v : x);
      for (const s of d.subways || []) s.stationIds = s.stationIds.map(x => x === sid ? v : x);
      for (const s of d.stations) for (const t of s.transfers || []) if (t.to === sid) t.to = v;
    });
    select(v, store.selPlatform);
  };

  // -------------------------------------------------------------- platforms
  body.querySelectorAll('[data-pf]').forEach(el => {
    el.onchange = () => {
      const code = el.dataset.code, f = el.dataset.pf;
      let v = el.value.trim();

      if (f === 'id') {
        if (!v || v === code) { renderInspector(); return; }
        const clash = store.doc.stations.some(s2 =>
          platformsOf(s2).some(p => String(p.id) === v && !(s2.id === sid && String(p.id) === code)));
        if (clash) { el.classList.add('bad'); hint(`Stop code ${v} is already used`); return; }
        commit(`Stop code ${code} → ${v}`, s => { plat(s, code).id = v; });
        select(sid, v);
        return;
      }
      if (f === 'name') v = v === '' ? null : v;
      commit(`Edit ${code}`, s => { plat(s, code)[f] = v; });
    };
  });

  body.querySelectorAll('[data-act="add-plat"]').forEach(b => {
    b.onclick = () => {
      const code = freshCode();
      // Only what a person decides. Its coordinate, stop name and lines are 511's.
      commit('Add platform', s => {
        s.platforms.push({ id: code, heading: 'northbound', name: null });
      });
      select(sid, code);
      hint(`Platform ${code} added — set its real stop code`);
    };
  });

  // ------------------------------------------------------------ row actions
  body.querySelectorAll('.plat[data-code]').forEach(card => {
    card.addEventListener('click', ev => {
      if (ev.target.closest('[data-act]') || ev.target.closest('input,select,button')) return;
      select(sid, card.dataset.code);
      renderInspector(); refresh('platforms');
    });
  });

  body.querySelectorAll('[data-act="hover-link"]').forEach(chip => {
    chip.onmouseenter = () => highlightLink(sid, chip.dataset.id);
    chip.onmouseleave = () => highlightLink(null, null);
  });

  body.querySelectorAll('[data-act]').forEach(b => {
    const act = b.dataset.act;
    if (['add-plat', 'hover-link'].includes(act)) return;
    b.onclick = ev => {
      ev.stopPropagation();
      const code = b.dataset.code;

      if (act === 'locate') {
        const p = plat(st, code);
        select(sid, code);
        if (Number.isFinite(p.latitude) && Number.isFinite(p.longitude)) {
          map.easeTo({ center: [p.longitude, p.latitude], zoom: Math.max(map.getZoom(), 17.4), duration: 800 });
        } else hint(`${code} has no coordinate`);
        renderInspector(); refresh('platforms');
      }

      if (act === 'del-plat') {
        if (platformsOf(st).length === 1) { hint('A station must keep at least one platform'); return; }
        commit(`Remove platform ${code}`, s => {
          s.platforms = s.platforms.filter(p => String(p.id) !== String(code));
        });
      }

      if (act === 'untransfer') {
        commit(`Unlink ${b.dataset.id}`, s => {
          s.transfers = (s.transfers || []).filter(t => t.to !== b.dataset.id);
        });
      }

      // An indoor passage cannot be one-way, so switching to indoor sets both
      // sides; switching away only touches this one.
      if (act === 'cycle-mode') {
        const other = b.dataset.id;
        const cur = (st.transfers || []).find(t => t.to === other)?.mode;
        const to = cur === 'indoor' ? 'street' : 'indoor';
        commit(`${other} transfer → ${to}`, (s, d) => {
          const t = s.transfers.find(x => x.to === other);
          t.mode = to;
          if (to === 'indoor') {
            const os = d.stations.find(x => x.id === other);
            const back = (os.transfers || (os.transfers = [])).find(x => x.to === s.id);
            if (back) back.mode = 'indoor';
            else os.transfers.push({ to: s.id, mode: 'indoor' });
          }
        });
        if (to === 'indoor') hint('Indoor links work both ways, so the reciprocal was added too');
      }

      if (act === 'reciprocate') {
        const other = b.dataset.id;
        const mode = (stationById(other)?.transfers || []).find(t => t.to === sid)?.mode || 'street';
        commit(`Link ${sid} back to ${other}`, s => {
          (s.transfers || (s.transfers = [])).push({ to: other, mode });
        });
      }

      if (act === 'agency') {
        const ag = b.dataset.ag;
        commit(`Toggle ${ag} at ${sid}`, s => {
          const list = s.transferAgencies || (s.transferAgencies = []);
          const k = list.indexOf(ag);
          if (k >= 0) list.splice(k, 1); else list.push(ag);
        });
      }

      if (act === 'add-transfer') {
        pickStationFor?.(target => {
          if (!target || target === sid) return;
          if ((st.transfers || []).some(t => t.to === target)) { hint('Already linked'); return; }
          commit(`Link ${sid} → ${target}`, s => {
            (s.transfers || (s.transfers = [])).push({ to: target, mode: 'street' });
          });
        });
      }
    };
  });

  body.querySelector('#del-station').onclick = () => {
    if (!confirm(`Delete "${st.name}"?\n\nIt is also removed from every line, subway and transfer that references it.`)) return;
    edit(`Delete ${sid}`, d => {
      d.stations = d.stations.filter(s => s.id !== sid);
      for (const l of d.lines) l.stationIds = l.stationIds.filter(x => x !== sid);
      for (const s of d.subways || []) s.stationIds = s.stationIds.filter(x => x !== sid);
      for (const s of d.stations) s.transfers = (s.transfers || []).filter(t => t.to !== sid);
    });
    select(null, null);
  };
}

/** A placeholder code that cannot collide with a real one already in the file. */
function freshCode() {
  const used = new Set();
  for (const s of store.doc.stations) for (const p of platformsOf(s)) used.add(String(p.id));
  for (let n = 90000; n < 99999; n++) if (!used.has(String(n))) return String(n);
  return '99999';
}
