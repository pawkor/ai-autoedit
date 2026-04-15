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
  await api.post(`/api/jobs/${currentJobId}/start-proxy`, {});
  _pollProxyStatus();
}

async function resumeProxyIfRunning() {
  if (!currentJobId) return;
  const st = await api.get(`/api/jobs/${currentJobId}/proxy-status`);
  if (!st) return;
  _updateProxyUI(st);
  if (!st.done && !st.not_started) _pollProxyStatus();
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
      const failedNames = (st.failed_files || []).join(', ');
      msg = `Done — ${finished}/${total} (${total - finished} failed${failedNames ? ': ' + failedNames : ''})`; color = 'var(--red)';
    } else {
      msg = `Done — ${finished}/${total} proxy files`; color = 'var(--muted)';
    }
    bars.innerHTML = '';
    const _msgSpan = document.createElement('span');
    _msgSpan.style.cssText = `font-size:11px;color:${color}`;
    _msgSpan.textContent = msg;
    bars.appendChild(_msgSpan);
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
        <span id="_proxy-file-label" style="color:var(--muted);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></span>
        <span style="color:var(--accent);font-weight:600">${finished}/${total}</span>
      </div>
      <div style="background:var(--bg3);border-radius:3px;height:3px;overflow:hidden">
        <div style="background:var(--accent);height:100%;width:${pct}%;transition:width 0.3s"></div>
      </div>`;
    const _lbl = bars.querySelector('#_proxy-file-label');
    if (_lbl) _lbl.textContent = st.current_file || 'Creating proxies…';
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

// ── S3 upload modal ───────────────────────────────────────────────────────────
let _s3FilePath = null, _s3Es = null, _s3FetchEs = null;

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
      loadMusicTracks();
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
