// modern_music.js — music modal, pin, rebuild trigger

let _allTracks = [];
let _musicDir  = '';
let _audioEl   = null;
let _playingFile = null;
let _seekTimer = null;
let _usedTracksIndex = {};

// ── Audio playback ────────────────────────────────────────────────────────────
function _fmtTime(s) {
  s = Math.floor(s || 0);
  return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
}

function _getAudio() {
  if (!_audioEl) {
    _audioEl = new Audio();
    _audioEl.onended = () => { _playingFile = null; _refreshPlayButtons(); _hideSeekBar(); };
  }
  return _audioEl;
}

function _showSeekBar(a) {
  const row = document.getElementById('m-music-seekbar-row');
  const bar = document.getElementById('m-music-seekbar');
  const durEl = document.getElementById('m-music-seek-dur');
  if (!row) return;
  row.style.display = 'flex';
  a.addEventListener('loadedmetadata', () => {
    if (bar) bar.max = Math.floor(a.duration);
    if (durEl) durEl.textContent = _fmtTime(a.duration);
  });
  if (_seekTimer) clearInterval(_seekTimer);
  _seekTimer = setInterval(() => {
    if (!_audioEl || _audioEl.paused) return;
    if (bar) bar.value = Math.floor(_audioEl.currentTime);
    const curEl = document.getElementById('m-music-seek-cur');
    if (curEl) curEl.textContent = _fmtTime(_audioEl.currentTime);
  }, 300);
}

function _hideSeekBar() {
  if (_seekTimer) { clearInterval(_seekTimer); _seekTimer = null; }
  const row = document.getElementById('m-music-seekbar-row');
  if (row) row.style.display = 'none';
}

function _musicSeek(val) {
  if (_audioEl) _audioEl.currentTime = +val;
}
window._musicSeek = _musicSeek;

function togglePlay(file) {
  const a = _getAudio();
  if (_playingFile === file) {
    a.pause();
    _playingFile = null;
    _refreshPlayButtons();
    _hideSeekBar();
    return;
  }
  a.pause();
  _playingFile = file;
  a.src = '/api/file?path=' + encodeURIComponent(file);
  a.play().catch(() => {});
  _showSeekBar(a);
  _refreshPlayButtons();
}

function _refreshPlayButtons() {
  document.querySelectorAll('.m-mtrack-play').forEach(btn => {
    const playing = btn.dataset.file === _playingFile;
    btn.textContent = playing ? '■' : '▶';
    btn.classList.toggle('playing', playing);
  });
}

// ── ACR copyright check ───────────────────────────────────────────────────────
async function acrCheckTrack(file, badgeEl) {
  badgeEl.textContent = '…';
  badgeEl.className = 'm-mtrack-acr checking';
  const result = await window._modernApi.post('/api/music/acr-check', { path: file });
  if (!result) { badgeEl.textContent = '?'; badgeEl.className = 'm-mtrack-acr'; return; }
  const t = _allTracks.find(t => t.file === file);
  if (t) { t.acr_matched = result.matched; t.acr_blocked = result.blocked; }
  _applyAcrBadge(badgeEl, result.matched, result.blocked);
}

function _applyAcrBadge(el, matched, blocked) {
  if (blocked)       { el.textContent = '©✗'; el.className = 'm-mtrack-acr blocked'; el.title = 'Blocked by Content ID'; }
  else if (matched)  { el.textContent = '©';  el.className = 'm-mtrack-acr matched';  el.title = 'Matched — may be restricted'; }
  else               { el.textContent = '✓';  el.className = 'm-mtrack-acr clear';    el.title = 'Clear'; }
}

// ── Modal ─────────────────────────────────────────────────────────────────────
function openMusicModal() {
  document.getElementById('m-music-modal').style.display = 'flex';
  _updateMusicTarget();
  if (_allTracks.length === 0 && _musicDir) loadMusicDirModal();
  else filterMusicModal();
}
window.openMusicModal = openMusicModal;

