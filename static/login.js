(() => {
  'use strict';
  const form = document.querySelector('#login-form');
  const button = document.querySelector('#login-submit');
  const error = document.querySelector('#login-error');
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (button.disabled || !form.reportValidity()) return;
    error.hidden = true;
    error.textContent = '';
    button.disabled = true;
    button.firstChild.textContent = 'Signing in ';
    const password = form.elements.password;
    try {
      const response = await fetch('/api/auth/login', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({username: form.elements.username.value, password: password.value})
      });
      if (response.ok) {
        password.value = '';
        window.location.replace('/');
        return;
      }
      error.textContent = response.status === 429 ? 'Too many attempts. Please wait and try again.' : response.status >= 500 ? 'Sign-in is temporarily unavailable. Please try again.' : 'Sign-in failed. Check your username and password, then try again.';
    } catch (_) {
      error.textContent = 'Cannot reach the application server. Check your connection and try again.';
    } finally {
      password.value = '';
      if (error.textContent) {
        error.hidden = false;
        button.disabled = false;
        button.firstChild.textContent = 'Sign in ';
        password.focus();
      }
    }
  });
})();
