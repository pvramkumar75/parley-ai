const $ = (s, r = document) => r.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmt = (c, v) => (v == null ? '—' : `${c} ${Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 })}`);
function toast(t) {
  const e = document.createElement('div'); e.className = 'toast'; e.textContent = t;
  document.body.append(e); setTimeout(() => e.remove(), 3200);
}
async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof j.detail === 'string' ? j.detail : (j.detail?.[0]?.msg || 'Request failed (' + r.status + ')'));
  return j;
}
function renderChat(el, msgs, who) {
  const stick = el.scrollTop + el.clientHeight >= el.scrollHeight - 40;
  const label = { bot: 'Buyer’s AI', sup: 'Supplier', buyer: 'Buyer', whisper: 'Whisper (hidden from supplier)' };
  el.innerHTML = msgs.map(m => `<div class="msg ${m.role === 'sup' && who === 'supplier' ? '' : m.role}" ${m.role === 'sup' && who === 'supplier' ? 'style="align-self:flex-end;background:var(--brand);color:#fff;border-color:var(--brand)"' : ''}>
    ${m.role === 'system' ? '' : `<small>${esc(label[m.role] || m.role)} · ${new Date(m.ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</small>`}${esc(m.text)}</div>`).join('');
  if (stick) el.scrollTop = el.scrollHeight;
}