function closeMusicModal() {
  if (_audioEl) { _audioEl.pause(); _playingFile = null; }
  _hideSeekBar();
  _refreshPlayButtons();
  document.getElementById('m-music-modal').style.display = 'none';
}
window.closeMusicModal = closeMusicModal;

async function loadMusicDirModal() {
  const input = document.getElementById('m-music-dir-input');
  const dir = input ? input.value.trim() : _musicDir;
  if (!dir) return;
  _musicDir = dir;
  const tracks = await window._modernApi.get(`/api/music-files?dir=${encodeURIComponent(dir)}`);
  _allTracks = tracks || [];
  filterMusicModal();
  if (typeof _jobId !== 'undefined' && _jobId) {
    await window._modernApi.patch(`/api/jobs/${_jobId}/params`, { music_dir: dir });
  }
}
window.loadMusicDirModal = loadMusicDirModal;

function filterMusicModal() {
  const q = (document.getElementById('m-music-filter')?.value || '').toLowerCase();
  const filtered = q
    ? _allTracks.filter(t => {
        const text = ((t.title || '') + ' ' + (t.file || '').split('/').pop()).toLowerCase();
        const durStr = t.duration ? fmtSec(t.duration) : '';
        return text.includes(q) || durStr.startsWith(q);
      })
    : _allTracks;
  renderMusicModalList(filtered);
}
window.filterMusicModal = filterMusicModal;

function _updateMusicTarget() {
  const targetSec = _timeline.reduce((s, c) => s + (c.duration || 0), 0);
  const el = document.getElementById('m-music-target');
  if (el) el.textContent = targetSec > 0 ? `Target: ${fmtSec(targetSec)}` : '';
}

function renderMusicModalList(tracks) {
  const list = document.getElementById('m-music-modal-list');
  if (!list) return;
  const count = document.getElementById('m-music-count');
  if (count) count.textContent = `${tracks.length} tracks`;
  if (tracks.length === 0) {
    list.innerHTML = '<div class="m-empty">No tracks found.</div>';
    return;
  }
  const targetSec = _timeline.reduce((s, c) => s + (c.duration || 0), 0);
  list.innerHTML = '';
  tracks.forEach(t => {
    const isPinned = t.file === _pinnedTrack;
    const dur = t.duration || 0;
    const near = targetSec > 0 && Math.abs(dur - targetSec) / targetSec < 0.15;
    const fname = t.file.split('/').pop();
    const usedEntries = _usedTracksIndex[fname] || _usedTracksIndex[t.file] || [];

    const row = document.createElement('div');
    row.className = `m-mtrack${isPinned ? ' pinned' : ''}`;
    if (usedEntries.length) row.classList.add('used');
    row.onclick = () => pinTrack(t.file);

    // Play button
    const playBtn = document.createElement('button');
    playBtn.className = 'm-mtrack-play m-btn m-btn-ghost m-btn-sm';
    playBtn.dataset.file = t.file;
    playBtn.textContent = _playingFile === t.file ? '■' : '▶';
    if (_playingFile === t.file) playBtn.classList.add('playing');
    playBtn.onclick = e => { e.stopPropagation(); togglePlay(t.file, playBtn); };

    const info = document.createElement('div');
    info.className = 'm-mtrack-info';
    const title = document.createElement('div');
    title.className = 'm-mtrack-title';
    title.textContent = t.title || t.file.split('/').pop();
    info.appendChild(title);
    if (t.artist) {
      const artist = document.createElement('div');
      artist.className = 'm-mtrack-artist';
      artist.textContent = t.artist;
      info.appendChild(artist);
    }

    const durEl = document.createElement('div');
    durEl.className = 'm-mtrack-dur';
    durEl.textContent = fmtSec(dur);

    const bpmEl = document.createElement('div');
    bpmEl.className = 'm-mtrack-bpm';
    bpmEl.textContent = t.bpm ? Math.round(t.bpm) + ' BPM' : '';

    const nearEl = document.createElement('div');
    nearEl.className = 'm-mtrack-near';
    nearEl.textContent = near ? '≈' : '';

    // ACR badge
    const acrEl = document.createElement('div');
    if (t.acr_matched !== undefined) {
      _applyAcrBadge(acrEl, t.acr_matched, t.acr_blocked);
    } else {
      acrEl.className = 'm-mtrack-acr';
      acrEl.textContent = '©?';
      acrEl.title = 'Check copyright';
    }
    acrEl.onclick = e => { e.stopPropagation(); acrCheckTrack(t.file, acrEl); };

    row.appendChild(playBtn);
    row.appendChild(info);
    row.appendChild(durEl);
    row.appendChild(bpmEl);
    row.appendChild(nearEl);
    const usedBadge = document.createElement('div');
    usedBadge.className = 'm-mtrack-used';
    if (usedEntries.length) {
      usedBadge.textContent = '✓ used';
      usedBadge.title = usedEntries.map(e =>
        `${(e.project || '').split('/').pop()}  ${e.render || ''}  ${e.date || ''}${e.yt_url ? '  YT: ' + e.yt_url : ''}`
      ).join('\n');
    }
    row.appendChild(usedBadge);
    row.appendChild(acrEl);
    list.appendChild(row);
  });
}

