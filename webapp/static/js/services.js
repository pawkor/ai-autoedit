function _s3DerivedPrefix(workDir) {
  // Strip BROWSE_ROOT, keep original path structure with slashes intact
  // e.g. /data/2025/04-Grecja/04.21 → '2025/04-Grecja/04.21/'
  let rel = workDir;
  if (_browseRoot && rel.startsWith(_browseRoot)) rel = rel.slice(_browseRoot.length);
  rel = rel.replace(/^\/+/, '').replace(/\/+$/, '');
  return rel ? rel + '/' : '';
}

let _s3Configured = false;

async function s3SectionInit() {
  const s3 = await api.get('/api/s3/status').catch(() => null);
  if (!s3?.configured) return;
  _s3Configured = true;
  const section = document.getElementById('js-s3-section');
  if (!section) return;
  section.style.display = '';
  await s3LoadFileList();
}

async function s3LoadFileList() {
  const wd = document.getElementById('js-workdir')?.value.trim();
  const listEl = document.getElementById('js-s3-list');
  if (!wd || !listEl) return;
  listEl.innerHTML = '<span style="color:var(--muted)">Loading…</span>';

  let data;
  try {
    data = await api.get(`/api/s3/source-status?work_dir=${encodeURIComponent(wd)}`);
  } catch(e) {
    listEl.innerHTML = `<span style="color:var(--red)">Error: ${e.message || e}</span>`;
    return;
  }

  const cams = data.cams || {};
  const entries = Object.entries(cams);
  if (!entries.length) {
    listEl.innerHTML = '<span style="color:var(--muted)">No video files found on S3</span>';
    return;
  }

  let html = '';
  let firstCam = true;
  for (const [cam, files] of entries) {
    if (cam) {
      const border = firstCam ? '' : 'border-top:1px solid var(--border);';
      html += `<div style="${border}padding:3px 4px 1px;font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px">${cam}</div>`;
      firstCam = false;
    }
    for (const f of files) {
      const sz = _s3FmtSize(f.size);
      const localStyle = f.local ? 'color:var(--green)' : 'color:var(--muted)';
      const localTxt   = f.local ? '✓' : '☁';
      const checked    = f.local ? '' : 'checked';
      html += `<label style="display:flex;align-items:center;gap:6px;padding:2px 4px;cursor:pointer;border-radius:2px" onmouseenter="this.style.background='var(--bg3)'" onmouseleave="this.style.background=''">` +
        `<input type="checkbox" class="s3-file-chk" data-key="${f.key}" ${checked}>` +
        `<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11px" title="${f.name}">${f.name}</span>` +
        `<span style="color:var(--muted);font-size:10px;white-space:nowrap">${sz}</span>` +
        `<span style="${localStyle};font-size:11px;min-width:1.2em;text-align:center" title="${f.local ? 'local' : 'S3 only'}">${localTxt}</span>` +
        `</label>`;
    }
  }
  listEl.innerHTML = html;
  _s3UpdateAllChk();
}

function _s3FmtSize(b) {
  if (b >= 1_073_741_824) return (b / 1_073_741_824).toFixed(1) + ' GB';
  if (b >= 1_048_576)     return Math.round(b / 1_048_576) + ' MB';
  return Math.round(b / 1024) + ' KB';
}

function _s3ToggleAll(checked) {
  document.querySelectorAll('.s3-file-chk').forEach(cb => cb.checked = checked);
}

function _s3UpdateAllChk() {
  const all = document.querySelectorAll('.s3-file-chk');
  const chk = document.querySelectorAll('.s3-file-chk:checked');
  const allEl = document.getElementById('js-s3-all');
  if (!allEl) return;
  allEl.indeterminate = chk.length > 0 && chk.length < all.length;
  allEl.checked = all.length > 0 && chk.length === all.length;
}

// ── Proxy media ───────────────────────────────────────────────────────────────
let _proxyPollTimer = null;

async function startProxy() {
  if (!currentJobId) return;
  // Always kick off — server checks actual proxy files, not cached status.
  // Idempotent: does nothing if already running.
  await api.post(`/api/jobs/${currentJobId}/start-proxy`, {});
  _pollProxyStatus();
}

function _pollProxyStatus() {
  if (_proxyPollTimer) clearInterval(_proxyPollTimer);
  _proxyPollTimer = setInterval(async () => {
    if (!currentJobId) { clearInterval(_proxyPollTimer); return; }
    const st = await api.get(`/api/jobs/${currentJobId}/proxy-status`);
    if (!st) return;
    _updateProxyUI(st);
    if (st.done) { clearInterval(_proxyPollTimer); _proxyPollTimer = null; }
  }, 1000);
}

