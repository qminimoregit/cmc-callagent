// dashboard/js/bookings.js

async function fetchBookings() {
  const filter = document.getElementById('bookingCategoryFilter').value;
  try {
    const res = await fetch(`/dashboard/api/bookings?category=${encodeURIComponent(filter)}`);
    const data = await res.json();
    renderBookings(data.bookings || []);
  } catch (e) {
    console.error("Failed to fetch bookings", e);
    document.getElementById('bookingsList').innerHTML = '<tr><td colspan="5" class="table-empty">Error loading appointments.</td></tr>';
  }
}

function renderBookings(bookings) {
  const tbody = document.getElementById('bookingsList');
  if (!bookings || bookings.length === 0) {
    tbody.innerHTML = '<tr><td colspan="7" class="table-empty">No appointments found.</td></tr>';
    return;
  }
  
  tbody.innerHTML = bookings.map(b => {
    const apptDate = new Date(b.appointment_date);
    const createdDate = new Date(b.created_at);
    const now = new Date();
    
    let timeClass = '';
    const isToday = apptDate.toDateString() === now.toDateString();
    const isPast = apptDate < now && !isToday;
    
    if (isPast) timeClass = 'badge-error';
    else if (isToday) timeClass = 'badge-today';

    return `
      <tr>
        <td style="white-space: nowrap;">
          <strong>${apptDate.toLocaleDateString()}</strong><br>
          <span style="font-size: 1.1rem; color: var(--accent1); font-weight: 700;">${apptDate.toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'})}</span>
          ${timeClass ? `<span class="badge ${timeClass}" style="margin-left: 5px;">${isPast ? 'Past' : 'Today'}</span>` : ''}
        </td>
        <td><strong>${escapeHtml(b.caller_name || 'Unknown')}</strong></td>
        <td><code style="background: rgba(255,255,255,0.05); padding: 2px 6px; border-radius: 4px;">${escapeHtml(b.contact_number || '') || '-'}</code></td>
        <td><span class="badge badge-dept">${escapeHtml(b.service_category || '-')}</span></td>
        <td>${escapeHtml(b.specific_service || '-')}</td>
        <td><small style="opacity: 0.6;">${createdDate.toLocaleDateString()}<br>${createdDate.toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'})}</small></td>
        <td><span class="badge ${b.status === 'Pending' ? 'badge-warn' : 'badge-ok'}">${escapeHtml(b.status)}</span></td>
      </tr>
    `;
  }).join('');
}

// Auto-refresh when tab is active
let bookingInterval = null;
function startBookingRefresh() {
  if (bookingInterval) clearInterval(bookingInterval);
  bookingInterval = setInterval(() => {
    const panel = document.getElementById('panel-appointments');
    if (panel && panel.classList.contains('active')) {
      fetchBookings();
    }
  }, 30000); // 30 seconds
}

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

document.addEventListener('DOMContentLoaded', () => {
  if(document.getElementById('refreshBookingsBtn')) {
    document.getElementById('refreshBookingsBtn').addEventListener('click', fetchBookings);
    document.getElementById('bookingCategoryFilter').addEventListener('change', fetchBookings);
    startBookingRefresh();
  }
});
