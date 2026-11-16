let csrfToken;

async function api(path, method = 'GET') {
  if (method !== 'GET' && !csrfToken) {
    const response = await fetch('/api/csrf', { credentials: 'same-origin' });
    if (!response.ok) throw new Error('Secure action unavailable. Refresh this page.');
    csrfToken = (await response.json()).token;
  }
  const response = await fetch(path, {
    method, credentials: 'same-origin',
    headers: method === 'GET' ? {} : { 'X-CSRF-Token': csrfToken },
  });
  if (!response.ok) throw new Error('The action could not be completed.');
  return response.json();
}

function message(text, error = false) {
  const node = document.querySelector('#message');
  node.textContent = text;
  node.dataset.error = error ? 'true' : 'false';
  node.hidden = false;
}

function element(tag, text) {
  const node = document.createElement(tag);
  node.textContent = text;
  return node;
}

async function loadAssignments() {
  const assignments = await api('/api/admin/assignments');
  const list = document.querySelector('#assignment-list');
  list.replaceChildren();
  for (const assignment of assignments) {
    const row = element('article', '');
    row.className = 'record-row';
    row.append(
      element('strong', assignment.candidate_email),
      element('small', `${assignment.status.replaceAll('_', ' ')} · invitation email: ${assignment.mail_status}`),
      element('small', `Start: ${assignment.scheduled_start_at} · Latest start: ${assignment.latest_start_at} (${assignment.timezone})`),
    );
    const inspect = element('button', 'Inspect frozen plan');
    inspect.type = 'button'; inspect.className = 'secondary';
    const detail = document.createElement('details');
    inspect.addEventListener('click', async () => {
      try {
        const assigned = await api(`/api/admin/assignments/${assignment.id}`);
        const version = await api(`/api/admin/blueprint-versions/${assigned.blueprint_version_id}`);
        detail.replaceChildren(element('summary', `${version.content.title} · version ${version.version}`));
        const questions = document.createElement('ol');
        version.content.questions.forEach((question) => questions.append(element('li', question.question)));
        detail.append(questions); detail.open = true;
      } catch (error) { message(error.message, true); }
    });
    row.append(inspect, detail);
    if (assignment.status === 'assigned') {
      const resend = element('button', 'Resend invitation');
      resend.type = 'button'; resend.className = 'secondary';
      resend.addEventListener('click', async () => {
        try {
          const result = await api(`/api/admin/assignments/${assignment.id}/resend`, 'POST');
          message(`Invitation email: ${result.mail_status}.`); await loadAssignments();
        } catch (error) { message(error.message, true); }
      });
      const cancel = element('button', 'Cancel assignment');
      cancel.type = 'button'; cancel.className = 'secondary';
      cancel.addEventListener('click', async () => {
        if (!confirm('Cancel this unstarted assignment?')) return;
        try {
          await api(`/api/admin/assignments/${assignment.id}/cancel`, 'POST');
          message('Assignment cancelled.'); await loadAssignments();
        } catch (error) { message(error.message, true); }
      });
      row.append(resend, cancel);
    }
    list.append(row);
  }
  if (!assignments.length) list.append(element('p', 'No existing assignments. New interview scheduling is being updated.'));
}

document.querySelector('#logout').addEventListener('click', async () => {
  try { await api('/api/auth/logout', 'POST'); location.assign('/login'); }
  catch (error) { message(error.message, true); }
});
loadAssignments().catch((error) => message(error.message, true));
