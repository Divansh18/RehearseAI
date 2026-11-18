let csrfToken;

async function csrf() {
  const response = await fetch('/api/csrf', { credentials: 'same-origin' });
  if (!response.ok) throw new Error('Could not prepare the secure form. Refresh this page.');
  csrfToken = (await response.json()).token;
}

async function post(path, body) {
  if (!csrfToken) await csrf();
  const response = await fetch(path, {
    method: 'POST', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    let detail = 'Request unavailable. Try again later.';
    try { detail = (await response.json()).detail || detail; } catch { /* No JSON body. */ }
    throw new Error(typeof detail === 'string' ? detail : 'Check your entry and try again.');
  }
  return response.json();
}

function showError(error) {
  const message = document.querySelector('#message');
  message.textContent = error.message;
  message.dataset.error = 'true';
  message.hidden = false;
}

function time(value, timezone) {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium', timeStyle: 'short', timeZone: timezone,
  }).format(new Date(value));
}

async function invitationPage() {
  const token = location.pathname.split('/').at(-1);
  const button = document.querySelector('#redeem-invitation');
  try {
    const response = await fetch(`/api/invitations/${encodeURIComponent(token)}`);
    if (!response.ok) throw new Error('This invitation is invalid, expired, or no longer available.');
    const data = await response.json();
    document.querySelector('#invitation-detail').textContent =
      `${data.title}. Scheduled ${time(data.scheduled_start_at, data.timezone)} ${data.timezone}. ` +
      `Begin by ${time(data.latest_start_at, data.timezone)} ${data.timezone}. ` +
      (data.target_minutes ? `Estimated duration: ${data.target_minutes} minutes. ` : '') +
      'You will need a camera, microphone, speakers, and screen sharing.';
    button.disabled = false;
  } catch (error) { showError(error); return; }
  button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      await post(`/api/invitations/${encodeURIComponent(token)}/redeem`, {});
      history.replaceState(null, '', '/join');
      location.replace('/candidate/welcome');
    } catch (error) { showError(error); button.disabled = false; }
  });
}

async function welcomePage() {
  try {
    const response = await fetch('/api/candidate/interview', { credentials: 'same-origin' });
    if (!response.ok) throw new Error('Your invitation session is unavailable. Reopen the private link.');
    const data = await response.json();
    document.querySelector('#welcome-title').textContent = data.title;
    document.querySelector('#welcome-detail').textContent =
      `${data.candidate_instructions || 'Please complete your device checks before beginning.'} ` +
      `Scheduled ${time(data.scheduled_start_at, data.timezone)} ${data.timezone}; ` +
      `latest start ${time(data.latest_start_at, data.timezone)} ${data.timezone}.`;
    document.querySelector('#full-name').value = data.self_reported_name || data.candidate_name || '';
    if (data.status !== 'assigned') {
      document.querySelector('#name-form').hidden = true;
      throw new Error(data.status === 'completed' ? 'This interview is complete.' :
        'This interview can no longer be started. Contact the interviewer.');
    }
    const note = (text) => {
      const node = document.querySelector('#message');
      node.textContent = text; node.dataset.error = 'false'; node.hidden = false;
    };
    if (data.plan_state === 'preparing') {
      note('Your interviewer is still preparing your interview. You can confirm your name and check your devices now; Begin unlocks automatically when it is ready.');
    } else if (data.plan_state === 'unavailable') {
      note('Your interview is not ready to begin. Please contact the interviewer.');
    } else if (data.window_state === 'too_early') {
      note('You can confirm your name now. Begin will be available when the joining window opens.');
    }
    if (data.window_state === 'closed') {
      document.querySelector('#name-form').hidden = true;
      throw new Error('The joining window has closed. Contact the interviewer.');
    }
  } catch (error) { showError(error); return; }
  document.querySelector('#name-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = event.currentTarget.querySelector('button');
    button.disabled = true;
    try {
      await post('/api/candidate/name', {
        full_name: document.querySelector('#full-name').value.trim(),
      });
      document.querySelector('#setup-link').hidden = false;
      document.querySelector('#setup-link').focus();
      button.textContent = 'Name confirmed';
    } catch (error) { showError(error); button.disabled = false; }
  });
}

if (location.pathname.startsWith('/join/')) invitationPage();
else if (location.pathname === '/candidate/welcome') welcomePage();
