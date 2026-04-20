// ── YouTube upload ────────────────────────────────────────────────────────────

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
  let savedTitle = '', savedDesc = '', savedNotes = '';
  if (currentJobId) {
    try {
      const cfg = await api.get(`/api/job-config?dir=${encodeURIComponent(workdir)}`);
      savedTitle = cfg?.yt_title || '';
      savedDesc  = cfg?.yt_desc  || '';
      savedNotes = cfg?.yt_notes || '';
    } catch (_) {}
  }
  document.getElementById('yt-title').value = savedTitle || projectName;
  document.getElementById('yt-desc').value  = savedDesc  || _YT_DEFAULT_FOOTER;
  const _notesEl = document.getElementById('yt-notes'); if (_notesEl) _notesEl.value = savedNotes;
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
  const notes = document.getElementById('yt-notes')?.value?.trim() || '';
  await api.post(`/api/jobs/${currentJobId}/save-yt-meta`, { title, desc, notes });
  _ytMetaSaved = true;
}

function _ytFooterFromDesc(desc) {
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
  const notes = document.getElementById('yt-notes')?.value?.trim() || '';
  const res = await api.post(`/api/jobs/${currentJobId}/generate-yt-meta`, { project_name: projectName, footer, notes });
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
  btn.onclick = ytShortsUpload;
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

async function ytClearUrl() {
  if (!currentJobId || !_ytFileName) return;
  const resp = await api.post(`/api/jobs/${currentJobId}/youtube-url`, { filename: _ytFileName, url: '' });
  if (resp?.ok) {
    document.getElementById('yt-existing-url').value = '';
    document.getElementById('yt-status').textContent = '';
    loadResults(currentJobId);
  }
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
    loadResults(currentJobId);
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
      statusEl.textContent = '✓ ';
      const _ytLink = document.createElement('a');
      if (/^https?:\/\//i.test(s.url)) _ytLink.href = s.url;
      _ytLink.target = '_blank'; _ytLink.style.color = 'var(--accent)'; _ytLink.textContent = s.url;
      statusEl.appendChild(_ytLink);
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

// ── AI Chapters ───────────────────────────────────────────────────────────────

async function _generateYtChapters() {
  if (!currentJobId) return;
  const btn    = document.getElementById('btn-yt-chapters');
  const status = document.getElementById('yt-gen-status');
  btn.disabled = true;
  btn.textContent = '⏳ Analyzing…';
  status.textContent = 'CLIP zero-shot running…';
  try {
    const result = await api.post(`/api/jobs/${currentJobId}/generate-metadata`, {});
    if (!result?.chapters) throw new Error(result?.detail || 'No chapters returned');

    const desc = document.getElementById('yt-desc');
    const footer = _YT_DEFAULT_FOOTER;
    const existing = (desc.value || '').trim();

    // Prepend AI block before existing content (or footer)
    const aiBlock = result.description_block;
    if (existing && !existing.startsWith('Na tym filmie')) {
      desc.value = aiBlock + '\n\n' + existing;
    } else {
      desc.value = aiBlock + '\n\n' + footer;
    }

    status.textContent = `✓ ${result.chapters.length} chapters · ${result.detected.join(', ')}`;
    status.style.color = 'var(--green)';
    _ytMetaSave();
  } catch (e) {
    status.textContent = '✗ ' + e.message;
    status.style.color = 'var(--red)';
  } finally {
    btn.disabled = false;
    btn.textContent = '✦ AI Chapters';
    setTimeout(() => { status.textContent = ''; status.style.color = ''; }, 6000);
  }
}
