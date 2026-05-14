// dashboard/js/complaints.js

async function fetchComplaints() {
  const filter = document.getElementById('complaintCategoryFilter').value;
  try {
    const res = await fetch(`/dashboard/api/complaints?category=${encodeURIComponent(filter)}`);
    const data = await res.json();
    renderComplaints(data.complaints || []);
  } catch (e) {
    console.error("Failed to fetch complaints", e);
    document.getElementById('complaintsList').innerHTML = '<tr><td colspan="6" class="table-empty">Error loading complaints.</td></tr>';
  }
}

function renderComplaints(complaints) {
  const tbody = document.getElementById('complaintsList');
  if (!complaints || complaints.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" class="table-empty">No complaints found.</td></tr>';
    return;
  }
  
  tbody.innerHTML = complaints.map(c => {
    const d = new Date(c.created_at).toLocaleString();
    return `
      <tr>
        <td>${d}</td>
        <td><strong>${escapeHtml(c.caller_name || 'Unknown')}</strong><br><small>${escapeHtml(c.contact_number || '')}</small></td>
        <td><span class="badge badge-dept">${escapeHtml(c.service_category || '-')}</span></td>
        <td style="min-width: 180px;"><strong>${escapeHtml(c.specific_service || '-')}</strong><br><small style="color: var(--text3);">${escapeHtml(c.location_address || '')}</small></td>
        <td>
          <div class="complaint-desc-cell" title="${escapeHtml(c.description || 'No description provided')}">
            ${escapeHtml(c.description || '-')}
          </div>
        </td>
        <td><span class="badge ${c.status === 'Open' ? 'badge-err' : 'badge-ok'}">${escapeHtml(c.status)}</span></td>
      </tr>
    `;
  }).join('');
}

document.addEventListener('DOMContentLoaded', () => {
  if(document.getElementById('refreshComplaintsBtn')) {
    document.getElementById('refreshComplaintsBtn').addEventListener('click', fetchComplaints);
    document.getElementById('complaintCategoryFilter').addEventListener('change', fetchComplaints);
  }
});
