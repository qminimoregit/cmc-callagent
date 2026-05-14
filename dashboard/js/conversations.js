// dashboard/js/conversations.js — Panel 2: Twilio Call Conversations

const Conversations = (() => {
  let allCalls = [];
  let selectedSid = null;
  let searchTerm = '';
  let statusFilter = 'all';

  const LANG_FLAGS = { si: '🇱🇰', en: '🇬🇧', ta: '🇮🇳' };
  const STATUS_BADGE = {
    completed:    'badge-ok',
    escalated:    'badge-danger',
    failed:       'badge-danger',
    'in-progress':'badge-warn',
    'no-answer':  'badge-warn',
    canceled:     'badge-warn',
    busy:         'badge-warn',
  };
  const STATUS_LABEL = {
    completed:    'Call Ended',
    escalated:    'Escalated',
    failed:       'Failed',
    'in-progress':'In Progress',
    'no-answer':  'No Answer',
    canceled:     'Canceled',
    busy:         'Busy',
  };

  function fmtDuration(s) {
    if (!s) return '—';
    const m = Math.floor(s / 60), sec = s % 60;
    return m > 0 ? `${m}m ${sec}s` : `${sec}s`;
  }

  function fmtTime(iso) {
    if (!iso) return '—';
    try {
      return new Date(iso).toLocaleString('en-GB', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
    } catch { return iso; }
  }

  // ── Load stats ──────────────────────────────────────────────────
  async function loadStats() {
    try {
      const data = await fetch('/dashboard/api/stats').then(r => r.json());
      document.getElementById('statTotal').textContent = data.total_calls ?? '—';
      document.getElementById('statToday').textContent = data.today_calls ?? '—';
      document.getElementById('statEscalated').textContent = data.escalated ?? '—';
      document.getElementById('statEscRate').textContent = `${data.escalation_rate ?? 0}%`;
      document.getElementById('statAvgDur').textContent = fmtDuration(data.avg_duration_sec);
      
      const statB = document.getElementById('statBookings');
      if (statB) statB.textContent = data.total_bookings ?? '0';
      const statC = document.getElementById('statComplaints');
      if (statC) statC.textContent = data.total_complaints ?? '0';

      // Language bars
      const lb = data.lang_breakdown || {};
      const total = (lb.si || 0) + (lb.en || 0) + (lb.ta || 0) || 1;
      const bars = document.getElementById('langBars');
      bars.innerHTML = ['si', 'en', 'ta'].map(l =>
        `<div class="lang-bar ${l}" style="height:${Math.max(4, Math.round((lb[l] || 0) / total * 36))}px"
          title="${l.toUpperCase()}: ${lb[l] || 0}"></div>`
      ).join('');
    } catch (e) {
      console.warn('Stats load failed', e);
    }
  }

  // ── Load call list ──────────────────────────────────────────────
  async function loadCalls() {
    const res = await fetch(`/dashboard/api/calls?limit=100&status=${statusFilter}`);
    const data = await res.json();
    allCalls = data.calls || [];
    renderList();
  }

  function renderList() {
    const el = document.getElementById('callList');
    const filtered = allCalls.filter(c => {
      if (!searchTerm) return true;
      return (c.phone_number || '').includes(searchTerm) ||
             (c.call_sid || '').includes(searchTerm) ||
             JSON.stringify(c.turns || []).toLowerCase().includes(searchTerm.toLowerCase());
    });

    if (!filtered.length) {
      el.innerHTML = '<div class="list-empty">No calls found.</div>';
      return;
    }

    el.innerHTML = filtered.map(c => `
      <div class="call-item${c.call_sid === selectedSid ? ' selected' : ''}"
           data-sid="${c.call_sid}">
        <div class="call-item-top">
          <span class="call-number">${c.phone_number || c.call_sid.slice(0, 12)}</span>
          <span class="badge ${STATUS_BADGE[c.status] || 'badge-ok'}">${STATUS_LABEL[c.status] || c.status}</span>
        </div>
        <div class="call-item-bot">
          <span>${fmtTime(c.started_at)}</span>
          <span class="call-duration">${fmtDuration(c.duration_sec)}</span>
        </div>
      </div>
    `).join('');

    el.querySelectorAll('.call-item').forEach(item => {
      item.addEventListener('click', () => selectCall(item.dataset.sid));
    });
  }

  // ── Select & render transcript ──────────────────────────────────
  async function selectCall(sid) {
    selectedSid = sid;
    renderList();

    const panel = document.getElementById('transcriptPanel');
    panel.innerHTML = '<div style="padding:20px;color:var(--text3)"><div class="spinner"></div> Loading…</div>';

    try {
      const call = await fetch(`/dashboard/api/calls/${sid}`).then(r => r.json());
      const turns = call.turns || [];

      panel.innerHTML = `
        <div class="transcript-header">
          <div class="transcript-title">${call.phone_number || 'Unknown'}</div>
          <div class="transcript-meta">
            <span>📅 ${fmtTime(call.started_at)}</span>
            <span>⏱ ${fmtDuration(call.duration_sec)}</span>
            <span>🔁 ${Math.ceil(turns.length / 2)} turns</span>
            <span class="badge ${STATUS_BADGE[call.status] || 'badge-ok'}">${STATUS_LABEL[call.status] || call.status}</span>
            ${call.escalated ? '<span class="badge badge-danger">⚠️ Escalated</span>' : ''}
          </div>
        </div>
        <div class="transcript-turns">
          ${turns.length === 0 ? '<div class="list-empty">No transcript data recorded.</div>'
            : turns.map(t => `
            <div class="turn-item">
              <div class="turn-role ${t.role}">
                ${t.role === 'user' ? '👤 Caller' : '🤖 Nimali'}
                <span class="badge ${t.role === 'user' ? (t.lang === 'si' ? 'badge-si' : t.lang === 'ta' ? 'badge-ta' : 'badge-en') : 'badge-ok'}">
                  ${LANG_FLAGS[t.lang] || ''} ${(t.lang || '').toUpperCase()}
                </span>
              </div>
              <div class="turn-text">${escHtml(t.text || '')}</div>
              ${t.ts ? `<div class="turn-ts">${fmtTime(t.ts)}</div>` : ''}
            </div>
          `).join('')}
        </div>`;
    } catch (e) {
      panel.innerHTML = `<div class="transcript-empty"><div class="empty-icon">⚠️</div><div>${e.message}</div></div>`;
    }
  }

  function escHtml(s) {
    if (!s) return '';
    if (typeof s === 'object') s = JSON.stringify(s);
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }

  // ── Init ────────────────────────────────────────────────────────
  function init() {
    document.getElementById('refreshCallsBtn').addEventListener('click', () => {
      loadStats(); loadCalls();
    });
    document.getElementById('callSearch').addEventListener('input', e => {
      searchTerm = e.target.value; renderList();
    });
    document.getElementById('statusFilter').addEventListener('change', e => {
      statusFilter = e.target.value; loadCalls();
    });
  }

  function onActivate() {
    loadStats();
    loadCalls();
  }

  return { init, onActivate };
})();