// ── Music directory browser ───────────────────────────────────────────────────
let _musicBrowserOpen = false;

function musicToggleBrowser() {
  if (_musicBrowserOpen) { _musicCloseBrowser(); return; }
  _musicBrowserOpen = true;
  const dir = document.getElementById('m-music-dir-input')?.value.trim();
  _loadMusicBrowser(dir || null);
}
window.musicToggleBrowser = musicToggleBrowser;

function _musicCloseBrowser() {
  _musicBrowserOpen = false;
  const el = document.getElementById('m-music-browser');
  if (el) el.style.display = 'none';
}

async function _loadMusicBrowser(path) {
  const el = document.getElementById('m-music-browser');
  if (!el) return;
  el.style.display = '';
  const entries = document.getElementById('m-music-browser-entries');
  if (entries) entries.innerHTML = '<div style="padding:4px;color:var(--muted)">Loading…</div>';

  const url = path ? `/api/browse?path=${encodeURIComponent(path)}` : '/api/browse';
  const data = await window._modernApi.get(url);
  if (!data) {
    if (entries) entries.innerHTML = '<div style="padding:4px;color:var(--red)">Error loading directory</div>';
    return;
  }

  const pathEl = document.getElementById('m-music-browser-path');
  if (pathEl) {
    pathEl.innerHTML = '';
    if (data.parent) {
      const up = document.createElement('button');
      up.className = 'm-btn m-btn-ghost m-btn-sm';
      up.textContent = '↑ ..';
      up.onclick = () => _loadMusicBrowser(data.parent);
      pathEl.appendChild(up);
    }
    const span = document.createElement('span');
    span.style.marginLeft = '4px';
    span.textContent = data.path;
    pathEl.appendChild(span);
  }

  if (!entries) return;
  entries.innerHTML = '';

  const selBtn = document.createElement('div');
  selBtn.className = 'm-mtrack-row';
  selBtn.style.cssText = 'cursor:pointer;font-weight:600;color:var(--blue)';
  selBtn.textContent = '✓ Select this folder';
  selBtn.onclick = () => _selectMusicBrowserPath(data.path);
  entries.appendChild(selBtn);

  for (const e of data.entries) {
    if (!e.is_dir) continue;
    const row = document.createElement('div');
    row.className = 'm-mtrack-row';
    row.style.cssText = 'cursor:pointer;display:flex;align-items:center;gap:6px';
    const icon = document.createElement('span');
    icon.style.color = 'var(--muted)'; icon.textContent = '📁';
    const name = document.createElement('span');
    name.style.flex = '1'; name.textContent = e.name;
    row.appendChild(icon); row.appendChild(name);
    row.onclick = () => _loadMusicBrowser(e.path);
    entries.appendChild(row);
  }
}

