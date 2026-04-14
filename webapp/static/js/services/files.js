// ── File list browser ─────────────────────────────────────────────────────────

async function loadFileList(prefix) {
  const wd   = document.getElementById(prefix + '-workdir').value.trim();
  const wrap = document.getElementById(prefix + '-files-wrap');
  const list = document.getElementById(prefix + '-files-list');
  if (!wd) { wrap.style.display = 'none'; return; }
  const files = await api.get(`/api/files?path=${encodeURIComponent(wd)}`);
  if (!files?.length) { wrap.style.display = 'none'; return; }
  wrap.style.display = '';
  const allCb = document.getElementById(prefix + '-files-all');
  if (allCb) allCb.checked = false;
  list.innerHTML = '';
  for (const f of files) {
    const row = document.createElement('div');
    row.className = 'fitem';
    row.dataset.path = f.path;
    const cb = document.createElement('input');
    cb.type = 'checkbox'; cb.style.flexShrink = '0';
    const name = document.createElement('span');
    name.className = 'fname'; name.textContent = f.name; name.title = f.name;
    const size = document.createElement('span');
    size.className = 'fsize'; size.textContent = _fmtSize(f.size);
    const del = document.createElement('button');
    del.className = 'fdel'; del.textContent = '✕'; del.title = 'Delete file';
    del.onclick = e => { e.stopPropagation(); _deleteFile(f.path, row, prefix); };
    row.addEventListener('mouseenter', e => _showFilePreview(f.path, e));
    row.addEventListener('mousemove',  e => _moveFileTip(e));
    row.addEventListener('mouseleave', _hideFilePreview);
    row.append(cb, name, size, del);
    list.appendChild(row);
  }
}

function _fmtSize(b) {
  if (b >= 1073741824) return (b / 1073741824).toFixed(1) + ' GB';
  if (b >= 1048576)    return (b / 1048576).toFixed(0) + ' MB';
  return (b / 1024).toFixed(0) + ' KB';
}

async function _deleteFile(path, rowEl) {
  const name = rowEl.querySelector('.fname')?.textContent || path;
  if (!await showConfirm('Delete file', `Delete ${name}?`, null, 'Delete')) return;
  const r = await api.del(`/api/file?path=${encodeURIComponent(path)}`);
  if (r?.ok) rowEl.remove();
  else alert('Delete failed');
}

async function deleteCheckedFiles(prefix) {
  const list = document.getElementById(prefix + '-files-list');
  const rows = [...list.querySelectorAll('input[type=checkbox]:checked')].map(cb => cb.closest('.fitem'));
  if (!rows.length) return;
  const names = rows.map(r => r.querySelector('.fname')?.textContent).join('\n');
  if (!await showConfirm('Delete files', `Delete ${rows.length} file(s)?`, names, 'Delete')) return;
  for (const row of rows) {
    const r = await api.del(`/api/file?path=${encodeURIComponent(row.dataset.path)}`);
    if (r?.ok) row.remove();
  }
}

function toggleAllFiles(prefix, checked) {
  document.getElementById(prefix + '-files-list')
    .querySelectorAll('input[type=checkbox]').forEach(cb => cb.checked = checked);
}

function openFileBrowser(workDir, camSub) {
  if (!workDir) { alert('Set working directory first'); return; }
  const dir = camSub ? workDir.replace(/\/$/, '') + '/' + camSub : workDir;
  _fileBrowserPath = dir;
  document.getElementById('files-path-txt').textContent = dir;
  document.getElementById('files-modal').classList.add('open');
  _refreshFilesModal();
}

function closeFileBrowser() {
  document.getElementById('files-modal').classList.remove('open');
  _hideFilePreview();
}

async function _createFolderInFiles() {
  const name = prompt('Folder name:');
  if (!name?.trim()) return;
  const data = await api.post('/api/mkdir', { path: _fileBrowserPath, name: name.trim() });
  if (data?.path) {
    _fileBrowserPath = data.path;
    document.getElementById('files-path-txt').textContent = data.path;
    _refreshFilesModal();
  } else {
    alert(data?.error || 'Failed to create folder');
  }
}

