// ── Modern UI Upload Module ─────────────────────────────────────────────────
// All functions parameterized by jobId/workDir — no global scope coupling.

const _YT_DEFAULT_FOOTER = '#motorcyclelife #motovlog #adventurebike #ktm #roadtrip\nhttps://github.com/pawkor/ai-autoedit';
const _YT_SHORTS_FOOTER  = '#shorts #motorcycle #motovlog #adventurebike #ktm #roadtrip\nhttps://github.com/pawkor/ai-autoedit';

let _mYtFilePath = null, _mYtFileName = null, _mYtJobId = null, _mYtWorkDir = null;
let _mYtMetaSaved = true;
let _mYtsFilePath = null, _mYtsFileName = null, _mYtsJobId = null, _mYtsWorkDir = null;
let _mIgFilePath  = null, _mIgFileName  = null;

// ── Shared helpers ──────────────────────────────────────────────────────────

function _mYtProjectMeta(workDir) {
  const parts = (workDir || '').replace(/\\/g, '/').split('/').filter(Boolean);
  let year = '', location = '';
  for (const p of parts) {
    if (/^\d{4}$/.test(p)) { year = p; continue; }
    if (year && /^\d{2}-/.test(p)) { location = p.replace(/^\d{2}-/, ''); break; }
  }
  return [year, location].filter(Boolean).join(' ');
}