async function _selectMusicBrowserPath(path) {
  const input = document.getElementById('m-music-dir-input');
  if (input) input.value = path;
  _musicCloseBrowser();
  _musicDir = path;
  await loadMusicDirModal();
}

async function _onMusicDirBlur(dir) {
  if (!dir || dir === _musicDir) return;
  _musicDir = dir;
  await loadMusicDirModal();
}
window._onMusicDirBlur = _onMusicDirBlur;

// ── Rebuild music index ───────────────────────────────────────────────────────
async function rebuildMusicIndex(force = false) {
  if (!_musicDir) { alert('Load a music directory first.'); return; }
  const btn      = document.getElementById('m-btn-rebuild-idx');
  const forceBtn = document.getElementById('m-btn-force-idx');
  const statusEl = document.getElementById('m-music-rebuild-status');
  const allBtns  = [btn, forceBtn].filter(Boolean);

  const _setBusy = (label) => {
    allBtns.forEach(b => { b.disabled = true; });
    if (btn)      btn.textContent      = force ? '↺ Rebuild' : '⟳ …';
    if (forceBtn) forceBtn.textContent = force ? '⟳ …'       : '⟳ Force';
    if (statusEl) { statusEl.textContent = label; statusEl.style.display = ''; statusEl.style.color = ''; }
  };
  const _setIdle = (label, ok) => {
    allBtns.forEach(b => { b.disabled = false; });
    if (btn)      btn.textContent      = '↺ Rebuild';
    if (forceBtn) forceBtn.textContent = '⟳ Force';
    if (statusEl) {
      statusEl.textContent = label;
      statusEl.style.color = ok ? 'var(--green-hi, #4ade80)' : 'var(--red)';
      setTimeout(() => { if (statusEl) { statusEl.textContent = ''; statusEl.style.display = 'none'; } }, 3000);
    }
  };

  _setBusy('Starting…');
  const resp = await window._modernApi.post('/api/music-rebuild', { dir: _musicDir, force });
  if (!resp?.task_id) { _setIdle('✗ Failed to start', false); return; }

  const taskId = resp.task_id;
  const poll = setInterval(async () => {
    const s = await window._modernApi.get(`/api/music-rebuild-status/${taskId}`);
    if (!s) return;
    _setBusy(s.total > 0 ? `${s.progress}/${s.total}` : 'Working…');
    if (s.done) {
      clearInterval(poll);
      _setIdle(s.ok ? '✓ Done' : '✗ Failed', s.ok);
      if (s.ok) await loadMusicDirModal();
    }
  }, 800);
}
window.rebuildMusicIndex = rebuildMusicIndex;

// ── Pinned track ──────────────────────────────────────────────────────────────
function _updatePinnedInfo() {
  const t = _allTracks.find(t => t.file === _pinnedTrack);
  const info    = document.getElementById('m-pinned-info');
  const titleEl = document.getElementById('m-pinned-title');
  const durEl   = document.getElementById('m-pinned-dur');
  const label   = document.getElementById('m-music-label');
  if (_pinnedTrack) {
    const name = t?.title || _pinnedTrack.split('/').pop();
    if (info)    info.style.display = '';
    if (titleEl) titleEl.textContent = name;
    if (durEl)   durEl.textContent = t ? fmtSec(t.duration || 0) : '';
    if (label)   label.textContent = name;
  } else {
    if (info)  info.style.display = 'none';
    if (label) label.textContent = 'no track selected';
  }
}

