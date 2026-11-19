let csrfToken;
const POLL_MS = 4000;
const cards = new Map();
let pollTimer = null;

async function api(path, method = 'GET', body = null) {
  const headers = {};
  if (method !== 'GET') {
    if (!csrfToken) {
      const token = await fetch('/api/csrf', { credentials: 'same-origin' });
      if (!token.ok) throw new Error('Secure action unavailable. Refresh this page.');
      csrfToken = (await token.json()).token;
    }
    headers['X-CSRF-Token'] = csrfToken;
  }
  if (body) headers['Content-Type'] = 'application/json';
  const response = await fetch(path, {
    method, credentials: 'same-origin', headers, body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) {
    let detail = '';
    try { detail = (await response.json()).detail; } catch { /* no body */ }
    throw new Error(typeof detail === 'string' && detail ? detail : 'The action could not be completed.');
  }
  return response.status === 204 ? null : response.json();
}

function message(text, error = false) {
  const node = document.querySelector('#message');
  node.textContent = text;
  node.dataset.error = error ? 'true' : 'false';
  node.hidden = false;
}

function element(tag, text = '', className = '') {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  return node;
}

function badge(text, tone) {
  const node = element('span', text, 'badge');
  node.dataset.tone = tone;
  return node;
}

const PREPARATION = {
  pending: ['Questions queued', 'warn'], preparing: ['Preparing questions…', 'warn'],
  ready: ['Questions ready', 'good'], failed: ['Question preparation failed', 'bad'],
};
const MAIL = {
  pending: ['Invitation queued', 'warn'], sending: ['Sending invitation…', 'warn'],
  sent: ['Invitation sent', 'good'], failed: ['Invitation not sent', 'bad'],
};
const ERRORS = {
  model_error: 'The AI service could not be reached or returned an unusable answer.',
  plan_invalid: 'The generated questions did not pass checks (for example, a résumé claim could not be verified).',
  timeout: 'Preparation took too long.',
  internal_error: 'An unexpected error occurred.',
  lease_expired: 'Preparation stopped unexpectedly.',
  missing_inputs: 'The saved résumé or job description could not be found.',
  smtp_failed: 'The email service did not accept the message.',
};
const STALE = new Set(['stale_revision', 'plan_changed', 'assignment_started_or_cancelled', 'assignment_unavailable']);

function label(value) { return value.replaceAll('_', ' ').replace(/^./, (c) => c.toUpperCase()); }

function whenText(value) {
  const match = value.match(/^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/);
  return match ? `${match[1]} ${match[2]}` : value;
}

function isBusy(a) {
  return ['pending', 'preparing'].includes(a.preparation_status)
    || ['pending', 'sending'].includes(a.mail_status)
    || ['queued', 'running'].includes(a.revision?.status);
}

function buildCard(a) {
  const card = element('article', '', 'assignment-card');
  card.dataset.id = a.id;
  const parts = {
    title: element('h3'), status: element('div', '', 'status-row'), when: element('small'),
    problem: element('small', '', 'field-error'), actions: element('div', '', 'assignment-actions'),
    detail: element('div'),
  };
  card.append(parts.title, parts.status, parts.when, parts.problem, parts.actions, parts.detail);
  card.parts = parts;
  return card;
}

function action(label_, handler, className = 'secondary') {
  const button = element('button', label_, className);
  button.type = 'button';
  button.addEventListener('click', async () => {
    button.disabled = true;
    try { await handler(button); } catch (error) { message(error.message, true); } finally { button.disabled = false; }
  });
  return button;
}

async function showPlan(card, a) {
  const { detail } = card.parts;
  const existing = detail.querySelector('.plan-view');
  if (existing) { existing.remove(); card.dataset.planOpen = 'false'; return; }
  const data = await api(`/api/admin/assignments/${a.id}`);
  const version = await api(`/api/admin/blueprint-versions/${data.blueprint_version_id}`);
  const view = element('div', '', 'plan-view');
  view.append(element('strong', `${version.content.title} · version ${version.version}`));
  view.append(element('p', version.content.purpose, 'muted'));
  view.append(element('p', `Private to administrators. Candidates never see questions, guidance or notes. ${version.content.target_minutes} minute target.`, 'private-note'));
  const list = document.createElement('ol');
  version.content.questions.forEach((q) => {
    const item = document.createElement('li');
    item.append(element('p', q.question));
    item.append(element('p', `${label(q.priority || (q.required ? 'must' : 'should'))} priority · ${q.max_probes} follow-up${q.max_probes === 1 ? '' : 's'}${q.budget_seconds ? ` · about ${Math.round(q.budget_seconds / 60 * 10) / 10} min` : ''}${q.source && q.source !== 'general' ? ` · from ${q.source === 'both' ? 'résumé and job description' : q.source === 'resume' ? 'résumé' : 'job description'}` : ''}`, 'private-note'));
    if (q.basis) item.append(element('p', `Why: ${q.basis}`, 'private-note'));
    if (q.follow_up_guidance) item.append(element('p', `Listen for: ${q.follow_up_guidance.replace(/^listen for\s*/i, '')}`, 'private-note'));
    list.append(item);
  });
  view.append(list);
  detail.prepend(view);
  card.dataset.planOpen = 'true';
  card.dataset.planVersion = a.blueprint_version_id || '';
}

function showRevise(card, a) {
  const { detail } = card.parts;
  const existing = detail.querySelector('.revise-box');
  if (existing) { existing.remove(); return; }
  const box = element('div', '', 'revise-box');
  const field = document.createElement('label');
  field.textContent = 'Describe the change you want, in plain words';
  field.htmlFor = `revise-${a.id}`;
  const input = document.createElement('textarea');
  input.id = `revise-${a.id}`; input.rows = 3; input.maxLength = 1000;
  input.placeholder = 'For example: add a question about testing, and make the closing question about future goals.';
  const send = action('Apply changes', async () => {
    const text = input.value.trim();
    if (text.length < 3) throw new Error('Describe the change in a few words.');
    await api(`/api/admin/assignments/${a.id}/revise`, 'POST', { request: text });
    box.remove();
    notice('Change request queued. The current questions stay in use until the new version is ready.');
    refresh();
  }, 'primary');
  box.append(field, input, send);
  detail.append(box);
  input.focus();
}

function notice(text) {
  const node = document.querySelector('#notice');
  node.textContent = text;
  node.hidden = false;
}

function updateCard(card, a) {
  const { title, status, when, problem, actions } = card.parts;
  title.textContent = a.candidate_name ? `${a.candidate_name} · ${a.candidate_email}` : a.candidate_email;
  const prep = PREPARATION[a.preparation_status] || [label(a.preparation_status), 'warn'];
  const mail = MAIL[a.mail_status] || [label(a.mail_status), 'warn'];
  const interview = ['assigned'].includes(a.status) ? ['Awaiting candidate', 'warn']
    : ['completed'].includes(a.status) ? ['Completed', 'good']
    : ['cancelled', 'failed', 'expired'].includes(a.status) ? [label(a.status), 'bad']
    : [label(a.status), 'warn'];
  status.replaceChildren(badge(interview[0], interview[1]), badge(prep[0], prep[1]), badge(mail[0], mail[1]));
  if (a.revision && ['queued', 'running'].includes(a.revision.status)) status.append(badge('Applying requested changes…', 'warn'));
  when.textContent = `Start ${whenText(a.scheduled_start_at)} · latest start ${whenText(a.latest_start_at)} (${a.timezone})`;
  const notes = [];
  if (a.preparation_status === 'failed') notes.push(ERRORS[a.preparation_error] || 'Question preparation failed.');
  if (a.mail_status === 'failed') notes.push('The invitation email was not delivered. Use Resend invitation.');
  if (a.revision?.status === 'failed') notes.push(`The last change request failed (${ERRORS[a.revision.error] || 'unknown reason'}). The previous questions are unchanged.`);
  if (a.revision?.status === 'cancelled' && STALE.has(a.revision.error)) notes.push('The last change request was not applied because the interview had already started or changed.');
  problem.textContent = notes.join(' ');
  problem.hidden = notes.length === 0;
  const signature = [a.status, a.preparation_status, a.mail_status, a.blueprint_version_id, a.revision?.status].join('|');
  if (card.dataset.signature !== signature) {
    card.dataset.signature = signature;
    actions.replaceChildren();
    if (a.blueprint_version_id) actions.append(action('Review questions', () => showPlan(card, a)));
    const unstarted = a.status === 'assigned';
    if (unstarted && a.preparation_status === 'ready' && !['queued', 'running'].includes(a.revision?.status)) {
      actions.append(action('Request changes', () => showRevise(card, a)));
    }
    if (unstarted && a.preparation_status === 'failed') {
      actions.append(action('Retry preparation', async () => {
        await api(`/api/admin/assignments/${a.id}/retry-preparation`, 'POST');
        notice('Preparation restarted.'); refresh();
      }, 'primary'));
    }
    if (unstarted) {
      actions.append(action('Resend invitation', async () => {
        const result = await api(`/api/admin/assignments/${a.id}/resend`, 'POST');
        notice(`Invitation email: ${result.mail_status}.`); refresh();
      }));
      let armed = null;
      actions.append(action('Cancel interview', async (button) => {
        if (!armed) {
          button.textContent = 'Click again to confirm cancel';
          armed = setTimeout(() => { armed = null; button.textContent = 'Cancel interview'; }, 5000);
          return;
        }
        clearTimeout(armed);
        await api(`/api/admin/assignments/${a.id}/cancel`, 'POST');
        notice('Interview cancelled.'); refresh();
      }, 'danger'));
    }
    if (card.dataset.planOpen === 'true' && card.dataset.planVersion !== (a.blueprint_version_id || '')) {
      card.parts.detail.querySelector('.plan-view')?.remove();
      card.dataset.planOpen = 'false';
      if (a.blueprint_version_id) showPlan(card, a).catch(() => {});
    }
  }
}

async function refresh() {
  clearTimeout(pollTimer);
  let busy = false;
  try {
    const assignments = await api('/api/admin/assignments');
    const list = document.querySelector('#assignment-list');
    const seen = new Set();
    assignments.forEach((a, index) => {
      seen.add(a.id);
      let card = cards.get(a.id);
      if (!card) { card = buildCard(a); cards.set(a.id, card); }
      updateCard(card, a);
      if (list.children[index] !== card) list.insertBefore(card, list.children[index] || null);
      busy = busy || isBusy(a);
    });
    for (const [id, card] of cards) if (!seen.has(id)) { card.remove(); cards.delete(id); }
    document.querySelector('#empty')?.remove();
    if (!assignments.length) {
      const empty = element('p', 'No interviews yet. Use Schedule interview to create the first one.', 'muted');
      empty.id = 'empty'; list.append(empty);
    }
  } catch (error) { message(error.message, true); }
  if (busy) pollTimer = setTimeout(refresh, POLL_MS);
}

document.querySelector('#logout').addEventListener('click', async () => {
  try { await api('/api/auth/logout', 'POST'); location.assign('/login'); }
  catch (error) { message(error.message, true); }
});
if (new URLSearchParams(location.search).has('scheduled')) {
  notice('Interview scheduled. The invitation is being emailed and the questions are being prepared. You can leave this page; nothing waits on it.');
  history.replaceState(null, '', location.pathname);
}
refresh();
