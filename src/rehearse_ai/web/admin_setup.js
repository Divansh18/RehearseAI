let csrfToken;

async function api(path, { method = 'GET', body } = {}) {
  if (method !== 'GET' && !csrfToken) {
    const csrfResponse = await fetch('/api/csrf', { credentials: 'same-origin' });
    if (!csrfResponse.ok) throw new Error('Secure form unavailable. Refresh this page.');
    csrfToken = (await csrfResponse.json()).token;
  }
  const response = await fetch(path, {
    method, credentials: 'same-origin',
    headers: method === 'GET' ? {} : {
      'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    let detail = 'The request could not be completed.';
    try { detail = (await response.json()).detail || detail; } catch { /* No JSON body. */ }
    throw new Error(typeof detail === 'string' ? detail : 'Check the form values.');
  }
  return response.status === 204 ? null : response.json();
}

function message(text, error = false) {
  const node = document.querySelector('#message');
  node.textContent = text;
  node.dataset.error = error ? 'true' : 'false';
  node.hidden = false;
}

function element(tag, text = '') {
  const node = document.createElement(tag);
  node.textContent = text;
  return node;
}

function questionRow(question = {}) {
  const row = element('fieldset');
  row.className = 'blueprint-question';
  const legend = element('legend', 'Core question');
  row.append(legend);
  const fields = [
    ['topic', 'Topic', 'text', question.topic || ''],
    ['question', 'Exact core question', 'text', question.question || ''],
    ['follow_up_guidance', 'Follow-up guidance', 'text', question.follow_up_guidance || ''],
    ['max_probes', 'Maximum probes (0–2)', 'number', String(question.max_probes ?? 1)],
  ];
  for (const [key, labelText, type, value] of fields) {
    const label = element('label', labelText);
    const input = document.createElement('input');
    input.dataset.field = key;
    input.type = type;
    input.value = value;
    if (key !== 'follow_up_guidance') input.required = true;
    if (key === 'max_probes') { input.min = '0'; input.max = '2'; }
    label.append(input);
    row.append(label);
  }
  const requiredLabel = element('label', 'Required question');
  const requiredInput = document.createElement('input');
  requiredInput.type = 'checkbox';
  requiredInput.dataset.field = 'required';
  requiredInput.checked = question.required !== false;
  requiredLabel.prepend(requiredInput);
  row.append(requiredLabel);
  const remove = element('button', 'Remove question');
  remove.type = 'button';
  remove.className = 'secondary';
  remove.addEventListener('click', () => row.remove());
  row.append(remove);
  document.querySelector('#question-editor').append(row);
}

function blueprintContent() {
  const questions = Array.from(document.querySelectorAll('.blueprint-question'), (row) => {
    const value = (field) => row.querySelector(`[data-field="${field}"]`);
    return {
      topic: value('topic').value.trim(), question: value('question').value.trim(),
      follow_up_guidance: value('follow_up_guidance').value.trim(),
      max_probes: Number(value('max_probes').value), required: value('required').checked,
    };
  });
  return {
    title: document.querySelector('#title').value.trim(),
    purpose: document.querySelector('#purpose').value.trim(),
    questions,
    focus_areas: document.querySelector('#focus-areas').value.split('\n').map((v) => v.trim()).filter(Boolean),
    target_minutes: Number(document.querySelector('#target-minutes').value),
    interviewer_style: document.querySelector('#interviewer-style').value.trim(),
    closing_instructions: document.querySelector('#closing-instructions').value.trim(),
  };
}

function populateBlueprint(item) {
  const draft = item.draft;
  document.querySelector('#blueprint-id').value = item.id;
  for (const key of ['title', 'purpose', 'target_minutes', 'interviewer_style', 'closing_instructions']) {
    document.querySelector(`#${key.replaceAll('_', '-')}`).value = draft[key];
  }
  document.querySelector('#focus-areas').value = draft.focus_areas.join('\n');
  document.querySelector('#question-editor').replaceChildren();
  draft.questions.forEach(questionRow);
  document.querySelector('#publish').disabled = false;
  scrollTo({ top: 0, behavior: 'smooth' });
}

async function loadBlueprints() {
  const drafts = await api('/api/admin/blueprints');
  const list = document.querySelector('#blueprint-list');
  list.replaceChildren();
  for (const item of drafts) {
    const row = element('div'); row.className = 'record-row';
    const button = element('button', `${item.draft.title} · Edit draft`);
    button.type = 'button'; button.className = 'secondary';
    button.addEventListener('click', () => populateBlueprint(item));
    row.append(button); list.append(row);
  }
  if (!drafts.length) list.append(element('p', 'No drafts yet.'));
}

async function blueprintPage() {
  questionRow();
  await loadBlueprints();
  document.querySelector('#add-question').addEventListener('click', () => questionRow());
  document.querySelector('#preview').addEventListener('click', () => {
    const draft = blueprintContent();
    const preview = document.querySelector('#blueprint-preview');
    preview.replaceChildren(element('h3', draft.title), element('p', draft.purpose));
    const list = document.createElement('ol');
    for (const question of draft.questions) {
      list.append(element('li', `${question.topic}: ${question.question} ` +
        `(${question.required ? 'required' : 'optional'}, up to ${question.max_probes} probes)`));
    }
    preview.append(list); preview.hidden = false;
  });
  document.querySelector('#blueprint-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    try {
      const id = document.querySelector('#blueprint-id').value;
      const saved = await api(id ? `/api/admin/blueprints/${id}` : '/api/admin/blueprints', {
        method: id ? 'PUT' : 'POST', body: blueprintContent(),
      });
      document.querySelector('#blueprint-id').value = saved.id;
      document.querySelector('#publish').disabled = false;
      message('Draft saved. Preview it, then publish when ready.');
      await loadBlueprints();
    } catch (error) { message(error.message, true); }
  });
  document.querySelector('#publish').addEventListener('click', async () => {
    try {
      const id = document.querySelector('#blueprint-id').value;
      const published = await api(`/api/admin/blueprints/${id}/publish`, {
        method: 'POST', body: {},
      });
      message(`Version ${published.version} published. Assign it on the Assignments page.`);
    } catch (error) { message(error.message, true); }
  });
}