function _mYtFooterFromDesc(desc) {
  const lines = (desc || '').trimEnd().split('\n');
  let i = lines.length - 1;
  while (i >= 0 && /^(\s*|#\S+(\s+#\S+)*|https?:\/\/\S+)(\s+.*)?$/.test(lines[i])) i--;
  return lines.slice(i + 1).join('\n').trim() || _YT_DEFAULT_FOOTER;
}

async function _mLoadPlaylists(selId) {
  const sel = document.getElementById(selId);
  if (!sel) return;
  sel.innerHTML = '<option value="">— None —</option>';
  try {
    const lists = await api.get('/api/youtube/playlists');
    if (Array.isArray(lists)) {
      for (const pl of lists) {
        const opt = document.createElement('option');
        opt.value = pl.id;
        opt.textContent = pl.title;
        sel.appendChild(opt);
      }
    }
  } catch (_) {}
}

function _mPollYtUpload(uploadId, statusEl, btn, onClose, onUrl) {
  let ticks = 0;
  const poll = setInterval(async () => {
    if (++ticks > 150) {
      clearInterval(poll);
      statusEl.textContent = '⚠ Upload timed out — check YouTube Studio';
      statusEl.style.color = 'var(--red)';
      btn.disabled = false; btn.textContent = '▲ Upload';
      return;
    }
    let s;
    try { s = await api.get(`/api/youtube/upload/${uploadId}`); } catch (_) { return; }
    if (!s) return;
    if (s.status === 'uploading') {
      const spd = s.speed_mbps ? ` · ${s.speed_mbps} Mbps` : '';
      statusEl.textContent = `Uploading… ${s.pct}%${spd}`;
      statusEl.style.color = 'var(--muted)';
    } else if (s.status === 'done') {
      clearInterval(poll);
      statusEl.innerHTML = '✓ ';
      const a = document.createElement('a');
      if (/^https?:\/\//i.test(s.url)) a.href = s.url;
      a.target = '_blank'; a.style.color = 'var(--green-hi)'; a.textContent = s.url;
      statusEl.appendChild(a);
      statusEl.style.color = 'var(--green-hi)';
      btn.textContent = '✓ Done'; btn.disabled = false;
      btn.onclick = onClose;
      if (onUrl) onUrl(s.url);
      if (typeof loadResults === 'function') loadResults();
    } else if (s.status === 'error') {
      clearInterval(poll);
      statusEl.textContent = '⚠ ' + (s.error || 'upload failed');
      statusEl.style.color = 'var(--red)';
      btn.disabled = false; btn.textContent = '▲ Upload';
    }
  }, 2000);
}

// ── YT Main modal ───────────────────────────────────────────────────────────

async function mYtOpen(filePath, fileName, existingUrl, jobId, workDir) {
  const ytStatus = await api.get('/api/youtube/status');
  if (!ytStatus?.authenticated) {
    alert('Connect YouTube first (Settings ⚙ → YouTube)');
    return;
  }
  _mYtFilePath = filePath; _mYtFileName = fileName;
  _mYtJobId = jobId; _mYtWorkDir = workDir;

  const projectName = _mYtProjectMeta(workDir)
    || (workDir || '').split('/').filter(Boolean).pop()
    || fileName.replace(/\.mp4$/i, '');

  document.getElementById('m-yt-filename').textContent = fileName;
  document.getElementById('m-yt-existing-url').value = existingUrl || '';
  document.getElementById('m-yt-gen-status').textContent = '';
  document.getElementById('m-yt-status').textContent = '';

  let savedTitle = '', savedDesc = '', savedNotes = '';
  if (jobId && workDir) {
    try {
      const cfg = await api.get(`/api/job-config?dir=${encodeURIComponent(workDir)}`);
      savedTitle = cfg?.yt_title || '';
      savedDesc  = cfg?.yt_desc  || '';
      savedNotes = cfg?.yt_notes || '';
    } catch (_) {}
  }

  document.getElementById('m-yt-title').value = savedTitle || projectName;
  document.getElementById('m-yt-desc').value  = savedDesc  || _YT_DEFAULT_FOOTER;
  document.getElementById('m-yt-notes').value = savedNotes;
  document.querySelector('input[name="m-yt-privacy"][value="unlisted"]').checked = true;
  document.getElementById('m-yt-new-playlist').style.display = 'none';
  document.getElementById('m-yt-new-playlist').value = '';

  const btn = document.getElementById('m-yt-upload-btn');
  btn.disabled = false; btn.textContent = '▲ Upload'; btn.onclick = mYtUpload;
  _mYtMetaSaved = true;

  document.getElementById('m-yt-modal').style.display = 'flex';
  await _mLoadPlaylists('m-yt-playlist');
}
window.mYtOpen = mYtOpen;

function mYtClose() { document.getElementById('m-yt-modal').style.display = 'none'; }
window.mYtClose = mYtClose;

function mYtMetaDirty() { _mYtMetaSaved = false; }
window.mYtMetaDirty = mYtMetaDirty;

async function mYtMetaSave() {
  if (_mYtMetaSaved || !_mYtJobId) return;
  const title = document.getElementById('m-yt-title').value.trim();
  const desc  = document.getElementById('m-yt-desc').value.trim();
  const notes = document.getElementById('m-yt-notes').value.trim();
  try {
    await api.post(`/api/jobs/${_mYtJobId}/save-yt-meta`, { title, desc, notes });
    _mYtMetaSaved = true;
  } catch (_) {}
}
window.mYtMetaSave = mYtMetaSave;

async function mYtGenDesc() {
  if (!_mYtJobId) return;
  const btn = document.getElementById('m-yt-gen-btn');
  const st  = document.getElementById('m-yt-gen-status');
  btn.disabled = true; st.textContent = 'generating…'; st.style.color = 'var(--muted)';
  const projectName = document.getElementById('m-yt-title').value.trim()
    || _mYtProjectMeta(_mYtWorkDir)
    || (_mYtWorkDir || '').split('/').filter(Boolean).pop() || '';
  const footer = _mYtFooterFromDesc(document.getElementById('m-yt-desc').value);
  const notes  = document.getElementById('m-yt-notes').value.trim();
  try {
    const res = await api.post(`/api/jobs/${_mYtJobId}/generate-yt-meta`,
      { project_name: projectName, footer, notes });
    if (res?.ok) {
      document.getElementById('m-yt-desc').value = res.description;
      st.textContent = ''; _mYtMetaSaved = false; mYtMetaSave();
    } else {
      st.textContent = '⚠ ' + (res?.error || 'failed'); st.style.color = 'var(--red)';
    }
  } catch (e) {
    st.textContent = '⚠ ' + e.message; st.style.color = 'var(--red)';
  } finally {
    btn.disabled = false;
  }
}
window.mYtGenDesc = mYtGenDesc;

async function mYtChapters() {
  if (!_mYtJobId) return;
  const btn    = document.getElementById('m-yt-chapters-btn');
  const status = document.getElementById('m-yt-gen-status');
  btn.disabled = true; btn.textContent = '⏳ Analyzing…';
  status.textContent = 'CLIP zero-shot running…'; status.style.color = 'var(--muted)';
  try {
    const result = await api.post(`/api/jobs/${_mYtJobId}/generate-metadata`, {});
    if (!result?.chapters) throw new Error(result?.detail || 'No chapters returned');
    const desc = document.getElementById('m-yt-desc');
    const existing = (desc.value || '').trim();
    desc.value = existing && !existing.startsWith('Na tym filmie')
      ? result.description_block + '\n\n' + existing
      : result.description_block + '\n\n' + _YT_DEFAULT_FOOTER;
    status.textContent = `✓ ${result.chapters.length} chapters · ${result.detected.join(', ')}`;
    status.style.color = 'var(--green-hi)';
    _mYtMetaSaved = false; mYtMetaSave();
  } catch (e) {
    status.textContent = '✗ ' + e.message; status.style.color = 'var(--red)';
  } finally {
    btn.disabled = false; btn.textContent = '✦ AI Chapters';
    setTimeout(() => { status.textContent = ''; status.style.color = ''; }, 6000);
  }
}
window.mYtChapters = mYtChapters;

async function mYtSaveUrl() {
  const url = document.getElementById('m-yt-existing-url').value.trim();
  if (!url || !_mYtJobId || !_mYtFileName) return;
  const status = document.getElementById('m-yt-status');
  const resp = await api.post(`/api/jobs/${_mYtJobId}/youtube-url`,
    { filename: _mYtFileName, url });
  if (resp?.ok) {
    status.innerHTML = '';
    const sp = document.createElement('span');
    sp.textContent = '✓ Linked: ';
    const a = document.createElement('a');
    if (/^https?:\/\//i.test(url)) a.href = url;
    a.target = '_blank'; a.style.color = 'var(--green-hi)'; a.textContent = url;
    sp.appendChild(a); status.appendChild(sp);
    status.style.color = 'var(--green-hi)';
    if (typeof loadResults === 'function') loadResults();
  } else {
    status.textContent = 'Save failed'; status.style.color = 'var(--red)';
  }
}
window.mYtSaveUrl = mYtSaveUrl;

async function mYtClearUrl() {
  if (!_mYtJobId || !_mYtFileName) return;
  const resp = await api.post(`/api/jobs/${_mYtJobId}/youtube-url`,
    { filename: _mYtFileName, url: '' });
  if (resp?.ok) {
    document.getElementById('m-yt-existing-url').value = '';
    document.getElementById('m-yt-status').textContent = '';
    if (typeof loadResults === 'function') loadResults();
  }
}
window.mYtClearUrl = mYtClearUrl;

function mYtToggleNewPlaylist() {
  const inp = document.getElementById('m-yt-new-playlist');
  const vis = inp.style.display !== 'none';
  inp.style.display = vis ? 'none' : '';
  if (!vis) { inp.focus(); document.getElementById('m-yt-playlist').value = ''; }
}
window.mYtToggleNewPlaylist = mYtToggleNewPlaylist;

async function mYtUpload() {
  if (!_mYtFilePath) return;
  const title = document.getElementById('m-yt-title').value.trim();
  if (!title) { alert('Enter a title'); return; }
  const privacy    = document.querySelector('input[name="m-yt-privacy"]:checked')?.value || 'unlisted';
  const playlistId = document.getElementById('m-yt-new-playlist').style.display !== 'none'
    ? null : (document.getElementById('m-yt-playlist').value || null);
  const newPlaylist = document.getElementById('m-yt-new-playlist').value.trim() || null;

  const btn    = document.getElementById('m-yt-upload-btn');
  const status = document.getElementById('m-yt-status');
  btn.disabled = true; btn.textContent = 'Uploading…';
  status.textContent = 'Starting…'; status.style.color = 'var(--muted)';

  const resp = await api.post('/api/youtube/upload', {
    file_path: _mYtFilePath, title,
    description: document.getElementById('m-yt-desc').value,
    privacy, playlist_id: playlistId, new_playlist: newPlaylist,
  });

  if (!resp?.upload_id) {
    status.textContent = '⚠ ' + (resp?.detail || 'failed to start');
    status.style.color = 'var(--red)';
    btn.disabled = false; btn.textContent = '▲ Upload';
    return;
  }

  _mPollYtUpload(resp.upload_id, status, btn, mYtClose,
    url => { document.getElementById('m-yt-existing-url').value = url; });
}
window.mYtUpload = mYtUpload;

// ── YT Shorts modal ─────────────────────────────────────────────────────────

async function mYtsOpen(filePath, fileName, jobId, workDir) {
  const ytStatus = await api.get('/api/youtube/status');
  if (!ytStatus?.authenticated) {
    alert('Connect YouTube first (Settings ⚙ → YouTube)');
    return;
  }
  _mYtsFilePath = filePath; _mYtsFileName = fileName;
  _mYtsJobId = jobId; _mYtsWorkDir = workDir;

  const projectName = _mYtProjectMeta(workDir)
    || (workDir || '').split('/').filter(Boolean).pop()
    || fileName.replace(/-short_v\d+\.mp4$/i, '');

  const mainVideos = [];
  if (jobId) {
    try {
      const results = await api.get(`/api/jobs/${jobId}/result`);
      for (const [name, info] of Object.entries(results || {})) {
        if (!/short/i.test(name) && info.yt_url) mainVideos.push({ name, url: info.yt_url });
      }
    } catch (_) {}
  }
  const mainVideoUrl = mainVideos[0]?.url || null;

  const selRow = document.getElementById('m-yts-fullvideo-row');
  const selEl  = document.getElementById('m-yts-fullvideo-select');
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

  let titleVal = projectName;
  if (jobId && workDir) {
    try {
      const cfg = await api.get(`/api/job-config?dir=${encodeURIComponent(workDir)}`);
      if (cfg?.title) titleVal = cfg.title.split('\n')[0].trim();
    } catch (_) {}
  }

  document.getElementById('m-yts-filename').textContent = fileName;
  document.getElementById('m-yts-title').value = titleVal;
  const linkLine = mainVideoUrl ? `Full video: ${mainVideoUrl}` : '';
  document.getElementById('m-yts-desc').value = linkLine
    ? `${linkLine}\n\n${_YT_SHORTS_FOOTER}` : _YT_SHORTS_FOOTER;
  document.querySelector('input[name="m-yts-privacy"][value="public"]').checked = true;
  document.getElementById('m-yts-new-playlist').style.display = 'none';
  document.getElementById('m-yts-new-playlist').value = '';
  document.getElementById('m-yts-gen-status').textContent = '';

  const statusEl = document.getElementById('m-yts-status');
  const btn = document.getElementById('m-yts-upload-btn');
  btn.textContent = '▲ Upload'; btn.onclick = mYtsUpload;
  if (!mainVideoUrl) {
    statusEl.textContent = '⚠ Main video not yet published on YouTube — upload it first to include a link.';
    statusEl.style.color = 'var(--yellow)';
    btn.disabled = true;
  } else {
    statusEl.textContent = ''; statusEl.style.color = '';
    btn.disabled = false;
  }

  document.getElementById('m-yts-modal').style.display = 'flex';
  await _mLoadPlaylists('m-yts-playlist');
}
window.mYtsOpen = mYtsOpen;

function mYtsClose() { document.getElementById('m-yts-modal').style.display = 'none'; }
window.mYtsClose = mYtsClose;

function mYtsUpdateLink() {
  const url  = document.getElementById('m-yts-fullvideo-select').value;
  const desc = document.getElementById('m-yts-desc');
  const lines = desc.value.split('\n');
  const idx = lines.findIndex(l => l.startsWith('Full video:'));
  const newLine = url ? `Full video: ${url}` : '';
  if (idx >= 0) {
    if (newLine) lines[idx] = newLine; else lines.splice(idx, 1);
  } else if (newLine) {
    lines.unshift(newLine, '');
  }
  desc.value = lines.join('\n');
  const btn = document.getElementById('m-yts-upload-btn');
  const statusEl = document.getElementById('m-yts-status');
  if (!url) {
    statusEl.textContent = '⚠ Main video not yet published on YouTube — upload it first to include a link.';
    statusEl.style.color = 'var(--yellow)';
    btn.disabled = true;
  } else {
    statusEl.textContent = ''; statusEl.style.color = '';
    btn.disabled = false; btn.textContent = '▲ Upload';
  }
}
window.mYtsUpdateLink = mYtsUpdateLink;

function mYtsToggleNewPlaylist() {
  const inp = document.getElementById('m-yts-new-playlist');
  const vis = inp.style.display !== 'none';
  inp.style.display = vis ? 'none' : '';
  if (!vis) { inp.focus(); document.getElementById('m-yts-playlist').value = ''; }
}
window.mYtsToggleNewPlaylist = mYtsToggleNewPlaylist;

async function mYtsGenDesc() {
  if (!_mYtsJobId) return;
  const btn = document.getElementById('m-yts-gen-btn');
  const st  = document.getElementById('m-yts-gen-status');
  btn.disabled = true; st.textContent = 'generating…'; st.style.color = 'var(--muted)';
  const projectName = document.getElementById('m-yts-title').value.trim() || '';
  const footer = _mYtFooterFromDesc(document.getElementById('m-yts-desc').value) || _YT_SHORTS_FOOTER;
  try {
    const res = await api.post(`/api/jobs/${_mYtsJobId}/generate-yt-meta`,
      { project_name: projectName, footer });
    if (res?.ok) {
      document.getElementById('m-yts-desc').value = res.description;
      st.textContent = '';
    } else {
      st.textContent = '⚠ ' + (res?.error || 'failed'); st.style.color = 'var(--red)';
    }
  } catch (e) {
    st.textContent = '⚠ ' + e.message; st.style.color = 'var(--red)';
  } finally {
    btn.disabled = false;
  }
}
window.mYtsGenDesc = mYtsGenDesc;

async function mYtsUpload() {
  if (!_mYtsFilePath) return;
  const title = document.getElementById('m-yts-title').value.trim();
  if (!title) { alert('Enter a title'); return; }
  const privacy    = document.querySelector('input[name="m-yts-privacy"]:checked')?.value || 'public';
  const playlistId = document.getElementById('m-yts-new-playlist').style.display !== 'none'
    ? null : (document.getElementById('m-yts-playlist').value || null);
  const newPlaylist = document.getElementById('m-yts-new-playlist').value.trim() || null;

  const status = document.getElementById('m-yts-status');
  const btn    = document.getElementById('m-yts-upload-btn');
  btn.disabled = true; btn.textContent = 'Uploading…';
  status.textContent = 'Starting…'; status.style.color = 'var(--muted)';

  const res = await api.post('/api/youtube/upload', {
    file_path: _mYtsFilePath, title,
    description: document.getElementById('m-yts-desc').value,
    privacy, playlist_id: playlistId, new_playlist: newPlaylist,
  });

  if (!res?.upload_id) {
    btn.disabled = false; btn.textContent = '▲ Upload';
    status.textContent = '⚠ ' + (res?.detail || 'Upload failed');
    status.style.color = 'var(--red)';
    return;
  }
  _mPollYtUpload(res.upload_id, status, btn, mYtsClose);
}
window.mYtsUpload = mYtsUpload;

// ── IG Reel modal ───────────────────────────────────────────────────────────

async function mIgOpen(filePath, fileName, ncsAttr, jobId) {
  const status = await api.get('/api/ig/status');
  if (!status?.configured) {
    alert('Instagram not configured.\nSet IG_ACCESS_TOKEN and IG_USER_ID in .env and restart the server.');
    return;
  }
  _mIgFilePath = filePath; _mIgFileName = fileName;

  document.getElementById('m-ig-filename').textContent = fileName;
  document.getElementById('m-ig-status').textContent = '';

  const tokenWarn = document.getElementById('m-ig-token-warn');
  if (status.days_until_expiry != null && status.days_until_expiry <= 5) {
    tokenWarn.textContent = `⚠ IG token expires in ${Math.ceil(status.days_until_expiry)} day(s) — auto-refresh attempted at startup`;
    tokenWarn.style.display = '';
  } else {
    tokenWarn.style.display = 'none';
  }

  const warn = document.getElementById('m-ig-cooldown-warn');
  const btn  = document.getElementById('m-ig-upload-btn');
  if (!status.ready) {
    const rem = Math.ceil((status.cooldown_remaining_h || 0) * 60);
    warn.textContent = `⚠ Cooldown active — ${rem} min until next upload (min ${status.min_hours}h between posts)`;
    warn.style.display = '';
    btn.disabled = true;
  } else {
    warn.style.display = 'none';
    btn.disabled = false;
  }

  const hashtags = '#reels #motorcycle #motovlog #ktm #adventurebike #roadtrip';
  const repoUrl  = 'https://github.com/pawkor/ai-autoedit';
  document.getElementById('m-ig-caption').value = ncsAttr
    ? `Music: ${ncsAttr} (NCS Release)\n\n${hashtags}\n${repoUrl}`
    : `${hashtags}\n${repoUrl}`;

  btn.textContent = '▲ Upload'; btn.onclick = mIgUpload;
  document.getElementById('m-ig-modal').style.display = 'flex';
}
window.mIgOpen = mIgOpen;

function mIgClose() { document.getElementById('m-ig-modal').style.display = 'none'; }
window.mIgClose = mIgClose;

async function mIgUpload() {
  const btn    = document.getElementById('m-ig-upload-btn');
  const status = document.getElementById('m-ig-status');
  const caption = document.getElementById('m-ig-caption').value.trim();
  btn.disabled = true; btn.textContent = 'Uploading…';
  status.textContent = 'Submitting…'; status.style.color = 'var(--muted)';

  const res = await api.post('/api/ig/upload', { file_path: _mIgFilePath, caption });
  if (!res?.upload_id) {
    status.textContent = '⚠ ' + (res?.detail || 'Failed to start upload');
    status.style.color = 'var(--red)';
    btn.disabled = false; btn.textContent = '▲ Upload';
    return;
  }

  let igTicks = 0;
  const poll = setInterval(async () => {
    if (++igTicks > 60) {
      clearInterval(poll);
      status.textContent = '⚠ Upload timed out — check Instagram';
      status.style.color = 'var(--red)';
      btn.disabled = false; btn.textContent = '▲ Upload';
      return;
    }
    let s;
    try { s = await api.get(`/api/ig/upload/${res.upload_id}`); } catch (_) { return; }
    if (!s) return;
    status.textContent = s.message || s.status;
    if (s.status === 'done') {
      clearInterval(poll);
      status.innerHTML = '';
      const a = document.createElement('a');
      a.href = s.url; a.target = '_blank'; a.style.color = 'var(--green-hi)';
      a.textContent = '✓ ' + s.url;
      status.appendChild(a); status.style.color = 'var(--green-hi)';
      btn.disabled = false; btn.textContent = '✓ Done';
      btn.onclick = mIgClose;
      if (typeof loadResults === 'function') loadResults();
    } else if (s.status === 'error') {
      clearInterval(poll);
      status.textContent = '⚠ ' + s.message; status.style.color = 'var(--red)';
      btn.disabled = false; btn.textContent = '▲ Retry';
    }
  }, 5000);
}
window.mIgUpload = mIgUpload;
