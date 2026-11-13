let csrfToken = null;

async function csrf() {
  const response = await fetch('/api/csrf', { credentials: 'same-origin' });
  if (!response.ok) throw new Error('Could not start a secure form. Refresh the page.');
  csrfToken = (await response.json()).token;
}

async function post(path, body = {}) {
  if (!csrfToken) await csrf();
  const response = await fetch(path, {
    method: 'POST', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    let detail = 'The request could not be completed.';
    try { detail = (await response.json()).detail || detail; } catch { /* Keep generic error. */ }
    throw new Error(typeof detail === 'string' ? detail : 'Check the form and try again.');
  }
  return response.status === 204 ? null : response.json();
}

function show(message, error = false) {
  const target = document.querySelector('#message');
  target.textContent = message;
  target.dataset.error = error ? 'true' : 'false';
  target.hidden = false;
}

async function dashboard() {
  try {
    const response = await fetch('/api/auth/me', { credentials: 'same-origin' });
    if (!response.ok) { location.assign('/login'); return; }
    const account = await response.json();
    document.querySelector('#candidate-email').textContent = account.email;
    document.querySelector('#admin-link').hidden = account.role !== 'admin';
  } catch { show('Could not load your account. Refresh the page.', true); }
  document.querySelector('#logout').addEventListener('click', async () => {
    try { await post('/api/auth/logout'); location.assign('/login'); }
    catch (error) { show(error.message, true); }
  });
}

function account() {
  const route = location.pathname;
  const reason = new URLSearchParams(location.search).get('reason');
  const title = document.querySelector('#title');
  const intro = document.querySelector('#intro');
  const form = document.querySelector('#account-form');
  const email = document.querySelector('#email');
  const password = document.querySelector('#password');
  const submit = document.querySelector('#submit');
  document.querySelector('#forgot').hidden = route === '/reset-password';
  const hasToken = new URLSearchParams(location.search).get('token');
  if (hasToken) history.replaceState(null, '', location.pathname);
  const token = hasToken;
  const modes = {
    '/login': ['Sign in', 'Continue to your interview workspace.', 'Sign in'],
    '/signup': ['Create account', 'Create an account to access your interviews.', 'Create account'],
    '/verify': ['Verify email', 'A previously issued verification link may be used here.', 'Verify email'],
    '/reset-password': ['Reset password', 'Set a new password for your account.', 'Reset password'],
  };
  const [heading, copy, action] = modes[route] || modes['/login'];
  document.title = `RehearseAI — ${heading}`;
  document.querySelector(`.auth-nav a[href="${route}"]`)?.setAttribute('aria-current', 'page');
  title.textContent = heading;
  intro.textContent = copy;
  if (route === '/login' && reason === 'expired') {
    show('Your session expired. Sign in again to continue.');
  } else if (route === '/login' && reason === 'required') {
    show('Sign in to continue.');
  } else if (route === '/login' && reason === 'registered') {
    show('Account created. Sign in to continue.');
  }
  submit.textContent = action;
  email.hidden = route === '/verify' || route === '/reset-password';
  document.querySelector('#email-label').hidden = email.hidden;
  email.required = !email.hidden;
  password.hidden = route === '/verify';
  document.querySelector('#password-label').hidden = password.hidden;
  password.required = !password.hidden;
  password.autocomplete = route === '/signup' || route === '/reset-password'
    ? 'new-password' : 'current-password';
  if (route === '/verify' && !token) show('This verification link is missing a token.', true);
  if (route === '/reset-password' && !token) {
    title.textContent = 'Request password reset';
    intro.textContent = 'If the account exists, a reset link goes to the configured email service. Local development captures mail in Mailpit.';
    submit.textContent = 'Send reset link';
    password.hidden = true;
    document.querySelector('#password-label').hidden = true;
    password.required = false;
    email.hidden = false;
    document.querySelector('#email-label').hidden = false;
    email.required = true;
  }
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    submit.disabled = true;
    const readyLabel = submit.textContent;
    submit.textContent = 'Please wait…';
    try {
      const address = email.value.trim();
      if (route === '/login') { await post('/api/auth/login', { email: address, password: password.value }); location.assign('/dashboard'); return; }
      if (route === '/signup') { await post('/api/auth/signup', { email: address, password: password.value }); location.assign('/login?reason=registered'); return; }
      if (route === '/verify') { const result = await post('/api/auth/verify', { token }); show(result.message); }
      if (route === '/reset-password' && token) { const result = await post('/api/auth/reset-password', { token, password: password.value }); show(result.message); }
      if (route === '/reset-password' && !token) { const result = await post('/api/auth/request-reset', { email: address }); show(result.message); }
    } catch (error) { show(error.message, true); }
    finally { submit.disabled = false; submit.textContent = readyLabel; }
  });
  document.querySelector('#forgot').addEventListener('click', () => location.assign('/reset-password'));
}

if (location.pathname === '/dashboard') dashboard();
else account();