function startUploadModal(inputEl) {
  const files = [...inputEl.files];
  inputEl.value = '';
  if (!files.length) return;
  if (!_fileBrowserPath) { alert('No directory set'); return; }

  const wrap    = document.getElementById('files-upload-wrap');
  const nameEl  = document.getElementById('files-upload-name');
  const speedEl = document.getElementById('files-upload-speed');
  const pctEl   = document.getElementById('files-upload-pct');
  const bar     = document.getElementById('files-upload-bar');

  wrap.style.display = '';
  let idx = 0;

  function uploadNext() {
    if (idx >= files.length) {
      nameEl.textContent = `Done — ${files.length} file${files.length > 1 ? 's' : ''} uploaded`;
      speedEl.textContent = ''; pctEl.textContent = '';
      bar.style.width = '100%';
      setTimeout(() => { wrap.style.display = 'none'; bar.style.width = '0%'; _refreshFilesModal(); }, 1500);
      return;
    }
    const file = files[idx++];
    nameEl.textContent = `${idx}/${files.length}: ${file.name}`;
    speedEl.textContent = ''; pctEl.textContent = '0%'; bar.style.width = '0%';

    const formData = new FormData();
    formData.append('file', file);
    formData.append('work_dir', _fileBrowserPath);

    const xhr = new XMLHttpRequest();
    let lastLoaded = 0, lastTime = Date.now();

    xhr.upload.onprogress = e => {
      if (!e.lengthComputable) return;
      const pct = Math.round(e.loaded / e.total * 100);
      pctEl.textContent = pct + '%';
      bar.style.width = pct + '%';
      const now = Date.now(), dt = (now - lastTime) / 1000;
      if (dt >= 0.5) {
        speedEl.textContent = ((e.loaded - lastLoaded) / dt / 1048576).toFixed(1) + ' MB/s';
        lastLoaded = e.loaded; lastTime = now;
      }
    };
    xhr.onload = () => {
      if (xhr.status === 200) uploadNext();
      else { alert('Upload failed: ' + xhr.responseText); wrap.style.display = 'none'; }
    };
    xhr.onerror = () => { alert('Upload error'); wrap.style.display = 'none'; };
    xhr.open('POST', '/api/upload');
    xhr.send(formData);
  }
  uploadNext();
}

async function _refreshFilesModal() {
  const list = document.getElementById('files-list');
  list.innerHTML = '<div style="padding:10px 11px;font-size:11px;color:var(--muted)">Loading…</div>';
  document.getElementById('files-all').checked = false;
  const files = await api.get(`/api/files?path=${encodeURIComponent(_fileBrowserPath)}`);
  list.innerHTML = '';
  if (!files?.length) {
    list.innerHTML = '<div style="padding:10px 11px;font-size:11px;color:var(--muted)">No video files found.</div>';
    return;
  }
  for (const f of files) {
    const row = document.createElement('div');
    row.className = 'fitem';
    row.dataset.path = f.path;
    const cb = document.createElement('input');
    cb.type = 'checkbox'; cb.style.flexShrink = '0';
    const name = document.createElement('span');
    name.className = 'fname'; name.textContent = f.name; name.title = f.name;
    const size = document.createElement('span');
    size.className = 'fsize'; size.textContent = _fmtSize(f.size);
    const del = document.createElement('button');
    del.className = 'fdel'; del.textContent = '✕'; del.title = 'Delete file';
    del.onclick = e => { e.stopPropagation(); _deleteFileModal(f.path, row); };
    row.addEventListener('mouseenter', e => _showFilePreview(f.path, e));
    row.addEventListener('mousemove',  e => _moveFileTip(e));
    row.addEventListener('mouseleave', _hideFilePreview);
    row.append(cb, name, size, del);
    list.appendChild(row);
  }
}

async function _deleteFileModal(path, rowEl) {
  const name = rowEl.querySelector('.fname')?.textContent || path;
  if (!await showConfirm('Delete file', 'This cannot be undone.', name)) return;
  const r = await api.del(`/api/file?path=${encodeURIComponent(path)}`);
  if (r?.ok) rowEl.remove();
  else alert('Delete failed');
}

async function _deleteCheckedFilesModal() {
  const list = document.getElementById('files-list');
  const rows = [...list.querySelectorAll('input[type=checkbox]:checked')].map(cb => cb.closest('.fitem'));
  if (!rows.length) return;
  const names = rows.map(r => r.querySelector('.fname')?.textContent).join('\n');
  if (!await showConfirm(`Delete ${rows.length} file${rows.length > 1 ? 's' : ''}`, 'This cannot be undone.', names)) return;
  for (const row of rows) {
    const r = await api.del(`/api/file?path=${encodeURIComponent(row.dataset.path)}`);
    if (r?.ok) row.remove();
  }
}

function _toggleAllFilesModal(checked) {
  document.getElementById('files-list')
    .querySelectorAll('input[type=checkbox]').forEach(cb => cb.checked = checked);
}

// Video preview tooltip
let _previewTimer = null;
function _showFilePreview(path, e, delay = 400) {
  clearTimeout(_previewTimer);
  _previewTimer = setTimeout(() => {
    const v = document.getElementById('file-tip-video');
    const src = path.startsWith('/data/') ? path : `/api/file?path=${encodeURIComponent(path)}`;
    v.src = src;
    v.addEventListener('loadedmetadata', () => {
      v.currentTime = Math.min(3, v.duration * 0.05);
      v.play().catch(() => {});
    }, {once: true});
    document.getElementById('file-tip').style.display = 'block';
    _moveFileTip(e);
  }, delay);
}
function _moveFileTip(e) {
  const tip = document.getElementById('file-tip');
  if (tip.style.display === 'none') return;
  const m = 14, w = 964, h = 544;
  let x = e.clientX + m, y = e.clientY + m;
  if (x + w > window.innerWidth)  x = e.clientX - w - m;
  if (y + h > window.innerHeight) y = e.clientY - h - m;
  tip.style.left = x + 'px'; tip.style.top = y + 'px';
}
function _hideFilePreview() {
  clearTimeout(_previewTimer);
  const tip = document.getElementById('file-tip');
  tip.style.display = 'none';
  const v = document.getElementById('file-tip-video');
  v.pause(); v.removeAttribute('src'); v.load();
}
