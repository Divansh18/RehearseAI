let csrfToken = null;
let currentPage = 1;

function show(message) {
  const target = document.querySelector('#message');
  target.textContent = message;
  target.hidden = false;
  target.dataset.error = 'true';
}

async function getJson(path) {
  const response = await fetch(path, { credentials: 'same-origin' });
  if (!response.ok) throw new Error(response.status === 403 ? 'Access denied.' : 'Could not load interview records.');
  return response.json();
}

async function csrf() {
  csrfToken = (await getJson('/api/csrf')).token;
}

async function securePost(path) {
  if (!csrfToken) await csrf();
  const response = await fetch(path, {
    method: 'POST', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
    body: '{}',
  });
  if (!response.ok) throw new Error('The request could not be completed.');
  return response.status === 204 ? null : response.json();
}

function utc(value) {
  if (!value) return 'Not recorded';
  return new Date(value).toLocaleString(undefined, {
    timeZone: 'UTC', timeZoneName: 'short', year: 'numeric', month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit',
  });
}

function label(value) {
  return value.replaceAll('_', ' ').replace(/^./, (character) => character.toUpperCase());
}

function item(tag, text, className = '') {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  return node;
}

function renderList(data, admin) {
  const container = document.querySelector('#records');
  container.replaceChildren();
  document.querySelector('#record-empty').hidden = data.items.length !== 0;
  data.items.forEach((record) => {
    const row = item('article', '', 'record-row');
    const link = item('a', `${utc(record.started_at || record.created_at)} — ${label(record.status)}`);
    link.href = `${admin ? '/admin' : ''}/interviews/${record.id}`;
    row.append(link);
    if (admin) row.append(item('small', record.candidate_email));
    if (record.status === 'interrupted' || record.status === 'failed') {
      row.append(item('small', 'Partial conversation'));
    }
    container.append(row);
  });
  document.querySelector('#page-label').textContent = `${data.total} total · Page ${data.page}`;
  document.querySelector('#previous').hidden = data.page <= 1;
  document.querySelector('#next').hidden = data.page * data.page_size >= data.total;
}

async function loadList() {
  const admin = location.pathname.startsWith('/admin');
  const params = new URLSearchParams({ page_number: String(currentPage) });
  if (admin) {
    const email = document.querySelector('#email-filter').value.trim();
    const status = document.querySelector('#status-filter').value;
    if (email) params.set('candidate_email', email);
    if (status) params.set('status', status);
  }
  renderList(await getJson(`/api/${admin ? 'admin/' : ''}interviews?${params}`), admin);
}

async function loadDetail() {
  const admin = location.pathname.startsWith('/admin');
  const id = location.pathname.split('/').at(-1);
  const base = `/api/${admin ? 'admin/' : ''}interviews/${id}`;
  const [record, transcript] = await Promise.all([getJson(base), getJson(`${base}/transcript`)]);
  document.querySelector('#record-date').textContent = `Started: ${utc(record.started_at)} · Ended: ${utc(record.ended_at)}`;
  document.querySelector('#record-status').textContent = `Status: ${label(record.status)}${record.termination_reason ? ` (${label(record.termination_reason)})` : ''}`;
  if (record.candidate_email) document.querySelector('#record-candidate').textContent = record.candidate_email;
  const conversation = document.querySelector('#conversation');
  conversation.replaceChildren();
  transcript.forEach((entry) => {
    const block = item('article', '', 'conversation-item');
    block.append(item('strong', entry.speaker === 'candidate' ? 'You' : 'RehearseAI'));
    block.append(item('small', `${utc(entry.timestamp_utc)} · ${label(entry.status)}`));
    block.append(item('p', entry.text));
    conversation.append(block);
  });
  if (!transcript.length) conversation.append(item('p', 'No final conversation text is saved yet.', 'muted'));
  const state = document.querySelector('#pdf-state');
  const download = document.querySelector('#pdf-link');
  const regenerate = document.querySelector('#pdf-regenerate');
  if (['starting', 'in_progress'].includes(record.status)) {
    state.textContent = 'Available after the interview ends.';
  } else if (record.pdf_status === 'available') {
    state.textContent = 'Ready to download.';
    download.href = `${base}/pdf`;
    download.hidden = false;
  } else if (record.pdf_status === 'failed') {
    state.textContent = 'PDF generation failed. The saved conversation is still available.';
    regenerate.hidden = false;
    regenerate.addEventListener('click', async () => {
      try { await securePost(`${base}/pdf/regenerate`); state.textContent = 'PDF regeneration queued. Refresh shortly.'; regenerate.hidden = true; }
      catch (error) { show(error.message); }
    });
  } else {
    state.textContent = 'PDF generation pending. Refresh shortly.';
  }
}

async function main() {
  try {
    const me = await getJson('/api/auth/me');
    const identity = document.querySelector('#candidate-email');
    if (identity) identity.textContent = me.email;
    const adminLink = document.querySelector('#admin-link');
    if (adminLink && me.role === 'admin') adminLink.hidden = false;
    if (location.pathname.endsWith('/dashboard') || location.pathname === '/admin/interviews') {
      await loadList();
      document.querySelector('#previous').addEventListener('click', async () => { currentPage -= 1; await loadList(); });
      document.querySelector('#next').addEventListener('click', async () => { currentPage += 1; await loadList(); });
      document.querySelector('#filters')?.addEventListener('submit', async (event) => {
        event.preventDefault(); currentPage = 1; await loadList();
      });
    } else await loadDetail();
  } catch (error) { show(error.message); }
  document.querySelector('#logout').addEventListener('click', async () => {
    try { await securePost('/api/auth/logout'); location.assign('/login'); }
    catch (error) { show(error.message); }
  });
}

main();
