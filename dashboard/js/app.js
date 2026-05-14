// dashboard/js/app.js — Tab navigation, toasts, health check

const App = (() => {
  // ── Toast ───────────────────────────────────────────────────────
  function toast(message, type = 'ok') {
    const container = document.getElementById('toastContainer');
    const el = document.createElement('div');
    el.className = `toast ${type}`;
    const icon = type === 'ok' ? '✅' : type === 'error' ? '❌' : '⚠️';
    el.innerHTML = `<span>${icon}</span><span>${message}</span>`;
    container.appendChild(el);
    setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity .3s'; setTimeout(() => el.remove(), 300); }, 3500);
  }

  // ── Tab switching ───────────────────────────────────────────────
  function initTabs() {
    document.querySelectorAll('.tab-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const tab = btn.dataset.tab;
        document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
        document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
        btn.classList.add('active');
        document.getElementById(`panel-${tab}`).classList.add('active');
        
        const titleEl = document.getElementById('pageTitle');
        if (titleEl) {
          titleEl.textContent = btn.innerText.replace(/^[^\w\s]+/, '').trim();
        }

        if (tab === 'conversations') Conversations.onActivate();
        if (tab === 'appointments') { if (typeof fetchBookings === 'function') fetchBookings(); }
        if (tab === 'complaints') { if (typeof fetchComplaints === 'function') fetchComplaints(); }
        if (tab === 'settings') Settings.onActivate();
      });
    });
  }

  // ── Health check ────────────────────────────────────────────────
  async function checkHealth() {
    const dot = document.getElementById('statusDot');
    const lbl = document.getElementById('statusLabel');
    const dbBadge = document.getElementById('dbStatusBadge');
    
    try {
      const res = await fetch('/health');
      const data = await res.json();
      if (data.status === 'ok') {
        dot.className = 'status-dot live';
        lbl.textContent = 'Server live';
      } else {
        dot.className = 'status-dot error';
        lbl.textContent = 'Server error';
      }
      
      if (dbBadge) {
        if (data.db_status === 'connected') {
          dbBadge.className = 'badge badge-ok';
          dbBadge.textContent = 'Connected';
        } else {
          dbBadge.className = 'badge badge-danger';
          dbBadge.textContent = 'Disconnected';
        }
      }
    } catch {
      dot.className = 'status-dot error';
      lbl.textContent = 'Server offline';
      if (dbBadge) {
        dbBadge.className = 'badge badge-danger';
        dbBadge.textContent = 'Offline';
      }
    }
  }

  // ── Boot ────────────────────────────────────────────────────────
  function init() {
    initTabs();
    Tester.init();
    Conversations.init();
    Settings.init();
    checkHealth();
    setInterval(checkHealth, 15000);
  }

  document.addEventListener('DOMContentLoaded', init);
  return { toast };
})();