function _updateProxyUI(st) {
  const bars = document.getElementById('js-proxy-bars');
  if (!bars) return;

  const total    = st.total || 0;
  const finished = st.finished || 0;
  const cams     = st.cams || null;

  if (st.not_started) {
    bars.innerHTML = '<span style="font-size:11px;color:var(--muted)">—</span>';
    return;
  }
  if (st.done) {
    let msg, color;
    if (st.cancelled) {
      msg = 'Cancelled'; color = 'var(--muted)';
    } else if (st.error) {
      msg = 'Error: ' + st.error; color = 'var(--red)';
      appendLog('[proxy] Error: ' + st.error);
    } else if (finished < total) {
      msg = `Done — ${finished}/${total} (${total - finished} failed)`; color = 'var(--red)';
    } else {
      msg = `Done — ${finished}/${total} proxy files`; color = 'var(--muted)';
    }
    bars.innerHTML = `<span style="font-size:11px;color:${color}">${msg}</span>`;
    return;
  }

  // Running — per-cam bars
  if (cams && Object.keys(cams).length > 0) {
    bars.innerHTML = '';
    for (const [cam, info] of Object.entries(cams)) {
      const pct = info.total > 0 ? Math.round(info.finished / info.total * 100) : 0;
      const isCurrent = st.current_cam === cam;

      const row = document.createElement('div');
      row.style.cssText = 'margin-bottom:7px';

      const labelRow = document.createElement('div');
      labelRow.style.cssText = 'display:flex;align-items:center;gap:8px;font-size:11px;margin-bottom:2px';

      const camSpan = document.createElement('span');
      camSpan.style.cssText = 'color:var(--fg);font-weight:600;min-width:40px';
      camSpan.textContent = cam || '—';

      const fileSpan = document.createElement('span');
      fileSpan.style.cssText = 'color:var(--muted);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:10px';
      fileSpan.textContent = isCurrent ? (st.current_file || '') : '';

      const countSpan = document.createElement('span');
      countSpan.style.cssText = 'color:var(--accent);font-weight:600;white-space:nowrap';
      countSpan.textContent = `${info.finished}/${info.total}`;

      labelRow.append(camSpan, fileSpan, countSpan);

      const track = document.createElement('div');
      track.style.cssText = 'background:var(--bg3);border-radius:3px;height:3px;overflow:hidden';
      const fill = document.createElement('div');
      fill.style.cssText = `background:var(--accent);height:100%;width:${pct}%;transition:width 0.3s`;
      track.appendChild(fill);

      row.append(labelRow, track);
      bars.appendChild(row);
    }
    if (st.error) {
      const err = document.createElement('div');
      err.style.cssText = 'font-size:10px;color:var(--red);margin-top:2px';
      err.textContent = '⚠ ' + st.error;
      bars.appendChild(err);
      appendLog('[proxy] ' + st.error);
    }
  } else {
    // Fallback: single bar (no cam info yet)
    const pct = total > 0 ? Math.round(finished / total * 100) : 0;
    bars.innerHTML = `
      <div style="display:flex;align-items:center;gap:8px;font-size:11px;margin-bottom:2px">
        <span style="color:var(--muted);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${st.current_file || 'Creating proxies…'}</span>
        <span style="color:var(--accent);font-weight:600">${finished}/${total}</span>
      </div>
      <div style="background:var(--bg3);border-radius:3px;height:3px;overflow:hidden">
        <div style="background:var(--accent);height:100%;width:${pct}%;transition:width 0.3s"></div>
      </div>`;
    if (st.error) appendLog('[proxy] ' + st.error);
  }
}

function s3FetchSources() {
  const wd = document.getElementById('js-workdir')?.value.trim();
  if (!wd) return;

  const keys = Array.from(document.querySelectorAll('.s3-file-chk:checked')).map(cb => cb.dataset.key);
  const status = document.getElementById('js-s3-fetch-status');
  if (!keys.length) {
    status.textContent = 'No files selected';
    status.style.color = 'var(--muted)';
    return;
  }

  const btn      = document.getElementById('btn-s3-fetch');
  const progress = document.getElementById('js-s3-fetch-progress');
  const fileEl   = document.getElementById('js-s3-fetch-file');
  const speedEl  = document.getElementById('js-s3-fetch-speed');
  const pctEl    = document.getElementById('js-s3-fetch-pct');
  const bar      = document.getElementById('js-s3-fetch-bar');

  _s3FetchEs?.close();
  btn.disabled = true;
  progress.style.display = '';
  status.textContent = '';
  bar.style.width = '0%';

  const url = `/api/s3/fetch-sources?work_dir=${encodeURIComponent(wd)}&keys=${encodeURIComponent(JSON.stringify(keys))}`;
  _s3FetchEs = new EventSource(url);
  _s3FetchEs.onmessage = e => {
    const d = JSON.parse(e.data);
    if (d.error) {
      status.textContent = '✗ ' + d.error;
      status.style.color = 'var(--red)';
      progress.style.display = 'none';
      btn.disabled = false;
      _s3FetchEs.close(); _s3FetchEs = null;
      return;
    }
    if (d.done) {
      const msg = d.fetched === 0
        ? 'Already up to date'
        : `Done — ${d.fetched} file${d.fetched > 1 ? 's' : ''} downloaded`;
      status.textContent = msg;
      status.style.color = 'var(--green)';
      progress.style.display = 'none';
      bar.style.width = '0%';
      btn.disabled = false;
      _s3FetchEs.close(); _s3FetchEs = null;
      s3LoadFileList();
      return;
    }
    if (d.file) {
      fileEl.textContent  = `[${d.idx}/${d.total}] ${d.file}`;
      speedEl.textContent = d.speed || '';
      pctEl.textContent   = (d.pct ?? 0) + '%';
      bar.style.width     = (d.pct ?? 0) + '%';
    }
  };
  _s3FetchEs.onerror = () => {
    status.textContent = '✗ Connection error';
    status.style.color = 'var(--red)';
    progress.style.display = 'none';
    btn.disabled = false;
    _s3FetchEs?.close(); _s3FetchEs = null;
  };
}

async function s3PurgeLocal() {
  const wd = document.getElementById('js-workdir')?.value.trim();
  if (!wd) return;
  if (!await showConfirm('Purge local sources', 'Delete local source video files and autocut scenes?\n\nCLIP cache and output files are kept.', null, 'Purge')) return;
  const r = await api.post('/api/purge-local', { work_dir: wd });
  const status = document.getElementById('js-s3-fetch-status');
  if (r?.ok) {
    status.textContent = `Purged — ${r.removed} file${r.removed !== 1 ? 's' : ''} removed`;
    status.style.color = 'var(--green)';
    s3LoadFileList();
  } else {
    status.textContent = '✗ Purge failed';
    status.style.color = 'var(--red)';
  }
}

// ── S3 & yt-dlp ───────────────────────────────────────────────────────────────
let _s3FilePath = null, _s3Es = null;

