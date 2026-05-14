// dashboard/js/settings.js — Panel 3: Agent Settings

const Settings = (() => {
  let originalPrompt = '';
  const SENSITIVE = ['GEMINI_API_KEY', 'TWILIO_AUTH_TOKEN', 'GOOGLE_APPLICATION_CREDENTIALS'];

  // ── Load all settings ───────────────────────────────────────────
  async function loadSettings() {
    try {
      const data = await fetch('/dashboard/api/settings').then(r => r.json());
      renderEnvForm(data.env || {}, data.sensitive_keys || []);
      const p = data.llm_params || {};
      const tts = data.tts_params || {};
      const mt = document.getElementById('maxTokens');
      const tp = document.getElementById('temperature');
      const ss = document.getElementById('speakingSpeed');
      const pt = document.getElementById('pitch');
      const vg = document.getElementById('volumeGain');
      
      if (mt) { mt.value = p.max_tokens || 180; document.getElementById('maxTokensVal').textContent = mt.value; }
      if (tp) { tp.value = p.temperature ?? 0.85; document.getElementById('temperatureVal').textContent = Number(tp.value).toFixed(2); }
      if (ss) { ss.value = tts.speaking_speed ?? 1.2; document.getElementById('speakingSpeedVal').textContent = Number(ss.value).toFixed(2); }
      if (pt) { pt.value = tts.pitch ?? 0.0; document.getElementById('pitchVal').textContent = Number(pt.value).toFixed(2); }
      if (vg) { vg.value = tts.volume_gain_db ?? 0.0; document.getElementById('volumeGainVal').textContent = Number(vg.value).toFixed(2); }
    } catch (e) {
      App.toast('Failed to load settings: ' + e.message, 'error');
    }
  }

  function renderEnvForm(env, sensitiveKeys) {
    const form = document.getElementById('envForm');
    const entries = Object.entries(env);
    form.innerHTML = entries.map(([k, v]) => {
      const isSensitive = sensitiveKeys.includes(k);
      const isSet = v && v.startsWith('*');
      return `
        <div class="env-row">
          <label class="env-label" for="env_${k}">
            ${k}
            ${isSensitive ? '<span class="badge badge-warn">secret</span>' : ''}
            ${isSet ? '<span class="badge badge-ok">set</span>' : ''}
          </label>
          <input
            class="env-input${isSensitive ? ' sensitive' : ''}"
            id="env_${k}" data-key="${k}"
            type="${isSensitive ? 'password' : 'text'}"
            value="${isSet ? '' : escHtml(v)}"
            placeholder="${isSet ? '(already set — enter new value to update)' : 'Enter value…'}"
          />
        </div>`;
    }).join('');
  }

  // ── Load prompt ─────────────────────────────────────────────────
  async function loadPrompt() {
    try {
      const data = await fetch('/dashboard/api/prompt').then(r => r.json());
      originalPrompt = data.prompt || '';
      const ta = document.getElementById('promptEditor');
      ta.value = originalPrompt;
      document.getElementById('promptChars').textContent = `${originalPrompt.length} chars`;
      document.getElementById('promptMeta').textContent = `File: ${data.path}`;
    } catch (e) {
      App.toast('Failed to load prompt: ' + e.message, 'error');
    }
  }

  // ── Load available slots ─────────────────────────────────────────
  async function loadSlots() {
    const list = document.getElementById('slotsList');
    try {
      const data = await fetch('/dashboard/api/slots').then(r => r.json());
      renderSlots(data.slots || []);
    } catch (e) {
      list.innerHTML = '<tr><td colspan="5" class="table-empty">Failed to load slots</td></tr>';
    }
  }

  function renderSlots(slots) {
    const list = document.getElementById('slotsList');
    if (!slots.length) {
      list.innerHTML = '<tr><td colspan="5" class="table-empty">No slots configured</td></tr>';
      return;
    }
    const days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
    list.innerHTML = slots.map(s => `
      <tr>
        <td><strong>${escHtml(s.department)}</strong></td>
        <td>${days[s.day_of_week]}</td>
        <td><code>${s.start_time} – ${s.end_time}</code></td>
        <td>
          <span class="badge ${s.is_active ? 'badge-ok' : 'badge-danger'}">
            ${s.is_active ? 'Active' : 'Disabled'}
          </span>
        </td>
        <td>
          <button class="btn btn-sm btn-ghost toggle-slot-btn" data-id="${s.id}" data-active="${s.is_active}">
            ${s.is_active ? 'Disable' : 'Enable'}
          </button>
        </td>
      </tr>
    `).join('');

    document.querySelectorAll('.toggle-slot-btn').forEach(btn => {
      btn.onclick = async () => {
        const id = btn.dataset.id;
        const active = btn.dataset.active === 'true';
        await toggleSlot(id, !active);
      };
    });
  }

  async function toggleSlot(id, active) {
    try {
      await fetch(`/dashboard/api/slots/${id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ is_active: active }),
      });
      loadSlots();
      App.toast('Slot updated', 'ok');
    } catch (e) {
      App.toast('Failed to update slot', 'error');
    }
  }

  // ── Save env ────────────────────────────────────────────────────
  async function saveEnv() {
    const inputs = document.querySelectorAll('#envForm .env-input');
    const env = {};
    inputs.forEach(inp => {
      if (inp.value.trim()) env[inp.dataset.key] = inp.value.trim();
    });
    try {
      const res = await fetch('/dashboard/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ env }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail);
      App.toast(`Saved: ${data.saved_env_keys.join(', ') || 'no changes'}`, 'ok');
      loadSettings();
    } catch (e) {
      App.toast('Save failed: ' + e.message, 'error');
    }
  }

  // ── Save Agent params ─────────────────────────────────────────────
  async function saveLlmParams() {
    const llm_params = {
      max_tokens: parseInt(document.getElementById('maxTokens').value),
      temperature: parseFloat(document.getElementById('temperature').value),
    };
    const tts_params = {
      speaking_speed: parseFloat(document.getElementById('speakingSpeed').value),
      pitch: parseFloat(document.getElementById('pitch').value),
      volume_gain_db: parseFloat(document.getElementById('volumeGain').value),
    };
    try {
      const res = await fetch('/dashboard/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ llm_params, tts_params }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail);
      App.toast('Agent parameters saved ✓', 'ok');
    } catch (e) {
      App.toast(e.message, 'error');
    }
  }

  // ── Save prompt ─────────────────────────────────────────────────
  async function savePrompt() {
    const prompt = document.getElementById('promptEditor').value;
    try {
      const res = await fetch('/dashboard/api/prompt', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail);
      originalPrompt = prompt;
      App.toast(`Prompt saved (${data.length} chars) ✓`, 'ok');
    } catch (e) {
      App.toast(e.message, 'error');
    }
  }

  // ── Test connections ────────────────────────────────────────────
  async function testConnections() {
    const btn = document.getElementById('testConnectionsBtn');
    btn.disabled = true;
    btn.textContent = 'Testing…';

    ['gemini', 'google', 'twilio', 'db'].forEach(k => {
      const el = document.querySelector(`#conn-${k} .conn-status`);
      el.className = 'conn-status conn-checking';
      el.textContent = '…';
    });

    try {
      const data = await fetch('/dashboard/api/test-connections', { method: 'POST' }).then(r => r.json());
      Object.entries(data).forEach(([k, v]) => {
        const el = document.querySelector(`#conn-${k} .conn-status`);
        if (!el) return;
        if (v.ok) {
          el.className = 'conn-status conn-ok';
          el.textContent = v.account_name ? `✓ ${v.account_name}` : '✓ OK';
        } else {
          el.className = 'conn-status conn-fail';
          el.textContent = '✗ Failed';
          console.warn(k, v.error);
        }
      });
      App.toast('Connection tests complete', 'ok');
    } catch (e) {
      App.toast('Connection test failed: ' + e.message, 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = 'Test All Connections';
    }
  }

  function escHtml(s) {
    return (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  // ── Init ────────────────────────────────────────────────────────
  function init() {
    document.getElementById('saveEnvBtn').addEventListener('click', saveEnv);
    document.getElementById('saveLlmBtn').addEventListener('click', saveLlmParams);
    document.getElementById('savePromptBtn').addEventListener('click', savePrompt);
    document.getElementById('revertPromptBtn').addEventListener('click', () => {
      document.getElementById('promptEditor').value = originalPrompt;
      App.toast('Reverted to saved prompt', 'warn');
    });
    document.getElementById('testConnectionsBtn').addEventListener('click', testConnections);

    document.getElementById('maxTokens').addEventListener('input', e => {
      document.getElementById('maxTokensVal').textContent = e.target.value;
    });
    document.getElementById('temperature').addEventListener('input', e => {
      document.getElementById('temperatureVal').textContent = Number(e.target.value).toFixed(2);
    });
    document.getElementById('speakingSpeed').addEventListener('input', e => {
      document.getElementById('speakingSpeedVal').textContent = Number(e.target.value).toFixed(2);
    });
    document.getElementById('pitch').addEventListener('input', e => {
      document.getElementById('pitchVal').textContent = Number(e.target.value).toFixed(2);
    });
    document.getElementById('volumeGain').addEventListener('input', e => {
      document.getElementById('volumeGainVal').textContent = Number(e.target.value).toFixed(2);
    });
    document.getElementById('promptEditor').addEventListener('input', e => {
      document.getElementById('promptChars').textContent = `${e.target.value.length} chars`;
    });
  }

  function onActivate() {
    loadSettings();
    loadPrompt();
    loadSlots();
  }

  return { init, onActivate };
})();