async function pinTrack(file) {
  _pinnedTrack = (_pinnedTrack === file) ? null : file;
  _updatePinnedInfo();
  filterMusicModal();
  const rebuild = document.getElementById('m-btn-rebuild');
  if (rebuild) rebuild.disabled = !_pinnedTrack;
  if (_pinnedTrack) {
    closeMusicModal();
    await rebuildTimeline();
  }
}
window.pinTrack = pinTrack;

// ── Load on project open ──────────────────────────────────────────────────────
async function loadMusicList(jobId) {
  const jobData = await window._modernApi.get(`/api/jobs/${jobId}`);
  _musicDir = jobData?.params?.music_dir || '';

  const dirInput = document.getElementById('m-music-dir-input');
  if (dirInput && _musicDir) dirInput.value = _musicDir;

  const usedRaw = await window._modernApi.get('/api/music/used-tracks').catch(() => null);
  _usedTracksIndex = usedRaw || {};

  if (_musicDir) {
    const tracks = await window._modernApi.get(`/api/music-files?dir=${encodeURIComponent(_musicDir)}`);
    _allTracks = tracks || [];
  } else {
    _allTracks = [];
  }

  if (jobData?.params?.selected_track) {
    _pinnedTrack = jobData.params.selected_track;
  }

  _updatePinnedInfo();

  if (_pinnedTrack) {
    const rebuild = document.getElementById('m-btn-rebuild');
    if (rebuild) rebuild.disabled = false;
    // only auto-build if no saved timeline was restored
    if (_timeline.length === 0) await rebuildTimeline();
    else { drawTimeline(); renderPool(); enableActions(true); }
  }
}
window.loadMusicList = loadMusicList;

// ── YT-DLP download ───────────────────────────────────────────────────────────
let _musicYtEs = null;

function musicYtDownload() {
  const url = document.getElementById('m-music-yt-url').value.trim();
  if (!url) return;
  const msgEl = document.getElementById('m-music-yt-msg');
  const pctEl = document.getElementById('m-music-yt-pct');
  msgEl.textContent = 'Starting…';
  pctEl.textContent = '';
  _musicYtEs?.close();
  _musicYtEs = new EventSource(`/api/music/yt-download?url=${encodeURIComponent(url)}`);
  _musicYtEs.onmessage = e => {
    const d = JSON.parse(e.data);
    if (d.msg) msgEl.textContent = d.msg.replace(/^\[.*?\]\s*/, '');
    if (d.pct != null) pctEl.textContent = Math.round(d.pct) + '%';
    if (d.done) {
      _musicYtEs.close(); _musicYtEs = null;
      pctEl.textContent = '';
      document.getElementById('m-music-yt-url').value = '';
      _musicYtSave(d.path, d.name, msgEl, pctEl);
    }
    if (d.error) {
      _musicYtEs.close(); _musicYtEs = null;
      msgEl.textContent = '✗ ' + d.error.split('\n').pop();
      pctEl.textContent = '';
    }
  };
  _musicYtEs.onerror = () => {
    _musicYtEs?.close(); _musicYtEs = null;
    document.getElementById('m-music-yt-msg').textContent = 'Connection error';
  };
}
window.musicYtDownload = musicYtDownload;

async function _musicYtSave(tmpPath, name, msgEl, pctEl) {
  const musicDir = document.getElementById('m-music-dir-input').value.trim();
  if (!musicDir) {
    _pinnedTrack = tmpPath;
    msgEl.textContent = '✓ ' + name + ' (set Music dir to save permanently)';
    _updatePinnedInfo();
    return;
  }
  const res = await window._modernApi.post('/api/music/save-downloaded',
    { tmp_path: tmpPath, music_dir: musicDir }
  ).catch(() => null);
  if (res?.ok) {
    _pinnedTrack = res.path;
    msgEl.textContent = '✓ ' + name;
    if (_jobId) await loadMusicList(_jobId);
  } else {
    _pinnedTrack = tmpPath;
    msgEl.textContent = '✓ ' + name + ' (save failed)';
    _updatePinnedInfo();
  }
}