function s3ModalOpen(filePath, fileName) {
  _s3FilePath = filePath;
  document.getElementById('s3-file-name').textContent = fileName;
  document.getElementById('s3-key').value = 'highlights/' + fileName;
  document.getElementById('s3-progress-wrap').style.display = 'none';
  document.getElementById('s3-bar').style.width = '0%';
  document.getElementById('s3-pct').textContent = '';
  document.getElementById('s3-speed').textContent = '';
  document.getElementById('s3-status').textContent = '';
  document.getElementById('btn-s3-upload').disabled = false;
  document.getElementById('s3-modal').classList.add('open');
}
function s3ModalClose() {
  _s3Es?.close(); _s3Es = null;
  document.getElementById('s3-modal').classList.remove('open');
}
function s3ModalUpload() {
  const key = document.getElementById('s3-key').value.trim();
  if (!key || !_s3FilePath) return;
  document.getElementById('s3-progress-wrap').style.display = '';
  document.getElementById('s3-status').textContent = 'Uploading…';
  document.getElementById('btn-s3-upload').disabled = true;
  _s3Es?.close();
  _s3Es = new EventSource(
    `/api/s3/upload?local_path=${encodeURIComponent(_s3FilePath)}&key=${encodeURIComponent(key)}`
  );
  _s3Es.onmessage = e => {
    const d = JSON.parse(e.data);
    if (d.pct != null) {
      document.getElementById('s3-bar').style.width = d.pct + '%';
      document.getElementById('s3-pct').textContent = d.pct + '%';
    }
    if (d.speed) document.getElementById('s3-speed').textContent = d.speed;
    if (d.done) {
      _s3Es.close(); _s3Es = null;
      document.getElementById('s3-status').textContent = 'Done ✓';
      document.getElementById('s3-pct').textContent = '100%';
      document.getElementById('s3-bar').style.width = '100%';
      document.getElementById('btn-s3-upload').disabled = false;
    }
    if (d.error) {
      _s3Es.close(); _s3Es = null;
      document.getElementById('s3-status').textContent = 'Error: ' + d.error;
      document.getElementById('btn-s3-upload').disabled = false;
    }
  };
  _s3Es.onerror = () => {
    _s3Es.close(); _s3Es = null;
    document.getElementById('s3-status').textContent = 'Connection error';
    document.getElementById('btn-s3-upload').disabled = false;
  };
}

async function s3MusicBrowse() {
  const prefix = document.getElementById('music-s3-prefix').value.trim();
  const statusEl = document.getElementById('music-s3-status');
  statusEl.textContent = 'Loading…';
  const data = await api.get(`/api/s3/list?prefix=${encodeURIComponent(prefix)}`);
  if (!data) { statusEl.textContent = 'Error'; return; }
  const items = data.items || [];
  statusEl.textContent = items.length ? `${items.length} tracks` : 'No audio files';
  const wrap = document.getElementById('music-s3-wrap');
  const list = document.getElementById('music-s3-list');
  list.innerHTML = '';
  if (!items.length) return;

  const musicDir = document.getElementById('music-dir-input').value.trim();
  for (const item of items) {
    const ext = item.name.split('.').pop().toLowerCase();
    if (!['mp3','m4a','flac','ogg','wav','aac'].includes(ext)) continue;
    const row = document.createElement('div');
    row.className = 's3-item';
    const name = document.createElement('span'); name.className = 's3-name'; name.textContent = item.name; name.title = item.key;
    const size = document.createElement('span'); size.className = 's3-size'; size.textContent = _fmtSize(item.size);
    const dlBtn = document.createElement('button'); dlBtn.className = 'btn-sm'; dlBtn.textContent = '↓';
    dlBtn.title = 'Download to local music dir';
    dlBtn.onclick = () => s3MusicDownload(item.key, item.name, musicDir, dlBtn);
    row.append(name, size, dlBtn);
    list.appendChild(row);
  }
  wrap.style.display = '';
  document.getElementById('btn-music-s3-close').style.display = '';
}

let _s3MusicEs = null;
function s3MusicDownload(key, name, musicDir, btnEl) {
  if (!musicDir) { alert('Set music directory first'); return; }
  const localPath = musicDir.replace(/\/$/, '') + '/' + name;
  btnEl.disabled = true;
  btnEl.textContent = '…';
  _s3MusicEs?.close();
  _s3MusicEs = new EventSource(
    `/api/s3/download?key=${encodeURIComponent(key)}&local_path=${encodeURIComponent(localPath)}`
  );
  _s3MusicEs.onmessage = e => {
    const d = JSON.parse(e.data);
    if (d.pct != null) btnEl.textContent = d.pct + '%';
    if (d.done) {
      _s3MusicEs.close(); _s3MusicEs = null;
      btnEl.textContent = '✓';
      loadMusicTracks(); // refresh local list
    }
    if (d.error) {
      _s3MusicEs.close(); _s3MusicEs = null;
      btnEl.textContent = '!';
      btnEl.title = d.error;
      btnEl.disabled = false;
    }
  };
  _s3MusicEs.onerror = () => {
    _s3MusicEs.close();
    btnEl.textContent = '!'; btnEl.disabled = false;
  };
}

let _ytdlEs = null;
function ytdlDownload() {
  const url = document.getElementById('music-yt-url').value.trim();
  if (!url) return;
  const msgEl  = document.getElementById('music-yt-msg');
  const pctEl  = document.getElementById('music-yt-pct');
  msgEl.textContent  = 'Starting…';
  pctEl.textContent  = '';
  _ytdlEs?.close();
  _ytdlEs = new EventSource(`/api/music/yt-download?url=${encodeURIComponent(url)}`);
  _ytdlEs.onmessage = e => {
    const d = JSON.parse(e.data);
    if (d.msg) msgEl.textContent = d.msg.replace(/^\[.*?\]\s*/, '');
    if (d.pct != null) pctEl.textContent = Math.round(d.pct) + '%';
    if (d.done) {
      _ytdlEs.close(); _ytdlEs = null;
      pctEl.textContent = '';
      document.getElementById('music-yt-url').value = '';
      _ytdlSave(d.path, d.name, msgEl, pctEl);
    }
    if (d.error) {
      _ytdlEs.close(); _ytdlEs = null;
      msgEl.textContent = '✗ ' + d.error.split('\n').pop();
      pctEl.textContent = '';
    }
  };
  _ytdlEs.onerror = () => {
    _ytdlEs.close();
    document.getElementById('music-yt-msg').textContent = 'Connection error';
  };
}

