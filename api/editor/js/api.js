const j = async (res) => {
  const body = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
  if (!res.ok) throw Object.assign(new Error(body.error || `HTTP ${res.status}`), { body, status: res.status });
  return body;
};

export const api = {
  state: () => fetch('api/state').then(j),
  history: () => fetch('api/history').then(j),
  diff: () => fetch('api/diff').then(j),
  write: (doc) => fetch('api/doc', {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ doc }),
  }).then(j),
  commit: (doc, message, push) => fetch('api/commit', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ doc, message, push }),
  }).then(j),
  discard: () => fetch('api/discard', { method: 'POST' }).then(j),
};
