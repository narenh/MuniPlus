// Metro lines that share track, drawn side by side the way a metro map draws
// them, instead of on top of each other where only the last one drawn shows.
//
// Every line's path is resampled every few metres and snapped onto the points
// of any line already placed within SNAP_M, so five lines down the Market St
// subway become one chain of shared points. A stretch between two shared
// points is then an edge, and the lines using an edge share it side by side:
// each gets a slot, an offset from the centre in line widths, and a width
// factor `k` that keeps the whole bundle within MAX_SPREAD line widths.
//
// Pure geometry, no map: map.js turns the pieces into features.

const M_LAT = 110540, M_LON = 111320 * Math.cos(37.76 * Math.PI / 180);

/** Resampling step. Short enough that a snapped path keeps the bends of the
 *  track at any zoom the map draws lines at. */
const SAMPLE_M = 8;
/** Two paths closer than this are the same track. Each direction of a subway
 *  line has its own tunnel, a few metres from the other; the nearest separate
 *  street is more than fifty away. */
const SNAP_M = 14;
/** How wide a bundle may get, in single line widths, however many lines are in
 *  it. Five lines at full width would bury the streets. */
export const MAX_SPREAD = 2.5;
/** Edges over which a line eases between its place in a bundle and its own
 *  centre, either side of where it joins or leaves: about 30 m each way, so the
 *  line bends across instead of stepping. */
const EASE = 4;
/** A gap this many points or fewer in another path's chain is filled in, so a
 *  path that snapped to every other point of it still shares every edge. */
const GAP = 6;

/**
 * @param traversals [{line, coords: [[lon, lat], ...]}], each direction of each
 *   line separately. Lines are given slots in `order`.
 * @param order line ids, first = leftmost facing along a bundle's canonical
 *   direction (see `orient`).
 * @returns {pieces: [{t, line, k, off, coords}], nodeNear(lonLat, maxM)}
 */
export function bundle(traversals, order) {
  const rank = new Map(order.map((id, i) => [id, i]));
  const nx = [], ny = [], owner = [], oidx = [];
  const grid = new Map();
  const cell = (x, y) => `${Math.floor(x / SNAP_M)},${Math.floor(y / SNAP_M)}`;

  function nearest(x, y, self) {
    const cx = Math.floor(x / SNAP_M), cy = Math.floor(y / SNAP_M);
    let best = -1, bestD = SNAP_M;
    for (let i = -1; i <= 1; i++) {
      for (let j = -1; j <= 1; j++) {
        for (const n of grid.get(`${cx + i},${cy + j}`) || []) {
          // Never onto its own path: a line that doubles back on its own track
          // (a terminal loop) is still one line there, not a bundle.
          if (owner[n] === self) continue;
          const d = Math.hypot(nx[n] - x, ny[n] - y);
          if (d < bestD) { bestD = d; best = n; }
        }
      }
    }
    return best;
  }

  // --- chains of shared nodes, one per traversal
  const chains = traversals.map(() => []);
  traversals.forEach((tr, t) => {
    const chain = chains[t];
    const push = n => {
      const last = chain[chain.length - 1];
      if (n === last) return;
      if (n === chain[chain.length - 2]) {
        // A -> B -> A: two samples either side of one node snapped back and
        // forth. Read as travel, the middle edge runs backwards and its offset
        // flips, so it is dropped: the path stays at A.
        const dropped = chain.pop();
        if (owner[dropped] === t) {
          // Laid by this path a moment ago and now on no chain: no later path
          // may snap to it.
          const list = grid.get(cell(nx[dropped], ny[dropped]));
          list.splice(list.indexOf(dropped), 1);
        }
        return;
      }
      if (last !== undefined && owner[last] === owner[n] && owner[n] !== t) {
        // Snapped to every other point of someone else's path: take the ones
        // skipped too, or the edges between would not be shared.
        const src = chains[owner[n]], a = oidx[last], b = oidx[n];
        if (Math.abs(b - a) > 1 && Math.abs(b - a) <= GAP) {
          const step = Math.sign(b - a);
          for (let i = a + step; i !== b; i += step) if (chain[chain.length - 1] !== src[i]) chain.push(src[i]);
        }
      }
      chain.push(n);
    };
    for (const [x, y] of resample(tr.coords)) {
      let n = nearest(x, y, t);
      if (n < 0) {
        n = nx.length;
        nx.push(x); ny.push(y); owner.push(t); oidx.push(chain.length);
        const key = cell(x, y);
        if (!grid.has(key)) grid.set(key, []);
        grid.get(key).push(n);
      }
      push(n);
    }
  });

  // --- which lines use each edge
  const ekey = (a, b) => (a < b ? `${a}|${b}` : `${b}|${a}`);
  const users = new Map();
  chains.forEach((chain, t) => {
    for (let i = 1; i < chain.length; i++) {
      const key = ekey(chain[i - 1], chain[i]);
      if (!users.has(key)) users.set(key, new Set());
      users.get(key).add(traversals[t].line);
    }
  });

  const orientation = orient(chains, ekey, users, nx, ny);

  // --- offsets per edge, eased, then cut into pieces of one offset each
  const pieces = [];
  chains.forEach((chain, t) => {
    const line = traversals[t].line;
    const raw = [];
    for (let i = 1; i < chain.length; i++) {
      const a = chain[i - 1], b = chain[i];
      const lines = [...users.get(ekey(a, b))].sort((p, q) => (rank.get(p) ?? 1e9) - (rank.get(q) ?? 1e9) || (p < q ? -1 : 1));
      const n = lines.length;
      const k = Math.min(1, MAX_SPREAD / n);
      // Positive line-offset is to the right of the direction of travel, so a
      // traversal running against the edge's canonical direction mirrors it.
      const sign = (a < b ? 1 : -1) * orientation(ekey(a, b));
      raw.push({ k, off: sign * (lines.indexOf(line) - (n - 1) / 2) * k });
    }
    const eased = raw.map((_, i) => {
      let k = 0, off = 0, w = 0;
      for (let j = Math.max(0, i - EASE); j <= Math.min(raw.length - 1, i + EASE); j++) {
        const wt = EASE + 1 - Math.abs(i - j);
        k += raw[j].k * wt; off += raw[j].off * wt; w += wt;
      }
      // Rounded so that a long run at one offset is one piece, not hundreds.
      return { k: Math.round(k / w * 20) / 20, off: Math.round(off / w * 20) / 20 };
    });
    let start = 0;
    for (let i = 1; i <= eased.length; i++) {
      if (i < eased.length && eased[i].k === eased[start].k && eased[i].off === eased[start].off) continue;
      pieces.push({
        t, line, k: eased[start].k, off: eased[start].off,
        coords: chain.slice(start, i + 1).map(n => [nx[n] / M_LON, ny[n] / M_LAT]),
      });
      start = i;
    }
  });

  /** The shared node nearest a point that at least `minLines` lines pass,
   *  with how many do and the bearing of the track there, for drawing a
   *  station across the bundle. */
  function nodeNear([lon, lat], maxM, minLines = 1) {
    const x = lon * M_LON, y = lat * M_LAT;
    let best = null, bestD = maxM;
    chains.forEach(chain => {
      for (let i = 1; i < chain.length; i++) {
        const a = chain[i - 1], b = chain[i];
        const d = Math.hypot(nx[b] - x, ny[b] - y);
        if (d >= bestD) continue;
        const n = users.get(ekey(a, b)).size;
        if (n < minLines) continue;
        bestD = d;
        best = {
          at: [nx[b] / M_LON, ny[b] / M_LAT], lines: n,
          bearing: (Math.atan2(nx[b] - nx[a], ny[b] - ny[a]) * 180 / Math.PI + 360) % 360,
        };
      }
    });
    return best;
  }

  return { pieces, nodeNear };
}