async function _ytdlSave(tmpPath, name, msgEl, pctEl) {
  const musicDir = document.getElementById('music-dir-input').value.trim();

  if (_s3Configured) {
    // ── S3 path ──────────────────────────────────────────────────────────────
    const prefix = (document.getElementById('music-s3-prefix').value.trim() || 'music/').replace(/\/*$/, '/');
    const key    = prefix + name + '.mp3';
    msgEl.textContent = 'Uploading to S3…';
    let _s3UpEs = new EventSource(`/api/s3/upload?local_path=${encodeURIComponent(tmpPath)}&key=${encodeURIComponent(key)}`);
    await new Promise(resolve => {
      _s3UpEs.onmessage = ev => {
        const u = JSON.parse(ev.data);
        if (u.pct != null) pctEl.textContent = u.pct + '%';
        if (u.speed)       msgEl.textContent = 'S3 ' + u.speed;
        if (u.done) {
          _s3UpEs.close();
          pctEl.textContent = '';
          resolve(true);
        }
        if (u.error) {
          _s3UpEs.close();
          msgEl.textContent = '✗ S3: ' + u.error;
          pctEl.textContent = '';
          // Fall back to tmp path so this session still works
          pinnedTrack = tmpPath;
          document.querySelector('#btn-music-to-summary')?.classList.remove('hidden');
          updatePhaseUI();
          resolve(false);
        }
      };
      _s3UpEs.onerror = () => { _s3UpEs.close(); msgEl.textContent = 'S3 upload error'; resolve(false); };
    });
    // After S3 upload — also save locally so pipeline can use it right away
    if (musicDir) {
      const res = await fetch('/api/music/save-downloaded', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({tmp_path: tmpPath, music_dir: musicDir}),
      }).then(r => r.ok ? r.json() : null).catch(() => null);
      if (res?.ok) {
        pinnedTrack = res.path;
        msgEl.textContent = '✓ ' + name + ' (S3 + local)';
        loadMusicTracks();
      } else {
        pinnedTrack = tmpPath;
        msgEl.textContent = '✓ ' + name + ' (S3)';
      }
    } else {
      pinnedTrack = tmpPath;
      msgEl.textContent = '✓ ' + name + ' (S3)';
    }
    s3MusicBrowse();
  } else {
    // ── Local path ───────────────────────────────────────────────────────────
    if (!musicDir) {
      // No music dir set — keep temp file for this session only
      pinnedTrack = tmpPath;
      msgEl.textContent = '✓ ' + name + ' (set Music dir to save permanently)';
      document.querySelector('#btn-music-to-summary')?.classList.remove('hidden');
      updatePhaseUI();
      return;
    }
    const res = await fetch('/api/music/save-downloaded', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({tmp_path: tmpPath, music_dir: musicDir}),
    }).then(r => r.ok ? r.json() : null).catch(() => null);
    if (res?.ok) {
      pinnedTrack = res.path;
      msgEl.textContent = '✓ ' + name;
      loadMusicTracks();
    } else {
      pinnedTrack = tmpPath;
      msgEl.textContent = '✓ ' + name + ' (save failed)';
    }
  }
  document.querySelector('#btn-music-to-summary')?.classList.remove('hidden');
  updatePhaseUI();
}

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

// ── Confirm modal ──────────────────────────────────────────────────────────────
let _confirmResolve = null;

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
    v.src = `/api/file?path=${encodeURIComponent(path)}`;
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
  const m = 14, w = 242, h = 137;
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

// ── Scene parameter calculator ───────────────────────────────────────────────
const _sceneParamManual = { f: false, js: false };

function markManual(prefix) {
  _sceneParamManual[prefix] = true;
  const info = document.getElementById(prefix + '-src-info');
  if (info && info.dataset.count) info.textContent = info.dataset.count + ' · manual';
  if (prefix === 'js') {
    const ms = parseFloat(document.getElementById('js-max-scene')?.value || '0');
    const pf = parseFloat(document.getElementById('js-per-file')?.value  || '0');
    if (ms > 0) currentJobMaxScene = ms;
    if (pf > 0) currentJobPerFile  = pf;
    _overridesChangedSinceRender = true;
    _syncThresholdDisplay();
    _scheduleEstimate();
  }
}

async function fillSceneParams(prefix) {
  const workdir = document.getElementById('js-workdir').value.trim();
  if (!workdir) return;
  const cameras = readCamsList(prefix + '-cam-list');
  // Target minutes: gallery field (shared) or form field, fallback 6
  const targetMinEl = document.getElementById('gallery-target-min') ||
                      document.getElementById(prefix + '-target-min');
  const targetMin = (_parseTargetInput(targetMinEl?.value) ?? 360) / 60;

  const camsParam = cameras.length ? cameras.join(',') : '';
  const data = await api.get(
    `/api/count-sources?dir=${encodeURIComponent(workdir)}` +
    (camsParam ? `&cameras=${encodeURIComponent(camsParam)}` : '')
  );
  if (!data) return;

  const total = data.total || 0;
  const nCams = Math.max(cameras.length, 1);
  const filesPerCam = total / nCams;

  const info = document.getElementById(prefix + '-src-info');
  const countStr = `${total} files / ${nCams} cam${nCams > 1 ? 's' : ''}`;
  if (info) { info.textContent = countStr; info.dataset.count = countStr; }

  // Formula: max_per_file = (target_s × n_cams) / (files_per_cam × 0.5 hit-rate)
  const targetSec = targetMin * 60;
  const maxPerFile = Math.max(5, Math.round((targetSec * nCams) / (filesPerCam * 0.5)));
  const maxScene   = Math.min(maxPerFile, Math.max(4, Math.round(maxPerFile * 0.2)));

  document.getElementById(prefix + '-max-scene').value = maxScene;
  document.getElementById(prefix + '-per-file').value  = maxPerFile;
  _sceneParamManual[prefix] = false;
  if (info) info.textContent = countStr;

  // Update in-memory state and retrigger estimate for the Settings panel
  if (prefix === 'js') {
    currentJobMaxScene = maxScene;
    currentJobPerFile  = maxPerFile;
    _overridesChangedSinceRender = true;
    _syncThresholdDisplay();
    _scheduleEstimate();
  }

  // Save target to config
  const wd = prefix === 'js'
    ? document.getElementById('js-workdir').value.trim()
    : workdir;
  if (wd) api.put('/api/job-config', { work_dir: wd, target_minutes: targetMin });
}

// ── Camera subfolder picker ───────────────────────────────────────────────────
const _CAM_LETTERS = ['A','B','C','D','E','F','G','H'];

async function _fetchCamSubdirs(workdir) {
  if (!workdir) return [];
  const data = await api.get(`/api/subdirs?dir=${encodeURIComponent(workdir)}`);
  return data || [];
}

function _camOptions(subdirs, selected) {
  const frag = document.createDocumentFragment();
  const none = document.createElement('option'); none.value = ''; none.textContent = '— none —';
  frag.appendChild(none);
  for (const d of subdirs) {
    const o = document.createElement('option'); o.value = o.textContent = d;
    if (d === selected) o.selected = true;
    frag.appendChild(o);
  }
  return frag;
}

function _relabelCams(container) {
  container.querySelectorAll('.cam-row').forEach((row, i) => {
    row.querySelector('.cam-label').textContent = 'Cam ' + (_CAM_LETTERS[i] || String.fromCharCode(65+i));
  });
  _updateWorkdirBrowseBtn();
}

function _setWorkdir(v) {
  const el = document.getElementById('js-workdir');
  if (!el || v == null) return;
  el.value = v;
  el.size = Math.max(32, v.length + 1);
}

function _updateWorkdirBrowseBtn() {
  const btn = document.getElementById('js-workdir-browse-btn');
  if (!btn) return;
  const count = document.getElementById('js-cam-list')?.querySelectorAll('.cam-row').length || 0;
  btn.style.display = count > 0 ? 'none' : '';
}

function _openWorkdirFileBrowser() {
  const wd = document.getElementById('js-workdir')?.value.trim();
  if (wd) openFileBrowser(wd, '');
}

async function addCamRow(containerId, value, subdirs, offsetSec) {
  const container = document.getElementById(containerId);
  if (!subdirs) {
    const wd = containerId.startsWith('f-')
      ? document.getElementById('js-workdir').value.trim()
      : document.getElementById('js-workdir').value.trim();
    subdirs = await _fetchCamSubdirs(wd);
  }
  const row = document.createElement('div'); row.className = 'cam-row';
  const lbl = document.createElement('span'); lbl.className = 'cam-label'; lbl.textContent = 'Cam ?';
  const sel = document.createElement('select'); sel.className = 'cam-select';
  sel.appendChild(_camOptions(subdirs, value || ''));
  const browse = document.createElement('button'); browse.className = 'btn-sm'; browse.textContent = '📁';
  browse.title = 'Browse files'; browse.style.flexShrink = '0';
  browse.onclick = () => {
    const wd = containerId.startsWith('f-')
      ? document.getElementById('js-workdir').value.trim()
      : document.getElementById('js-workdir').value.trim();
    const sub = sel.value.trim();
    openFileBrowser(wd, sub);
  };
  const offLabel = document.createElement('span');
  offLabel.textContent = '±s'; offLabel.style.cssText = 'font-size:10px;color:var(--muted);flex-shrink:0';
  offLabel.title = 'Time offset in seconds (e.g. 7200 = +2h)';
  const offInput = document.createElement('input');
  offInput.type = 'number'; offInput.className = 'cam-offset';
  offInput.value = offsetSec != null ? String(Math.round(offsetSec)) : '0';
  offInput.step = '1'; offInput.title = 'Time offset in seconds (e.g. 7200 = +2h, -3600 = -1h)';
  offInput.addEventListener('change', () => typeof saveSettingsField === 'function' && saveSettingsField());
  offInput.addEventListener('blur',   () => typeof saveSettingsField === 'function' && saveSettingsField());
  const btn = document.createElement('button'); btn.className = 'btn-sm'; btn.textContent = '−';
  btn.title = 'Remove camera'; btn.style.flexShrink = '0';
  btn.onclick = () => { row.remove(); _relabelCams(container); };
  row.append(lbl, sel, browse, offLabel, offInput, btn);
  container.appendChild(row);
  _relabelCams(container);
}

async function setCamsList(containerId, cameras, camOffsets) {
  const container = document.getElementById(containerId);
  container.innerHTML = '';
  const wd = document.getElementById('js-workdir').value.trim();
  const subdirs = await _fetchCamSubdirs(wd);
  const list = (cameras || []).filter(Boolean);
  for (const cam of list) await addCamRow(containerId, cam, subdirs, (camOffsets || {})[cam]);
  _updateWorkdirBrowseBtn();
}

function readCamOffsets(containerId) {
  const offsets = {};
  document.getElementById(containerId).querySelectorAll('.cam-row').forEach(row => {
    const cam = row.querySelector('.cam-select')?.value?.trim();
    const sec = parseInt(row.querySelector('.cam-offset')?.value || '0', 10);
    if (cam && sec !== 0) offsets[cam] = sec;
  });
  return Object.keys(offsets).length ? offsets : null;
}

function readCamsList(containerId) {
  return [...document.getElementById(containerId).querySelectorAll('.cam-select')]
    .map(s => s.value.trim()).filter(Boolean);
}

// Music dir (job Music tab)
function openMusicDirBrowser() {
  openDirBrowser(document.getElementById('music-dir-input').value, p => {
    document.getElementById('music-dir-input').value = p;
    const wd = document.getElementById('js-workdir').value.trim();
    if (wd) api.put('/api/job-config', { work_dir: wd, music_dir: p });
    if (currentJobId) api.patch(`/api/jobs/${currentJobId}/params`, { music_dir: p });
    loadMusicTracks();
  });
}

async function loadDirConfig(dir) {
  if (!dir || !dir.trim()) return;
  const data = await api.get(`/api/job-config?dir=${encodeURIComponent(dir.trim())}`);
  if (!data) return;

  // If this directory already has a job, open it instead of the new-job form
  if (data._resolved) {
    const jobList = await api.get('/api/jobs') || [];
    const existing = jobList.find(j => j.work_dir === data._resolved);
    if (existing) { openJob(existing.id); return; }
    // Directory was processed outside webapp — import it as a done job
    if (data._has_processed) {
      const imp = await api.post('/api/jobs/import', { work_dir: data._resolved });
      if (imp?.id) { refreshJobList(); openJob(imp.id); return; }
    }
    // New directory — create a draft job immediately so it persists in the sidebar
    const dr = await api.post('/api/jobs?draft=true', { work_dir: data._resolved });
    if (dr?.id) { refreshJobList(); openJob(dr.id); return; }
  }
}
async function dirUp() {
  const data = await api.get(`/api/browse?path=${encodeURIComponent(browsePath)}`);
  if (data?.parent) await browseTo(data.parent);
}
async function browseTo(path) {
  const url = path ? `/api/browse?path=${encodeURIComponent(path)}` : '/api/browse';
  const data = await api.get(url);
  if (!data) return;
  browsePath = data.path;
  document.getElementById('dir-path-txt').textContent = data.path;
  const list = document.getElementById('dir-list');
  list.innerHTML = '';
  if (data.parent) {
    const d=document.createElement('div'); d.className='de';
    d.innerHTML=`<span class="icon">↑</span><span class="name">..</span>`;
    d.onclick=()=>browseTo(data.parent); list.appendChild(d);
  }
  for (const e of data.entries) {
    if (!e.is_dir) continue;
    const d=document.createElement('div'); d.className='de';
    d.innerHTML=`<span class="icon">▶</span><span class="name"></span>
      ${e.has_mp4?'<span class="badge">MP4</span>':''}
      ${e.has_autoframe?'<span class="badge" style="color:var(--accent)">cached</span>':''}`;
    d.querySelector('.name').textContent = e.name;
    d.onclick=()=>{browsePath=e.path; document.getElementById('dir-path-txt').textContent=e.path;};
    d.ondblclick=()=>browseTo(e.path);
    list.appendChild(d);
  }
}

// ── Settings ──────────────────────────────────────────────────────────────────
async function openSettings() {
  const s = await api.get('/api/settings');
  if (s) {
    document.getElementById('s-max-jobs').value     = s.max_concurrent_jobs;
    document.getElementById('s-max-detect').value   = s.max_detect_workers;
    document.getElementById('s-clip-batch').value   = s.clip_batch_size;
    document.getElementById('s-clip-workers').value = s.clip_workers;
  }
  document.getElementById('settings-modal').classList.add('open');
  ytCheckStatus();
}
function _ytProjectMeta(workdir) {
  const parts = workdir.replace(/\\/g, '/').split('/').filter(Boolean);
  let year = '', location = '';
  for (const p of parts) {
    if (/^\d{4}$/.test(p)) { year = p; continue; }
    if (year && /^\d{2}-/.test(p)) { location = p.replace(/^\d{2}-/, ''); break; }
  }
  return [year, location].filter(Boolean).join(' ');
}

let _ytFileName = null;

const _YT_DEFAULT_FOOTER = '#motorcyclelife #motovlog #adventurebike #ktm #roadtrip\nhttps://github.com/pawkor/ai-autoedit';
let _ytMetaSaved = true;

async function ytModalOpen(filePath, fileName, existingUrl) {
  const status = await api.get('/api/youtube/status');
  if (!status?.authenticated) { alert('Connect YouTube first (Settings ⚙ → YouTube)'); return; }
  _ytFilePath = filePath;
  _ytFileName = fileName;
  document.getElementById('yt-file-name').textContent = fileName;
  document.getElementById('yt-existing-url').value = existingUrl || '';
  const workdir = document.getElementById('js-workdir').value;
  const projectName = _ytProjectMeta(workdir) || workdir.split('/').filter(Boolean).pop() || fileName.replace(/\.mp4$/i, '');
  // Try to load saved title/desc from config.ini
  let savedTitle = '', savedDesc = '';
  if (currentJobId) {
    try {
      const cfg = await api.get(`/api/job-config?dir=${encodeURIComponent(workdir)}`);
      savedTitle = cfg?.yt_title || '';
      savedDesc  = cfg?.yt_desc  || '';
    } catch (_) {}
  }
  document.getElementById('yt-title').value = savedTitle || projectName;
  document.getElementById('yt-desc').value  = savedDesc  || _YT_DEFAULT_FOOTER;
  document.getElementById('yt-gen-status').textContent = '';
  document.querySelector('input[name="yt-privacy"][value="unlisted"]').checked = true;
  document.getElementById('yt-new-playlist').style.display = 'none';
  document.getElementById('yt-new-playlist').value = '';
  document.getElementById('yt-status').textContent = '';
  const btn = document.getElementById('btn-yt-upload');
  btn.disabled = false;
  btn.textContent = '▲ Upload';
  _ytMetaSaved = true;
  document.getElementById('yt-modal').classList.add('open');
  await ytLoadPlaylists();
}

function _ytMetaDirty() { _ytMetaSaved = false; }

async function _ytMetaSave() {
  if (_ytMetaSaved || !currentJobId) return;
  const title = document.getElementById('yt-title').value.trim();
  const desc  = document.getElementById('yt-desc').value.trim();
  await api.post(`/api/jobs/${currentJobId}/save-yt-meta`, { title, desc });
  _ytMetaSaved = true;
}

function _ytFooterFromDesc(desc) {
  // Extract preserved footer: trailing lines that are all hashtags/URLs/blank
  const lines = desc.trimEnd().split('\n');
  let i = lines.length - 1;
  while (i >= 0 && /^(\s*|#\S+(\s+#\S+)*|https?:\/\/\S+)(\s+.*)?$/.test(lines[i])) i--;
  const footer = lines.slice(i + 1).join('\n').trim();
  return footer || _YT_DEFAULT_FOOTER;
}

async function _generateYtMeta() {
  if (!currentJobId) return;
  const btn = document.getElementById('btn-yt-gen');
  const st  = document.getElementById('yt-gen-status');
  btn.disabled = true;
  st.textContent = 'generating…';
  const workdir = document.getElementById('js-workdir').value;
  const projectName = document.getElementById('yt-title').value.trim()
                   || _ytProjectMeta(workdir) || workdir.split('/').filter(Boolean).pop() || '';
  const currentDesc = document.getElementById('yt-desc').value;
  const footer = _ytFooterFromDesc(currentDesc);
  const res = await api.post(`/api/jobs/${currentJobId}/generate-yt-meta`, { project_name: projectName, footer });
  btn.disabled = false;
  if (res?.ok) {
    document.getElementById('yt-desc').value  = res.description;
    st.textContent = '';
    _ytMetaSaved = false;
    _ytMetaSave();
  } else {
    st.textContent = '⚠ ' + (res?.error || 'failed');
    st.style.color = 'var(--red)';
  }
}

function ytModalClose() {
  document.getElementById('yt-modal').classList.remove('open');
}

// ── YouTube Shorts modal ───────────────────────────────────────────────────────
const _YT_SHORTS_FOOTER = '#shorts #motorcycle #motovlog #adventurebike #ktm #roadtrip\nhttps://github.com/pawkor/ai-autoedit';
let _ytsFilePath = null, _ytsFileName = null;

async function ytShortsModalOpen(filePath, fileName) {
  const status = await api.get('/api/youtube/status');
  if (!status?.authenticated) { alert('Connect YouTube first (Settings ⚙ → YouTube)'); return; }
  _ytsFilePath = filePath;
  _ytsFileName = fileName;
  const workdir = document.getElementById('js-workdir').value;
  const projectName = _ytProjectMeta(workdir) || workdir.split('/').filter(Boolean).pop() || fileName.replace(/-short_v\d+\.mp4$/i, '');

  // Collect ALL non-short videos with yt_url for dropdown
  const mainVideos = [];
  if (currentJobId) {
    try {
      const results = await api.get(`/api/jobs/${currentJobId}/result`);
      for (const [name, info] of Object.entries(results || {})) {
        if (!/short/i.test(name) && info.yt_url) mainVideos.push({ name, url: info.yt_url });
      }
    } catch (_) {}
  }
  const mainVideoUrl = mainVideos[0]?.url || null;

  // Populate full-video dropdown (show only when 2+ options)
  const selRow = document.getElementById('yts-fullvideo-row');
  const selEl  = document.getElementById('yts-fullvideo-select');
  selEl.innerHTML = '';
  if (mainVideos.length > 1) {
    for (const v of mainVideos) {
      const opt = document.createElement('option');
      opt.value = v.url;
      opt.textContent = v.name.replace(/\.mp4$/i, '');
      selEl.appendChild(opt);
    }
    selRow.style.display = '';
  } else {
    selRow.style.display = 'none';
  }

  // Load title from project config.ini [job] title field (first line only)
  let titleVal = projectName;
  try {
    const cfg = await api.get(`/api/job-config?dir=${encodeURIComponent(workdir)}`);
    if (cfg?.title) titleVal = cfg.title.split('\n')[0].trim();
  } catch (_) {}

  document.getElementById('yts-file-name').textContent = fileName;
  document.getElementById('yts-title').value = titleVal;

  const linkLine = mainVideoUrl ? `Full video: ${mainVideoUrl}` : '';
  document.getElementById('yts-desc').value = linkLine ? `${linkLine}\n\n${_YT_SHORTS_FOOTER}` : _YT_SHORTS_FOOTER;

  document.querySelector('input[name="yts-privacy"][value="public"]').checked = true;

  const statusEl = document.getElementById('yts-status');
  const btn = document.getElementById('btn-yts-upload');
  btn.onclick = ytShortsUpload;  // reset — may have been replaced by onClose after previous upload
  btn.textContent = '▲ Upload';
  if (!mainVideoUrl) {
    statusEl.textContent = '⚠ Main video not yet published on YouTube — upload it first to include a link.';
    statusEl.style.color = 'var(--yellow, #e5b400)';
    btn.disabled = true;
  } else {
    statusEl.textContent = '';
    btn.disabled = false;
  }
  document.getElementById('yts-new-playlist').style.display = 'none';
  document.getElementById('yts-new-playlist').value = '';
  document.getElementById('yt-shorts-modal').classList.add('open');
  await ytLoadPlaylists('yts-playlist');
}

function ytShortsModalClose() {
  document.getElementById('yt-shorts-modal').classList.remove('open');
}

function ytShortsUpdateLink() {
  const url  = document.getElementById('yts-fullvideo-select').value;
  const desc = document.getElementById('yts-desc');
  const lines = desc.value.split('\n');
  const idx = lines.findIndex(l => l.startsWith('Full video:'));
  const newLine = url ? `Full video: ${url}` : '';
  if (idx >= 0) {
    if (newLine) lines[idx] = newLine; else lines.splice(idx, 1);
  } else if (newLine) {
    lines.splice(1, 0, newLine);
  }
  desc.value = lines.join('\n');
  const btn = document.getElementById('btn-yts-upload');
  const statusEl = document.getElementById('yts-status');
  if (!url) {
    statusEl.textContent = '⚠ Main video not yet published on YouTube — upload it first to include a link.';
    statusEl.style.color = 'var(--yellow, #e5b400)';
    btn.disabled = true;
  } else {
    statusEl.textContent = ''; btn.disabled = false; btn.textContent = '▲ Upload';
  }
}

async function _generateYtShortsMeta() {
  if (!currentJobId) return;
  const btn = document.getElementById('btn-yts-gen');
  const st  = document.getElementById('yts-gen-status');
  btn.disabled = true; st.textContent = 'generating…';
  const workdir     = document.getElementById('js-workdir').value;
  const projectName = document.getElementById('yts-title').value.trim()
                   || _ytProjectMeta(workdir) || workdir.split('/').filter(Boolean).pop() || '';
  const footer      = _ytFooterFromDesc(document.getElementById('yts-desc').value) || _YT_SHORTS_FOOTER;
  const res = await api.post(`/api/jobs/${currentJobId}/generate-yt-meta`, { project_name: projectName, footer });
  btn.disabled = false;
  if (res?.ok) {
    document.getElementById('yts-desc').value = res.description;
    st.textContent = '';
  } else {
    st.textContent = '⚠ ' + (res?.error || 'failed');
    st.style.color = 'var(--red)';
  }
}

async function ytShortsUpload() {
  if (!_ytsFilePath) return;
  const title   = document.getElementById('yts-title').value.trim();
  if (!title) { alert('Enter a title'); return; }
  const privacy     = document.querySelector('input[name="yts-privacy"]:checked')?.value || 'public';
  const desc        = document.getElementById('yts-desc').value.trim();
  const playlistId  = document.getElementById('yts-new-playlist').style.display !== 'none'
                        ? null
                        : (document.getElementById('yts-playlist').value || null);
  const newPlaylist = document.getElementById('yts-new-playlist').value.trim() || null;
  const status  = document.getElementById('yts-status');
  const btn     = document.getElementById('btn-yts-upload');
  btn.disabled = true; btn.textContent = 'Uploading…'; status.textContent = '';
  const res = await api.post('/api/youtube/upload', {
    file_path: _ytsFilePath, title, description: desc, privacy,
    playlist_id: playlistId, new_playlist: newPlaylist,
  });
  if (!res?.upload_id) {
    btn.disabled = false; btn.textContent = '▲ Upload';
    status.textContent = '⚠ Upload failed'; status.style.color = 'var(--red)';
    return;
  }
  _pollYtUpload(res.upload_id, status, btn, _ytsFileName, ytShortsModalClose);
}

async function ytSaveExistingUrl() {
  const url = document.getElementById('yt-existing-url').value.trim();
  if (!url || !currentJobId || !_ytFileName) return;
  const status = document.getElementById('yt-status');
  const resp = await api.post(`/api/jobs/${currentJobId}/youtube-url`, { filename: _ytFileName, url });
  if (resp?.ok) {
    status.textContent = '';
    const linked = document.createElement('span');
    linked.textContent = '✓ Linked: ';
    const a = document.createElement('a');
    if (/^https?:\/\//i.test(url)) a.href = url;
    a.target = '_blank'; a.style.color = 'var(--accent)'; a.textContent = url;
    linked.appendChild(a);
    status.appendChild(linked);
    status.style.color = 'var(--green)';
    loadResults(currentJobId); // refresh Results tab
  } else {
    status.textContent = 'Save failed';
    status.style.color = 'var(--red)';
  }
}

function ytNewPlaylist() {
  const inp = document.getElementById('yt-new-playlist');
  const visible = inp.style.display !== 'none';
  inp.style.display = visible ? 'none' : '';
  if (!visible) { inp.focus(); document.getElementById('yt-playlist').value = ''; }
}

function ytShortsNewPlaylist() {
  const inp = document.getElementById('yts-new-playlist');
  const visible = inp.style.display !== 'none';
  inp.style.display = visible ? 'none' : '';
  if (!visible) { inp.focus(); document.getElementById('yts-playlist').value = ''; }
}

async function ytUpload() {
  if (!_ytFilePath) return;
  const title = document.getElementById('yt-title').value.trim();
  if (!title) { alert('Enter a title'); return; }
  const privacy     = document.querySelector('input[name="yt-privacy"]:checked')?.value || 'unlisted';
  const playlistId  = document.getElementById('yt-new-playlist').style.display !== 'none'
                        ? null
                        : (document.getElementById('yt-playlist').value || null);
  const newPlaylist = document.getElementById('yt-new-playlist').value.trim() || null;

  const btn    = document.getElementById('btn-yt-upload');
  const status = document.getElementById('yt-status');
  btn.disabled = true;
  status.textContent = 'Starting…';
  status.style.color = 'var(--muted)';

  const resp = await api.post('/api/youtube/upload', {
    file_path:    _ytFilePath,
    title,
    description:  document.getElementById('yt-desc').value,
    privacy,
    playlist_id:  playlistId,
    new_playlist: newPlaylist,
  });

  if (!resp?.upload_id) {
    status.textContent = 'Error: ' + (resp?.detail || 'failed to start');
    status.style.color = 'var(--red)';
    btn.disabled = false;
    return;
  }

  _pollYtUpload(resp.upload_id, status, btn, _ytFileName, ytModalClose,
    url => { document.getElementById('yt-existing-url').value = url; });
}

function _pollYtUpload(uploadId, statusEl, btn, fileName, onClose, onUrl) {
  const poll = setInterval(async () => {
    const s = await api.get(`/api/youtube/upload/${uploadId}`);
    if (!s) return;
    if (s.status === 'uploading') {
      const spd = s.speed_mbps ? ` · ${s.speed_mbps} Mbps` : '';
      statusEl.textContent = `Uploading… ${s.pct}%${spd}`;
    } else if (s.status === 'done') {
      clearInterval(poll);
      statusEl.innerHTML = `✓ <a href="${s.url}" target="_blank" style="color:var(--accent)">${s.url}</a>`;
      statusEl.style.color = 'var(--green)';
      btn.textContent = '✓ Done';
      btn.disabled = false;
      btn.onclick = onClose;
      if (onUrl) onUrl(s.url);
      if (currentJobId) loadResults(currentJobId);
    } else if (s.status === 'error') {
      clearInterval(poll);
      statusEl.textContent = 'Error: ' + (s.error || 'upload failed');
      statusEl.style.color = 'var(--red)';
      btn.disabled = false;
    }
  }, 2000);
}

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
      // First run — must create a user before continuing
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
    // First user created — show reload button
    document.getElementById('users-modal-close').style.display = '';
    document.getElementById('users-reload-row').style.display = '';
  }

  if (!users.length) { list.innerHTML = '<div style="font-size:11px;color:var(--muted);padding:4px 0">No users yet.</div>'; return; }

  list.innerHTML = users.map(u => `
    <div style="display:flex;align-items:center;justify-content:space-between;padding:5px 0;border-bottom:1px solid var(--border)">
      <span style="font-size:12px">${u.username}</span>
      <div style="display:flex;gap:6px;align-items:center">
        <span id="pw-edit-${u.username}" style="font-size:11px;color:var(--accent);cursor:pointer" onclick="_startEditPw('${u.username}')">change pw</span>
        <button class="icon-btn" style="font-size:11px;color:var(--red)" onclick="_authDeleteUser('${u.username}')" title="Delete user">✕</button>
      </div>
    </div>
    <div id="pw-row-${u.username}" style="display:none;padding:4px 0 6px">
      <input type="password" id="pw-input-${u.username}" placeholder="New password" style="width:100%;box-sizing:border-box;font-size:11px"
             onblur="_savePwIfValue('${u.username}')"
             onkeydown="if(event.key==='Enter')_savePw('${u.username}');else if(event.key==='Escape')_cancelEditPw('${u.username}')">
    </div>
  `).join('');
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

// ── Helpers ───────────────────────────────────────────────────────────────────
