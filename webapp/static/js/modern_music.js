// modern_music.js — music modal, pin, rebuild trigger

let _allTracks = [];
let _musicDir  = '';
let _audioEl   = null;   // singleton for music preview playback
let _playingFile = null; // currently playing track file path

// ── Audio playback ────────────────────────────────────────────────────────────
function _getAudio() {
  if (!_audioEl) {
    _audioEl = new Audio();
    _audioEl.onended = () => { _playingFile = null; _refreshPlayButtons(); };
  }
  return _audioEl;
}

function togglePlay(file, btn) {
  const a = _getAudio();
  if (_playingFile === file) {
    a.pause();
    _playingFile = null;
    _refreshPlayButtons();
    return;
  }
  a.pause();
  _playingFile = file;
  const src = file.startsWith('/data/') ? file : '/api/file?path=' + encodeURIComponent(file);
  a.src = src;
  a.play().catch(() => {});
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
    ? _allTracks.filter(t => (t.title || '').toLowerCase().includes(q)
        || (t.file || '').split('/').pop().toLowerCase().includes(q))
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

    const row = document.createElement('div');
    row.className = `m-mtrack${isPinned ? ' pinned' : ''}`;
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
    row.appendChild(acrEl);
    list.appendChild(row);
  });
}

// ── Rebuild music index ───────────────────────────────────────────────────────
async function rebuildMusicIndex() {
  if (!_musicDir) { alert('Load a music directory first.'); return; }
  const btn = document.getElementById('m-btn-rebuild-idx');
  if (btn) { btn.disabled = true; btn.textContent = 'Rebuilding…'; }
  const resp = await window._modernApi.post('/api/music-rebuild', { dir: _musicDir });
  if (btn) { btn.disabled = false; btn.textContent = '↺ Rebuild index'; }
  if (resp?.task_id) {
    await loadMusicDirModal();
  }
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