function assignmentPayload() {
  const value = (id) => document.querySelector(`#${id}`).value.trim();
  return {
    blueprint_version_id: value('version'), candidate_email: value('candidate-email'),
    candidate_name: value('candidate-name') || null,
    focus_areas: value('candidate-focus').split('\n').map((part) => part.trim()).filter(Boolean),
    candidate_instructions: value('candidate-instructions'),
    scheduled_start_at: value('scheduled-start'), latest_start_at: value('latest-start'),
    timezone: value('timezone'),
  };
}

async function loadAssignments() {
  const assignments = await api('/api/admin/assignments');
  const list = document.querySelector('#assignment-list');
  list.replaceChildren();
  for (const item of assignments) {
    const row = element('article'); row.className = 'record-row';
    row.append(element('strong', `${item.candidate_email} · ${item.status}`),
      element('small', `Invitation email: ${item.mail_status} · ${item.timezone}`));
    if (item.status === 'assigned') {
      const edit = element('button', 'Edit or reschedule'); edit.type = 'button';
      edit.className = 'secondary';
      edit.addEventListener('click', async () => {
        const detail = await api(`/api/admin/assignments/${item.id}`);
        document.querySelector('#assignment-id').value = item.id;
        for (const [id, key] of [
          ['version', 'blueprint_version_id'], ['candidate-email', 'candidate_email'],
          ['candidate-name', 'candidate_name'], ['candidate-instructions', 'candidate_instructions'],
          ['timezone', 'timezone'], ['scheduled-start', 'scheduled_start_at'],
          ['latest-start', 'latest_start_at'],
        ]) document.querySelector(`#${id}`).value = detail[key] || '';
        document.querySelector('#candidate-focus').value = detail.focus_areas.join('\n');
        document.querySelector('#assignment-form button[type="submit"]').textContent =
          'Save assignment and send updated invitation';
        scrollTo({ top: 0, behavior: 'smooth' });
      });
      const resend = element('button', 'Resend invitation'); resend.type = 'button';
      resend.className = 'secondary';
      resend.addEventListener('click', async () => {
        try { const sent = await api(`/api/admin/assignments/${item.id}/resend`, {
          method: 'POST', body: {},
        }); message(`Invitation email: ${sent.mail_status}.`); await loadAssignments(); }
        catch (error) { message(error.message, true); }
      });
      const cancel = element('button', 'Cancel assignment'); cancel.type = 'button';
      cancel.className = 'secondary';
      cancel.addEventListener('click', async () => {
        if (!confirm('Cancel this unstarted assignment?')) return;
        try { await api(`/api/admin/assignments/${item.id}/cancel`, {
          method: 'POST', body: {},
        }); message('Assignment cancelled.'); await loadAssignments(); }
        catch (error) { message(error.message, true); }
      });
      row.append(edit, resend, cancel);
    }
    list.append(row);
  }
  if (!assignments.length) list.append(element('p', 'No assignments yet.'));
}

async function assignmentPage() {
  const versions = await api('/api/admin/blueprint-versions');
  const select = document.querySelector('#version');
  for (const version of versions) {
    const option = document.createElement('option'); option.value = version.id;
    option.textContent = `${version.content.title} · version ${version.version}`;
    select.append(option);
  }
  if (!versions.length) message('Publish a blueprint before assigning an interview.', true);
  await loadAssignments();
  document.querySelector('#assignment-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    try {
      const id = document.querySelector('#assignment-id').value;
      const created = await api(id ? `/api/admin/assignments/${id}` : '/api/admin/assignments', {
        method: id ? 'PUT' : 'POST', body: assignmentPayload(),
      });
      message(`Assignment ${id ? 'updated' : 'created'}. Invitation email: ${created.mail_status}.`);
      form.reset();
      document.querySelector('#assignment-id').value = '';
      document.querySelector('#assignment-form button[type="submit"]').textContent =
        'Create assignment and send invitation';
      await loadAssignments();
    } catch (error) { message(error.message, true); }
  });
}

document.querySelector('#logout').addEventListener('click', async () => {
  try { await api('/api/auth/logout', { method: 'POST', body: {} }); location.assign('/login'); }
  catch (error) { message(error.message, true); }
});
(location.pathname === '/admin/blueprints' ? blueprintPage() : assignmentPage())
  .catch((error) => message(error.message, true));
