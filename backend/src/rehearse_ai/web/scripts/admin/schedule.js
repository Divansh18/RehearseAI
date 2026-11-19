const form = document.querySelector('#schedule-form');
const submit = document.querySelector('#submit');
const summary = document.querySelector('#form-errors');
const FIELD_NAMES = [
  'candidate_name', 'candidate_email', 'resume', 'job_description', 'interviewer_instructions',
  'scheduled_local', 'timezone', 'target_minutes', 'join_window_minutes',
];
const MAX_BYTES = 5 * 1024 * 1024;
let csrfToken = null;

function newKey() {
  if (globalThis.crypto?.randomUUID) return crypto.randomUUID().replaceAll('-', '');
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
}
// One key per form load: a double click or a retry after a lost response never schedules twice.
let idempotencyKey = newKey();

function pad(value) { return String(value).padStart(2, '0'); }
function localInputValue(date) {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function setError(field, message) {
  const node = document.querySelector(`#err-${field}`);
  if (!node) return false;
  node.textContent = message || '';
  node.hidden = !message;
  const input = document.querySelector(`#${field}`);
  if (input) input.setAttribute('aria-invalid', message ? 'true' : 'false');
  return true;
}

function clearErrors() {
  FIELD_NAMES.forEach((name) => setError(name, ''));
  summary.hidden = true;
  summary.replaceChildren();
}

function showErrors(errors) {
  summary.replaceChildren();
  const heading = document.createElement('strong');
  heading.textContent = errors.length === 1 ? 'Please fix this field:' : 'Please fix these fields:';
  const list = document.createElement('ul');
  const targets = { job_description: 'job_description_text', form: 'candidate_name' };
  errors.forEach(({ field, message }) => {
    setError(field, message);
    const item = document.createElement('li');
    const link = document.createElement('a');
    link.href = `#${targets[field] || field}`;
    link.textContent = message;
    item.append(link);
    list.append(item);
  });
  summary.append(heading, list);
  summary.hidden = false;
  summary.focus();
}

function currentZone() {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'; } catch { return 'UTC'; }
}

function zoneIsKnown(zone) {
  try { new Intl.DateTimeFormat(undefined, { timeZone: zone }); return true; } catch { return false; }
}

function updateSummary() {
  const start = document.querySelector('#scheduled_local').value;
  const zone = document.querySelector('#timezone').value.trim();
  const windowMinutes = Number(document.querySelector('#join_window_minutes').value) || 30;
  const hint = document.querySelector('#tz-hint');
  const output = document.querySelector('#window-summary');
  if (!zone || !zoneIsKnown(zone)) {
    hint.textContent = zone ? 'Unrecognised timezone. Use a name like Asia/Kolkata or Europe/London.' : '';
    output.textContent = '';
    return;
  }
  const label = new Intl.DateTimeFormat('en', { timeZone: zone, timeZoneName: 'short' })
    .formatToParts(new Date()).find((part) => part.type === 'timeZoneName')?.value || zone;
  hint.textContent = `Times are in ${zone} (${label} now).`;
  if (!start) { output.textContent = ''; return; }
  const [date, time] = start.split('T');
  const [hours, minutes] = time.split(':').map(Number);
  const end = new Date(Date.UTC(2000, 0, 1, hours, minutes + windowMinutes));
  const endTime = `${pad(end.getUTCHours())}:${pad(end.getUTCMinutes())}`;
  const sameDay = end.getUTCDate() === 1;
  output.textContent = `The candidate can begin from ${time} until ${endTime}${sameDay ? '' : ' (next day)'} on ${date} (${zone}). Daylight-saving ambiguities are checked when you save.`;
}

function toggleJobMode() {
  const paste = form.jd_mode.value === 'paste';
  document.querySelector('#jd-paste').hidden = !paste;
  document.querySelector('#jd-file').hidden = paste;
}

function clientChecks() {
  const errors = [];
  const need = (field, value, message) => { if (!value) errors.push({ field, message }); };
  need('candidate_name', form.candidate_name.value.trim().length >= 2, "Enter the candidate's full name.");
  need('candidate_email', /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(form.candidate_email.value.trim()), 'Enter a valid email address.');
  const resume = form.resume.files[0];
  need('resume', resume, "Upload the candidate's résumé (PDF or DOCX).");
  if (resume) {
    need('resume', /\.(pdf|docx)$/i.test(resume.name), 'The résumé must be a PDF or DOCX file.');
    need('resume', resume.size <= MAX_BYTES, 'The résumé is larger than 5 MB.');
  }
  if (form.jd_mode.value === 'paste') {
    need('job_description', form.job_description_text.value.trim(), 'Paste the job description or switch to uploading a file.');
  } else {
    const file = form.job_description_file.files[0];
    need('job_description', file, 'Choose a job description file (PDF or DOCX).');
    if (file) {
      need('job_description', /\.(pdf|docx)$/i.test(file.name), 'The job description must be a PDF or DOCX file.');
      need('job_description', file.size <= MAX_BYTES, 'The job description file is larger than 5 MB.');
    }
  }
  need('scheduled_local', form.scheduled_local.value, 'Choose the interview date and time.');
  need('timezone', form.timezone.value.trim() && zoneIsKnown(form.timezone.value.trim()), 'Enter a valid timezone such as Europe/London.');
  const minutes = Number(form.target_minutes.value);
  need('target_minutes', Number.isInteger(minutes) && minutes >= 5 && minutes <= 120, 'Duration must be 5 to 120 minutes.');
  const window = Number(form.join_window_minutes.value);
  need('join_window_minutes', Number.isInteger(window) && window >= 5 && window <= 1440, 'The joining window must be 5 to 1440 minutes.');
  return errors;
}

async function getCsrf() {
  if (!csrfToken) {
    const response = await fetch('/api/csrf', { credentials: 'same-origin' });
    if (!response.ok) throw new Error('Could not prepare a secure request. Refresh the page.');
    csrfToken = (await response.json()).token;
  }
  return csrfToken;
}

function readableServerError(status, body) {
  if (status === 401 || status === 403) return [{ field: 'form', message: 'Your session has expired or the request was rejected. Sign in again.' }];
  if (status === 413) return [{ field: 'form', message: 'The upload is too large. Each file may be at most 5 MB.' }];
  if (status === 429) return [{ field: 'form', message: 'Too many attempts. Wait a little and try again.' }];
  if (Array.isArray(body?.detail)) return body.detail;
  return [{ field: 'form', message: 'The interview could not be scheduled. Try again.' }];
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  clearErrors();
  const errors = clientChecks();
  if (errors.length) { showErrors(errors); return; }
  submit.disabled = true;
  submit.textContent = 'Scheduling…';
  try {
    const data = new FormData();
    for (const name of ['candidate_name', 'candidate_email', 'interviewer_instructions', 'scheduled_local', 'timezone', 'target_minutes', 'join_window_minutes']) {
      data.append(name, form[name].value.trim());
    }
    data.append('resume', form.resume.files[0]);
    if (form.jd_mode.value === 'paste') data.append('job_description_text', form.job_description_text.value);
    else data.append('job_description_file', form.job_description_file.files[0]);
    data.append('idempotency_key', idempotencyKey);
    const response = await fetch('/api/admin/schedule', {
      method: 'POST', credentials: 'same-origin', headers: { 'X-CSRF-Token': await getCsrf() }, body: data,
    });
    let body = null;
    try { body = await response.json(); } catch { /* not JSON */ }
    if (!response.ok) { showErrors(readableServerError(response.status, body)); return; }
    idempotencyKey = newKey();
    location.assign(`/admin/assignments?scheduled=${encodeURIComponent(body.assignment_id)}`);
  } catch (error) {
    showErrors([{ field: 'form', message: error.message || 'Network error. Check your connection and try again.' }]);
  } finally {
    submit.disabled = false;
    submit.textContent = 'Schedule interview';
  }
});

form.addEventListener('input', updateSummary);
form.addEventListener('change', (event) => { if (event.target.name === 'jd_mode') toggleJobMode(); updateSummary(); });
document.querySelector('#logout').addEventListener('click', async () => {
  try {
    const response = await fetch('/api/auth/logout', {
      method: 'POST', credentials: 'same-origin', headers: { 'X-CSRF-Token': await getCsrf() },
    });
    if (response.ok) location.assign('/login');
  } catch { /* leave the page as it is */ }
});

(function initialise() {
  const zone = currentZone();
  document.querySelector('#timezone').value = zone;
  try {
    const zones = Intl.supportedValuesOf?.('timeZone') || [];
    document.querySelector('#timezones').replaceChildren(...zones.map((name) => {
      const option = document.createElement('option'); option.value = name; return option;
    }));
  } catch { /* datalist is optional */ }
  const soon = new Date(Date.now() + 60 * 60 * 1000);
  soon.setMinutes(soon.getMinutes() < 30 ? 30 : 60, 0, 0);
  document.querySelector('#scheduled_local').value = localInputValue(soon);
  toggleJobMode();
  updateSummary();
})();
