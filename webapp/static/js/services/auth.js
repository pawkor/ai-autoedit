// ── Auth ─────────────────────────────────────────────────────────────────────
let _authEnabled = false;

async function initAuth() {
  const s = await api.get('/api/auth/status').catch(() => null);
  if (!s) return;
  _authEnabled = s.enabled;
  if (!s.enabled) return;

  document.getElementById('btn-settings-manage-users').style.display = '';

  if (!s.authenticated) {
    if (!s.has_users) {
      openManageUsers(true);
    } else {
      _showLoginModal();
    }
  }
}

function _showLoginModal() {
  const m = document.getElementById('login-modal');
  m.style.display = 'flex';
  setTimeout(() => document.getElementById('login-username')?.focus(), 50);
}

async function _authLogin() {
  const username = document.getElementById('login-username').value.trim();
  const password = document.getElementById('login-password').value;
  const errEl = document.getElementById('login-error');
  errEl.textContent = '';
  if (!username || !password) { errEl.textContent = 'Enter username and password'; return; }
  const res = await api.post('/api/auth/login', { username, password }).catch(() => null);
  if (!res?.ok) {
    errEl.textContent = 'Invalid credentials';
    document.getElementById('login-password').value = '';
    return;
  }
  document.getElementById('login-modal').style.display = 'none';
  document.getElementById('btn-settings-manage-users').style.display = '';
  fetch('/api/config').then(r=>r.ok?r.json():null).then(cfg=>{
    if (!cfg) return;
    _browseRoot = cfg.browse_root || '';
    if (!cfg.data_root_configured) _showDataRootModal();
  }).catch(()=>{});
  refreshJobList();
}

async function authLogout() {
  await api.post('/api/auth/logout', {}).catch(() => null);
  location.reload();
}

async function openManageUsers(firstRun = false) {
  const modal = document.getElementById('users-modal');
  const closeBtn = document.getElementById('users-modal-close');
  const firstRunMsg = document.getElementById('users-first-run-msg');
  if (firstRun) {
    closeBtn.style.display = 'none';
    firstRunMsg.style.display = '';
    document.getElementById('users-reload-row').style.display = 'none';
  } else {
    closeBtn.style.display = '';
    firstRunMsg.style.display = 'none';
  }
  modal.style.display = 'flex';
  await _renderUsersList(firstRun);
  setTimeout(() => document.getElementById('new-user-name')?.focus(), 50);
}

function _usersModalClose() {
  document.getElementById('users-modal').style.display = 'none';
}

async function _renderUsersList(firstRun = false) {
  const list = document.getElementById('users-list');
  const users = await api.get('/api/auth/users').catch(() => null);
  if (!users) { list.textContent = 'Error loading users'; return; }

  if (firstRun && users.length > 0) {
    document.getElementById('users-modal-close').style.display = '';
    document.getElementById('users-reload-row').style.display = '';
  }

  if (!users.length) { list.innerHTML = '<div style="font-size:11px;color:var(--muted);padding:4px 0">No users yet.</div>'; return; }

  list.innerHTML = '';
  for (const u of users) {
    const row = document.createElement('div');
    row.style.cssText = 'display:flex;align-items:center;justify-content:space-between;padding:5px 0;border-bottom:1px solid var(--border)';
    const nameSpan = document.createElement('span');
    nameSpan.style.fontSize = '12px';
    nameSpan.textContent = u.username;
    const btns = document.createElement('div');
    btns.style.cssText = 'display:flex;gap:6px;align-items:center';
    const editSpan = document.createElement('span');
    editSpan.id = `pw-edit-${_esc(u.username)}`;
    editSpan.style.cssText = 'font-size:11px;color:var(--accent);cursor:pointer';
    editSpan.textContent = 'change pw';
    editSpan.addEventListener('click', () => _startEditPw(u.username));
    const delBtn = document.createElement('button');
    delBtn.className = 'icon-btn';
    delBtn.style.cssText = 'font-size:11px;color:var(--red)';
    delBtn.title = 'Delete user';
    delBtn.textContent = '✕';
    delBtn.addEventListener('click', () => _authDeleteUser(u.username));
    btns.append(editSpan, delBtn);
    row.append(nameSpan, btns);
    list.appendChild(row);
    const pwRow = document.createElement('div');
    pwRow.id = `pw-row-${_esc(u.username)}`;
    pwRow.style.cssText = 'display:none;padding:4px 0 6px';
    const pwInput = document.createElement('input');
    pwInput.type = 'password';
    pwInput.id = `pw-input-${_esc(u.username)}`;
    pwInput.placeholder = 'New password';
    pwInput.style.cssText = 'width:100%;box-sizing:border-box;font-size:11px';
    pwInput.addEventListener('blur', () => _savePwIfValue(u.username));
    pwInput.addEventListener('keydown', e => {
      if (e.key === 'Enter') _savePw(u.username);
      else if (e.key === 'Escape') _cancelEditPw(u.username);
    });
    pwRow.appendChild(pwInput);
    list.appendChild(pwRow);
  }
}

function _startEditPw(username) {
  document.getElementById(`pw-row-${username}`).style.display = '';
  document.getElementById(`pw-input-${username}`)?.focus();
}
function _cancelEditPw(username) {
  document.getElementById(`pw-row-${username}`).style.display = 'none';
  document.getElementById(`pw-input-${username}`).value = '';
}
async function _savePwIfValue(username) {
  const inp = document.getElementById(`pw-input-${username}`);
  if (inp?.value) await _savePw(username);
  else _cancelEditPw(username);
}
async function _savePw(username) {
  const inp = document.getElementById(`pw-input-${username}`);
  const pw = inp?.value || '';
  if (!pw) return;
  await api.patch(`/api/auth/users/${username}`, { password: pw }).catch(() => null);
  inp.value = '';
  _cancelEditPw(username);
}

async function _authDeleteUser(username) {
  const firstRun = document.getElementById('users-first-run-msg').style.display !== 'none';
  await api.del(`/api/auth/users/${encodeURIComponent(username)}`).catch(() => null);
  await _renderUsersList(firstRun);
}

async function _authCreateUser() {
  const name = document.getElementById('new-user-name').value.trim();
  const pw   = document.getElementById('new-user-pw').value;
  const err  = document.getElementById('users-error');
  err.textContent = '';
  if (!name || !pw) { err.textContent = 'Username and password required'; return; }
  const firstRun = document.getElementById('users-first-run-msg').style.display !== 'none';
  const res = await api.post('/api/auth/users', { username: name, password: pw }).catch(() => null);
  if (!res?.ok) {
    err.textContent = res ? 'User already exists' : 'Error creating user';
    return;
  }
  document.getElementById('new-user-name').value = '';
  document.getElementById('new-user-pw').value = '';
  if (firstRun) {
    location.reload();
  } else {
    await _renderUsersList(false);
  }
}

initAuth();