/** Points along `coords` in local metres, no more than SAMPLE_M apart,
 *  keeping every original vertex. */
function resample(coords) {
  const out = [];
  for (let i = 0; i < coords.length; i++) {
    const x = coords[i][0] * M_LON, y = coords[i][1] * M_LAT;
    if (i > 0) {
      const [px, py] = out[out.length - 1];
      const steps = Math.ceil(Math.hypot(x - px, y - py) / SAMPLE_M);
      for (let s = 1; s < steps; s++) out.push([px + (x - px) * s / steps, py + (y - py) * s / steps]);
    }
    out.push([x, y]);
  }
  return out;
}

/**
 * Which way along each edge the bundle's slots are counted: +1 means from the
 * lower node id to the higher, -1 the reverse.
 *
 * Node ids alone are not enough. The Market St nodes are laid by whichever line
 * got there first, the Twin Peaks tunnel's by another, maybe running the other
 * way; the L runs through both, and would swap sides where they meet. So the
 * orientation is propagated along every path instead: two consecutive edges of
 * one traversal must be read the same way as that traversal reads them. That
 * is union-find with parity; a contradiction (a path doubling back on the same
 * edge) keeps whatever was decided first.
 *
 * Each connected set of edges then faces whichever way puts its busiest edge
 * heading east-north-east, the way Market St runs from Castro to Embarcadero,
 * so the first line in `order` is on the north side there as on the printed
 * Muni map.
 */
function orient(chains, ekey, users, nx, ny) {
  const parent = new Map(), parity = new Map();
  // Iterative: a chain of 20,000 edges would overflow a recursive find.
  const find = e => {
    if (!parent.has(e)) { parent.set(e, e); parity.set(e, 1); }
    const path = [];
    let root = e, p = 1;
    while (parent.get(root) !== root) { path.push(root); p *= parity.get(root); root = parent.get(root); }
    // Compress: each node on the path now points at the root, with its parity
    // to the root, computed from the top down.
    let above = 1;
    for (let i = path.length - 1; i >= 0; i--) {
      above *= parity.get(path[i]);
      parity.set(path[i], above);
      parent.set(path[i], root);
    }
    return [root, p];
  };
  const union = (e1, e2, rel) => {  // want o(e1) * o(e2) === rel
    const [r1, p1] = find(e1), [r2, p2] = find(e2);
    if (r1 === r2) return;
    parent.set(r2, r1);
    parity.set(r2, p1 * p2 * rel);
  };
  for (const chain of chains) {
    for (let i = 2; i < chain.length; i++) {
      const a = chain[i - 2], b = chain[i - 1], c = chain[i];
      if (a === c) continue;
      const d1 = a < b ? 1 : -1, d2 = b < c ? 1 : -1;
      // Travelling d1 on the first and d2 on the second, read the same way:
      // d1 * o1 === d2 * o2, so o1 * o2 === d1 * d2.
      union(ekey(a, b), ekey(b, c), d1 * d2);
    }
  }

  // Face each component's busiest edge east-north-east.
  const flip = new Map();
  const busiest = new Map();
  for (const [key, set] of users) {
    const [root] = find(key);
    if (!busiest.has(root) || set.size > busiest.get(root).n) busiest.set(root, { key, n: set.size });
  }
  for (const [root, { key }] of busiest) {
    const [a, b] = key.split('|').map(Number);  // a < b
    const [, p] = find(key);
    const east = (nx[b] - nx[a]) * 2 + (ny[b] - ny[a]) >= 0 ? 1 : -1;
    flip.set(root, east * p);
  }
  return key => {
    const [root, p] = find(key);
    return p * (flip.get(root) ?? 1);
  };
}
