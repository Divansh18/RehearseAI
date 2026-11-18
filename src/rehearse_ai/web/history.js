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

async function loadAssignmentOverview() {
  const assignments = await getJson('/api/admin/assignments');
  const container = document.querySelector('#assignment-overview');
  container.replaceChildren();
  assignments.slice(0, 5).forEach((assignment) => {
    const row = item('div', '', 'record-row');
    row.append(item('strong', assignment.candidate_email));
    row.append(item('small', `${label(assignment.status)} · questions: ${assignment.preparation_status} · invitation: ${assignment.mail_status}`));
    container.append(row);
  });
  if (!assignments.length) container.append(item('p', 'No interviews scheduled yet. Use Schedule interview to create one.', 'muted'));
}

function renderCoverage(coverage) {
  const card = document.querySelector('#coverage-card');
  if (!coverage || !card) return;
  const numbers = (list) => (list && list.length ? list.join(', ') : 'none');
  const minutes = (seconds) => `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
  const lines = [
    `Questions planned: ${coverage.planned}`,
    `Asked: ${numbers(coverage.asked)}`,
    `Covered by an earlier answer: ${numbers(coverage.covered_from_prior_turns)}`,
    `Skipped to respect the time budget: ${numbers(coverage.skipped_for_time)}`,
    `Answered by not knowing or declining: ${numbers(coverage.no_knowledge_or_declined)}`,
    `Required questions not reached: ${numbers(coverage.required_not_reached)}`,
    `Optional questions not reached: ${numbers(coverage.optional_not_reached)}`,
    `Active time ${minutes(coverage.active_seconds || 0)} of a ${minutes(coverage.target_seconds || 0)} target${coverage.paused_seconds ? ` (${minutes(coverage.paused_seconds)} paused, not counted)` : ''}`,
    `Ended: ${label(coverage.end_reason || 'unknown')}`,
  ];
  const list = document.querySelector('#coverage-list');
  list.replaceChildren(...lines.map((text) => item('li', text)));
  card.hidden = false;
}

async function loadDetail() {
  const admin = location.pathname.startsWith('/admin');
  const id = location.pathname.split('/').at(-1);
  const base = `/api/${admin ? 'admin/' : ''}interviews/${id}`;
  const record = await getJson(base);
  document.querySelector('#record-date').textContent = `Started: ${utc(record.started_at)} · Ended: ${utc(record.ended_at)}`;
  document.querySelector('#record-status').textContent = `Status: ${label(record.status)}${record.termination_reason ? ` (${label(record.termination_reason)})` : ''}`;
  if (record.candidate_email) document.querySelector('#record-candidate').textContent = record.candidate_email;
  if (!admin) {
    const transcript = await getJson(`${base}/transcript`);
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
    return;
  }
  renderCoverage(record.coverage);
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
    state.textContent = 'Generation failed. The saved conversation remains available for regeneration.';
    regenerate.hidden = false;
    regenerate.addEventListener('click', async () => {
      try { await securePost(`${base}/pdf/regenerate`); state.textContent = 'Conversation PDF regeneration queued. Refresh shortly.'; regenerate.hidden = true; }
      catch (error) { show(error.message); }
    });
  } else {
    state.textContent = 'Preparing the Conversation PDF. Refresh shortly.';
  }
}

async function main() {
  try {
    const me = await getJson('/api/auth/me');
    const identity = document.querySelector('#candidate-email');
    if (identity) identity.textContent = me.email;
    const adminLink = document.querySelector('#admin-link');
    if (adminLink && me.role === 'admin') adminLink.hidden = false;
    if (location.pathname === '/admin/interviews') {
      await loadAssignmentOverview();
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
