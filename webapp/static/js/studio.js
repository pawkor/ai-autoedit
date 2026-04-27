// modern.js — AI-autoedit Studio NLE

// ── State ────────────────────────────────────────────────────────────────────
let _jobId       = null;
let _frames      = [];     // [{scene, score, duration, path}]
let _timeline    = [];     // [{scene, duration, clip_score, frame_url, energy}]
let _overrides   = {};     // {scene: 'ban'}
let _removedScenes = new Set(); // dragged from timeline to pool — dashed red, not yet banned
let _pinnedTrack = null;   // music file path (set by modern_music.js)
let _browseRoot  = '';     // stripped from work_dir paths for display
let _timelineMusic = null; // music path from last dry-run (for preview playback)
let _workDir     = '';     // current job work_dir (for clip_path reconstruction)
let _activeCameras = new Set(); // currently visible cameras in pool filter

// ── Timeline persistence (backend) ───────────────────────────────────────────
let _saveTlTimer = null;
let _savePatternTimer = null;
function _saveTimeline(jobId) {
  if (!jobId) return;
  clearTimeout(_saveTlTimer);
  _saveTlTimer = setTimeout(() => {
    api.patch(`/api/jobs/${jobId}/params`, {
      manual_timeline: _timeline,
      manual_overrides: _overrides,
    });
  }, 1500);
}
function _loadTimeline(job) {
  const tl = job?.params?.manual_timeline;
  if (Array.isArray(tl) && tl.length > 0) {
    _timeline  = tl;
    _overrides = job.params.manual_overrides || {};
    return true;
  }
  return false;
}

// ── API ───────────────────────────────────────────────────────────────────────
const api = {
  async get(url) {
    try { const r = await fetch(url); return r.ok ? r.json() : null; } catch { return null; }
  },
  async post(url, body = {}) {
    try {
      const r = await fetch(url, { method: 'POST',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      return r.ok ? r.json() : null;
    } catch { return null; }
  },
  async patch(url, body = {}) {
    try {
      const r = await fetch(url, { method: 'PATCH',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      return r.ok ? r.json() : null;
    } catch { return null; }
  },
  async del(url) {
    try {
      const r = await fetch(url, { method: 'DELETE' });
      return r.ok;
    } catch { return false; }
  },
};
window._modernApi = api;

// ── Helpers ───────────────────────────────────────────────────────────────────
function trimPath(p) {
  if (!p) return '';
  if (_browseRoot && p.startsWith(_browseRoot + '/')) return p.slice(_browseRoot.length + 1);
  return p.replace(/^\/home\/[^/]+\//, '');
}

// ── Project list ──────────────────────────────────────────────────────────────
let _projSortAsc = false;

function toggleProjectSort() {
  _projSortAsc = !_projSortAsc;
  const btn = document.getElementById('m-sort-btn');
  if (btn) btn.textContent = _projSortAsc ? 'z→a' : 'a→z';
  refreshProjectList();
}
window.toggleProjectSort = toggleProjectSort;

async function deleteProject(id, ev) {
  ev.stopPropagation();
  const job = (await api.get('/api/jobs') || []).find(j => j.id === id);
  const name = trimPath(job?.work_dir) || id;
  if (!confirm(`Remove project "${name}" from list?\n(Files on disk are not deleted)`)) return;
  await fetch(`/api/jobs/${id}/remove`, { method: 'POST' });
  if (_jobId === id) {
    _jobId = null;
    document.getElementById('m-project-name').textContent = 'No project selected';
  }
  refreshProjectList();
}
window.deleteProject = deleteProject;

async function refreshProjectList() {
  const data = await api.get('/api/jobs') || [];
  const sorted = [...data].sort((a, b) => {
    const cmp = (a.work_dir || '').localeCompare(b.work_dir || '');
    return _projSortAsc ? cmp : -cmp;
  });
  const list = document.getElementById('m-project-list');
  if (!list) return;
  list.innerHTML = sorted.map(j => {
    const name = trimPath(j.work_dir) || j.id;
    return `<div class="m-proj-item${j.id === _jobId ? ' active' : ''}"
                 data-id="${j.id}" onclick="openProject('${j.id}')">
              <span style="display:block;padding-right:20px;word-break:break-all;line-height:1.4">${name}</span>
              <button class="m-proj-del" onclick="deleteProject('${j.id}',event)" title="Remove project">✕</button>
            </div>`;
  }).join('');
}

async function openProject(id) {
  if (_jobId === id) return;
  _jobId = id;
  _frames = [];
  _timeline = [];
  _overrides = {};
  _pinnedTrack = null;

  // Disconnect previous job WebSocket and reset per-job UI
  if (_jobWs) { _jobWs.close(); _jobWs = null; }
  clearLog();
  _hideStatus();

  localStorage.setItem('lastJobId', id);
  document.querySelectorAll('.m-proj-item')
    .forEach(el => el.classList.toggle('active', el.dataset.id === id));

  const job = await api.get(`/api/jobs/${id}`);
  if (!job) return;
  _workDir = job.params?.work_dir || '';
  _loadTimeline(job);  // restore from backend params before loadMusicList runs
  const name = trimPath(job.params?.work_dir) || id;

  // Pattern input
  const cameras = job.params?.cameras || [];
  const patInput = document.getElementById('m-cam-pattern');
  const patRow = document.getElementById('m-pattern-row');
  if (patInput) patInput.value = job.params?.cam_pattern || '';
  if (patRow) patRow.style.display = cameras.length >= 2 ? '' : 'none';
  _updatePatternHint(cameras);
  document.getElementById('m-project-name').textContent = name;
  document.title = `Studio — ${name}`;

  // Reconnect progress if this job is still running
  if (job.status === 'running' || job.status === 'queued') {
    _connectJobProgress(id);
  }

  _resultsVideo = null;
  _resultsData  = {};
  const rBtn = document.getElementById('m-btn-results');
  if (rBtn) rBtn.style.display = 'none';
  await loadPool(id);
  drawTimeline();
  if (typeof loadMusicList === 'function') await loadMusicList(id);
  loadResults();
  _checkProxyStatus();
}
window.openProject = openProject;

// ── Pool ──────────────────────────────────────────────────────────────────────
async function loadPool(id) {
  const grid = document.getElementById('m-pool-grid');
  grid.innerHTML = '<div class="m-empty"><span class="m-spinner"></span></div>';

  const [data, ar] = await Promise.all([
    api.get(`/api/jobs/${id}/frames`),
    api.get(`/api/jobs/${id}/analyze-result`).catch(() => null),
  ]);
  _frames = (data?.frames ?? data ?? []).sort((a, b) => b.score - a.score);
  const gpsBadge = document.getElementById('m-gps-badge');
  if (gpsBadge) gpsBadge.style.display = ar?.gps_detected ? '' : 'none';
  _buildCamFilters();
  renderPool();
}

function _buildCamFilters() {
  const bar = document.getElementById('m-cam-filters');
  if (!bar) return;
  const cams = [...new Set(_frames.map(f => f.camera).filter(Boolean))];
  if (cams.length <= 1) { bar.style.display = 'none'; _activeCameras = new Set(cams); return; }
  _activeCameras = new Set(cams);
  bar.style.display = 'flex';
  bar.innerHTML = '';
  for (const cam of cams) {
    const lbl = document.createElement('label');
    lbl.style.cssText = 'display:flex;align-items:center;gap:3px;font-size:10px;color:var(--sub);cursor:pointer;white-space:nowrap';
    const cb = document.createElement('input');
    cb.type = 'checkbox'; cb.checked = true; cb.value = cam;
    cb.onchange = () => {
      if (cb.checked) _activeCameras.add(cam); else _activeCameras.delete(cam);
      renderPool();
    };
    lbl.append(cb, document.createTextNode(cam.split('/').pop()));
    bar.appendChild(lbl);
  }
}

function scoreClass(score) {
  if (score >= 0.85) return 'm-score-hi';
  if (score >= 0.70) return 'm-score-mid';
  if (score >= 0.50) return 'm-score-low';
  return '';
}

function toggleBan(scene) {
  if (_overrides[scene] === 'ban') {
    delete _overrides[scene];
    _removedScenes.delete(scene);
  } else {
    _overrides[scene] = 'ban';
    _removedScenes.delete(scene);
  }
  _saveTimeline(_jobId);
  renderPool();
}

function renderPool() {
  const grid  = document.getElementById('m-pool-grid');
  const count = document.getElementById('m-pool-count');
  if (!grid) return;
  if (_frames.length === 0) {
    grid.innerHTML = '<div class="m-empty">No scenes analyzed yet.</div>';
    if (count) count.textContent = '';
    return;
  }
  const banned     = new Set(Object.keys(_overrides).filter(s => _overrides[s] === 'ban'));
  const inTimeline = new Set(_timeline.map(c => c.scene));
  const camFiltered = _activeCameras.size
    ? _frames.filter(f => !f.camera || _activeCameras.has(f.camera))
    : _frames;
  const available  = camFiltered.filter(f => !banned.has(f.scene) && !inTimeline.has(f.scene)).length;
  if (count) count.textContent = `${available} / ${camFiltered.length}`;
  grid.innerHTML = '';
  camFiltered.forEach(f => {
    if (inTimeline.has(f.scene)) return;
    const isBanned  = banned.has(f.scene);
    const isRemoved = _removedScenes.has(f.scene);
    const div = document.createElement('div');
    div.className = `m-thumb ${scoreClass(f.score)}${isBanned ? ' banned' : ''}${isRemoved ? ' removed' : ''}`;
    div.dataset.scene = f.scene;
    div.draggable = true;
    const scoreLabel = f.score >= 0.85 ? 'High score (≥0.85) — green border'
                     : f.score >= 0.70 ? 'Good score (≥0.70) — light green border'
                     : f.score >= 0.50 ? 'Low score (≥0.50) — yellow border'
                     : 'Very low score (<0.50)';
    div.title = isBanned ? `Banned — click to unban\n${scoreLabel}`
              : isRemoved ? `Removed from timeline — click to ban\n${scoreLabel}`
              : `Click to ban\n${scoreLabel}`;
    div.innerHTML = `
      <div class="m-thumb-img">
        <img src="/api/file?path=${encodeURIComponent(f.frame_url)}" loading="lazy" draggable="false">
        <span class="m-thumb-score">${f.score.toFixed(3)}</span>
      </div>
      <div class="m-thumb-label"></div>`;
    div.querySelector('.m-thumb-label').textContent = f.scene;
    div.addEventListener('click', () => toggleBan(f.scene));
    div.addEventListener('dragstart', onPoolDragStart);
    div.addEventListener('mouseenter', () => _showInlinePreview(div, f.frame_url));
    div.addEventListener('mouseleave', () => _hideInlinePreview(div));
    grid.appendChild(div);
  });
}

// ── Timeline ──────────────────────────────────────────────────────────────────
async function rebuildTimeline() {
  if (!_jobId || !_pinnedTrack) { alert('Pin a music track first.'); return; }

  const overlay = document.getElementById('m-build-overlay');
  if (overlay) overlay.style.display = 'flex';

  const _md = (typeof _musicDir !== 'undefined') ? _musicDir : '';
  clearTimeout(_savePatternTimer);
  const _patInput = document.getElementById('m-cam-pattern');
  const _camPattern = _patInput ? _patInput.value.trim() : '';
  // Clear manual timeline so dry-run rebuilds fresh from pattern
  _timeline = [];
  await api.patch(`/api/jobs/${_jobId}/params`, {
    selected_track: _pinnedTrack,
    manual_timeline: null,
    manual_overrides: {},
    cam_pattern: _camPattern,
    ...(_md ? { music_dir: _md } : {}),
  });
  const data = await api.post(`/api/jobs/${_jobId}/preview-sequence`);

  if (overlay) overlay.style.display = 'none';

  if (!data?.sequence) {
    const meta = document.getElementById('m-timeline-meta');
    if (meta) meta.textContent = 'failed — check server log';
    return;
  }
  _timeline = data.sequence;
  _timelineMusic = data.music || null;
  _overrides = {};
  _removedScenes.clear();
  _saveTimeline(_jobId);
  drawTimeline();
  renderPool();
  enableActions(true);
}
window.rebuildTimeline = rebuildTimeline;

function _updatePatternHint(cameras) {
  const el = document.getElementById('m-pattern-hint');
  if (!el) return;
  if (!cameras || cameras.length < 2) { el.textContent = ''; return; }
  el.textContent = cameras.map((c, i) => `${'abcdefghijklmnopqrstuvwxyz'[i]}=${c}`).join('  ');
}

function saveCamPattern(val) {
  if (!_jobId) return;
  clearTimeout(_savePatternTimer);
  _savePatternTimer = setTimeout(() => {
    api.patch(`/api/jobs/${_jobId}/params`, { cam_pattern: val.trim() });
  }, 600);
}
window.saveCamPattern = saveCamPattern;

function _pinnedTrackObj() {
  if (!_pinnedTrack) return null;
  try { return _allTracks?.find(t => t.file === _pinnedTrack) || null; } catch { return null; }
}

function _timelinePxPerSec(refDurSec) {
  if (!refDurSec) return 18;
  // sidebar 220 + right panel 180 + track label 44 + gap 8 + padding 24 + safety 24 = 500
  const available = Math.max(200, window.innerWidth - 500);
  return Math.max(4, Math.floor(available / refDurSec));
}

// drawTimeline — builds clip DOM elements in the timeline track
function drawTimeline() {
  const clipTrack  = document.getElementById('m-clip-track');
  const musicBar   = document.getElementById('m-music-bar');
  const musicLabel = document.getElementById('m-music-label');
  const meta       = document.getElementById('m-timeline-meta');
  if (!clipTrack) return;

  const totalDur = _timeline.reduce((s, c) => s + c.duration, 0);
  const trackObj = _pinnedTrackObj();
  const musicDur = trackObj?.duration || 0;

  const refDur = Math.max(musicDur || 0, totalDur || 0) || 1;

  // Clear FIRST, then measure with getBoundingClientRect (forces reflow, sub-px accurate).
  clipTrack.innerHTML = '';
  const trackW = Math.floor(clipTrack.getBoundingClientRect().width);
  if (!trackW) { requestAnimationFrame(drawTimeline); return; }
  const ruler = document.getElementById('m-time-ruler');

  if (meta) {
    if (musicDur > 0)
      meta.textContent = `${_timeline.length} clips · ${fmtSec(totalDur)} / ${fmtSec(musicDur)}`;
    else
      meta.textContent = `${_timeline.length} clips · ${fmtSec(totalDur)}`;
  }

  // Music bar: px width relative to clip track content area (same reference as clip %).
  if (musicBar) {
    const musicW = musicDur > 0 ? Math.round(musicDur / refDur * trackW) : trackW;
    musicBar.style.width    = musicW + 'px';
    musicBar.style.minWidth = '';
    musicBar.style.flex     = 'none';
    const name = trackObj?.title || (_pinnedTrack?.split('/').pop() || '');
    if (musicLabel) musicLabel.textContent = name
      ? `${name}${musicDur > 0 ? ' · ' + fmtSec(musicDur) : ''}`
      : 'no track selected';
  }

  // Ruler ticks in px — same reference as music bar.
  if (ruler) {
    ruler.innerHTML = '';
    ruler.style.width = trackW + 'px';
    const step = refDur <= 30 ? 5 : refDur <= 120 ? 10 : refDur <= 300 ? 30 : 60;
    const majorEvery = step <= 10 ? 4 : 2;
    for (let s = 0; s <= refDur; s += step) {
      const x = Math.round(s / refDur * trackW);
      const isMajor = (s / step) % majorEvery === 0;
      const tick = document.createElement('div');
      tick.className = 'm-ruler-tick' + (isMajor ? ' major' : '');
      tick.style.left = x + 'px';
      ruler.appendChild(tick);
      if (isMajor) {
        const lbl = document.createElement('div');
        lbl.className = 'm-ruler-label';
        lbl.style.left = x + 'px';
        lbl.textContent = fmtSec(s);
        ruler.appendChild(lbl);
      }
    }
  }

  const makeInsert = insertIdx => {
    const z = document.createElement('div');
    z.className = 'm-clip-insert';
    z.addEventListener('dragover',  e => { e.preventDefault(); z.classList.add('active'); });
    z.addEventListener('dragleave', () => z.classList.remove('active'));
    z.addEventListener('drop', e => { e.preventDefault(); z.classList.remove('active'); handleInsert(insertIdx); });
    return z;
  };

  clipTrack.appendChild(makeInsert(0));
  _timeline.forEach((slot, idx) => {
    const w = Math.round(slot.duration / refDur * trackW) + 'px';

    const div = document.createElement('div');
    div.className = 'm-clip';
    div.style.flex  = '0 0 ' + w;
    div.style.width = w;
    div.dataset.idx = idx;
    div.draggable = true;

    if (slot.type === 'photo') {
      const imgSrc = slot.frame_url ? `/api/file?path=${encodeURIComponent(slot.frame_url)}` : null;
      div.dataset.scene = slot.scene || ('photo_' + idx);
      div.title = `photo · ${slot.duration.toFixed(1)}s`;
      div.style.outline = '2px solid #818cf8';
      div.innerHTML = `
        <div class="m-clip-thumb">${imgSrc ? `<img src="${imgSrc}" loading="lazy" draggable="false">` : '<span style="font-size:16px;display:flex;align-items:center;justify-content:center;height:100%">🖼</span>'}</div>
        <div class="m-clip-score-bar" style="background:#818cf8"></div>
        <div class="m-clip-ts">${fmtSec(slot.music_start ?? 0)}</div>`;
    } else {
      const scoreColor = slot.clip_score >= 0.85 ? '#22c55e'
                       : slot.clip_score >= 0.70 ? '#4ade80'
                       : slot.clip_score >= 0.50 ? '#facc15' : '#475569';
      const frameObj = _frames.find(f => f.scene === slot.scene);
      const frameUrl = frameObj?.frame_url || slot.frame_url || null;
      const imgSrc   = frameUrl ? `/api/file?path=${encodeURIComponent(frameUrl)}` : null;
      div.dataset.scene = slot.scene;
      div.title = `${slot.scene} · ${slot.duration.toFixed(1)}s · score ${slot.clip_score?.toFixed(3)}`;
      div.innerHTML = `
        <div class="m-clip-thumb">${imgSrc ? `<img src="${imgSrc}" loading="lazy" draggable="false">` : ''}</div>
        <div class="m-clip-score-bar" style="background:${scoreColor}"></div>
        <div class="m-clip-ts">${fmtSec(slot.music_start ?? 0)}</div>`;
      div.addEventListener('mouseenter', () => _showTlPreview(div, frameUrl));
      div.addEventListener('mouseleave', _hideTlPreview);
    }

    div.addEventListener('dragstart', onClipDragStart);
    div.addEventListener('dblclick',  () => removeClip(idx));
    clipTrack.appendChild(div);
    clipTrack.appendChild(makeInsert(idx + 1));
  });
}

// ── Timeline clip hover preview ───────────────────────────────────────────────
let _tlPvTimer = null;

function _showTlPreview(clipEl, frameUrl) {
  clearTimeout(_tlPvTimer);
  if (!frameUrl) return;
  _tlPvTimer = setTimeout(() => {
    if (_drag) return;
    const pv = document.getElementById('m-tl-preview');
    if (!pv) return;
    const clipPath = _clipPath(frameUrl);
    if (!clipPath) return;
    const src = clipPath.startsWith('/data/') ? clipPath : `/api/file?path=${encodeURIComponent(clipPath)}`;
    const v = pv.querySelector('video');
    if (v && v.src !== src) { v.src = src; v.load(); }
    if (v) v.play().catch(() => {});
    const rect = clipEl.getBoundingClientRect();
    const wrap  = document.getElementById('m-timeline-wrap');
    const wRect = wrap?.getBoundingClientRect();
    pv.style.left    = Math.max(4, Math.min(rect.left, window.innerWidth - 484)) + 'px';
    pv.style.top     = wRect ? (wRect.top - 278) + 'px' : (rect.top - 278) + 'px';
    pv.style.display = 'block';
  }, 1000);
}

function _hideTlPreview() {
  clearTimeout(_tlPvTimer);
  const pv = document.getElementById('m-tl-preview');
  if (!pv) return;
  pv.style.display = 'none';
  const v = pv.querySelector('video');
  if (v) { v.pause(); v.src = ''; v.load(); }
}

function removeClip(idx) {
  const scene = _timeline[idx]?.scene;
  if (scene) _overrides[scene] = 'ban';
  _timeline.splice(idx, 1);
  _saveTimeline(_jobId);
  drawTimeline();
  renderPool();
}

// ── Drag & Drop ───────────────────────────────────────────────────────────────
let _drag = null;   // { from: 'pool'|'timeline', scene, idx }

function onPoolDragStart(e) {
  _drag = { from: 'pool', scene: e.currentTarget.dataset.scene };
  e.dataTransfer.effectAllowed = 'copy';
  e.dataTransfer.setData('text/plain', _drag.scene);
  e.currentTarget.addEventListener('dragend', () => { _drag = null; }, { once: true });
}

function onClipDragStart(e) {
  const idx = parseInt(e.currentTarget.dataset.idx);
  _drag = { from: 'timeline', scene: _timeline[idx]?.scene, idx };
  e.dataTransfer.effectAllowed = 'move';
  e.dataTransfer.setData('text/plain', _drag.scene || '');
  e.stopPropagation();
  e.currentTarget.addEventListener('dragend', () => { _drag = null; }, { once: true });
}

// Insert at position: handles pool→timeline and timeline reorder
function handleInsert(insertIdx) {
  if (!_drag) return;
  if (_drag.from === 'pool') {
    const frame = _frames.find(f => f.scene === _drag.scene);
    if (!frame) return;
    if (_overrides[frame.scene] === 'ban') delete _overrides[frame.scene];

    // Inherit duration from the existing slot at that position (preserves music sync).
    // Fall back to scene duration only if inserting at end or timeline is empty.
    const existingSlot = _timeline[insertIdx];
    const slotDur = (existingSlot && existingSlot.duration < frame.duration)
      ? existingSlot.duration
      : frame.duration;

    // clip_ss: center on the best CLIP-scored frame (_f0=25%, _f1=50%, _f2=75%)
    const url = frame.frame_url || '';
    const pct = url.includes('_f0') ? 0.25 : url.includes('_f2') ? 0.75 : 0.50;
    const bestT = pct * frame.duration;
    const clip_ss = Math.max(0, Math.min(bestT - slotDur / 2, frame.duration - slotDur));

    _timeline.splice(insertIdx, 0, {
      scene: frame.scene, duration: slotDur,
      clip_score: frame.score, frame_url: frame.frame_url || null, energy: 0.5,
      clip_path: _workDir ? _workDir + '/_autoframe/autocut/' + frame.scene + '.mp4' : '',
      clip_ss,
    });
  } else {
    const from = _drag.idx;
    const [moved] = _timeline.splice(from, 1);
    _timeline.splice(insertIdx > from ? insertIdx - 1 : insertIdx, 0, moved);
  }
  _drag = null;
  _saveTimeline(_jobId);
  drawTimeline();
  renderPool();
}

function _setupPoolDropTarget() {
  const grid = document.getElementById('m-pool-grid');
  if (!grid) return;
  grid.addEventListener('dragover', e => {
    if (_drag?.from === 'timeline') { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; }
  });
  grid.addEventListener('drop', e => {
    e.preventDefault();
    if (_drag?.from !== 'timeline') return;
    const scene = _timeline[_drag.idx]?.scene;
    _timeline.splice(_drag.idx, 1);
    _drag = null;
    if (scene) _removedScenes.add(scene);
    _saveTimeline(_jobId);
    drawTimeline();
    renderPool();
  });
}

// ── Job progress WebSocket ────────────────────────────────────────────────────
let _jobWs = null;

function _showStatus(phase, text, pct, state) {
  const bar  = document.getElementById('m-job-status');
  const ph   = document.getElementById('m-job-phase');
  const fill = document.getElementById('m-job-bar-fill');
  const txt  = document.getElementById('m-job-text');
  const cancel = document.getElementById('m-btn-cancel');
  if (!bar) return;
  bar.style.display = '';
  if (ph)  ph.textContent  = phase;
  if (txt) txt.textContent = text;
  fill.className = state === 'done' ? 'm-job-bar-fill done'
                 : state === 'error' ? 'm-job-bar-fill error'
                 : pct === null ? 'm-job-bar-fill indeterminate' : '';
  if (pct !== null) fill.style.width = pct + '%';
  if (cancel) cancel.style.display = state === 'running' ? '' : 'none';
}

function _hideStatus() {
  const bar = document.getElementById('m-job-status');
  if (bar) bar.style.display = 'none';
}

function _fmtEta(sec) {
  if (sec < 5)  return '< 5s';
  if (sec < 60) return `~${Math.round(sec)}s`;
  const m = Math.floor(sec / 60), s = Math.round(sec % 60);
  return s > 0 ? `~${m}m ${s}s` : `~${m}m`;
}

function _connectJobProgress(jobId) {
  if (_jobWs) { _jobWs.close(); _jobWs = null; }
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws/${jobId}`);
  _jobWs = ws;
  let total = 0;
  let current = 0;
  let startTime = null;

  ws.onmessage = e => {
    let msg;
    try { msg = JSON.parse(e.data); } catch { return; }

    if (msg.type === 'status') {
      const st = msg.status;
      if (st === 'running') {
        _showStatus(msg.phase || 'rendering', '', null, 'running');
        _setRenderBusy(true);
      } else if (st === 'done') {
        _showStatus('done', '✓ complete', 100, 'done');
        _setRenderBusy(false);
        ws.close();
        setTimeout(_hideStatus, 4000);
        loadResults();
        if (_jobId) loadPool(_jobId);
      } else if (st === 'failed' || st === 'killed') {
        _showStatus('failed', '✗ ' + (st === 'killed' ? 'cancelled' : 'error'), 100, 'error');
        _setRenderBusy(false);
        ws.close();
      }
    } else if (msg.type === 'log') {
      _appendLog(msg.line);
      const m = msg.line.match(/\[\s*(\d+)\s*\/\s*(\d+)\s*\]/);
      if (m) {
        current = parseInt(m[1]);
        total   = parseInt(m[2]);
        if (!startTime) startTime = Date.now();
        const pct = total > 0 ? Math.round(current / total * 100) : null;
        let label = `${current} / ${total}`;
        if (current > 0 && total > current) {
          const elapsed = (Date.now() - startTime) / 1000;
          const eta = elapsed / current * (total - current);
          label += `  ETA ${_fmtEta(eta)}`;
        }
        _showStatus('rendering', label, pct, 'running');
      }
    } else if (msg.type === 'shorts_status') {
      if (msg.running) {
        _showStatus('shorts', 'Generating short…', null, 'running');
        _setShortsRenderBusy(true);
      } else {
        _setShortsRenderBusy(false);
        if (msg.status === 'done') {
          _showStatus('shorts', '✓ Short ready', 100, 'done');
          setTimeout(_hideStatus, 3000);
          loadResults();
        } else {
          _showStatus('shorts', '✗ Shorts failed', 100, 'error');
          setTimeout(_hideStatus, 4000);
        }
      }
    } else if (msg.type === 'shorts_batch_progress') {
      _showStatus('shorts', `Short ${msg.done}/${msg.total}`, msg.pct, 'running');
    }
  };
  ws.onclose = () => { if (_jobWs === ws) _jobWs = null; };
}
window._connectJobProgress = _connectJobProgress;

function _setRenderBusy(busy) {
  ['m-btn-render'].forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    el.disabled = busy;
    el.textContent = busy ? 'Rendering…' : '⬡ Render';
  });
}

function _setShortsRenderBusy(busy) {
  const el = document.getElementById('m-btn-shorts');
  if (!el) return;
  el.disabled = busy;
  el.textContent = busy ? 'Generating…' : '▶ Shorts';
}

// ── Log panel ─────────────────────────────────────────────────────────────────
let _logLines = [];
let _logOpen  = false;

function _appendLog(line) {
  _logLines.push(line);
  if (_logLines.length > 200) _logLines.shift();
  const panel = document.getElementById('m-log');
  if (panel) panel.style.display = '';
  const meta = document.getElementById('m-log-meta');
  if (meta) meta.textContent = `${_logLines.length} lines`;
  if (!_logOpen) return;
  const el = document.getElementById('m-log-lines');
  if (!el) return;
  const div = document.createElement('div');
  div.className = 'll' + (/error|fail|Error|Fail/i.test(line) ? ' err' : '');
  div.textContent = line;
  el.appendChild(div);
  el.scrollTop = el.scrollHeight;
}

function toggleLog() {
  _logOpen = !_logOpen;
  const el    = document.getElementById('m-log-lines');
  const label = document.getElementById('m-log-toggle');
  if (!el) return;
  if (_logOpen) {
    el.style.display = '';
    if (label) label.textContent = 'LOG ▾';
    el.innerHTML = '';
    _logLines.forEach(line => {
      const div = document.createElement('div');
      div.className = 'll' + (/error|fail|Error|Fail/i.test(line) ? ' err' : '');
      div.textContent = line;
      el.appendChild(div);
    });
    el.scrollTop = el.scrollHeight;
  } else {
    el.style.display = 'none';
    if (label) label.textContent = 'LOG ▸';
  }
}
window.toggleLog = toggleLog;

let _logMaximized = false;

function _collapseLogMax() {
  if (!_logMaximized) return;
  _logMaximized = false;
  const log  = document.getElementById('m-log');
  const pool = document.getElementById('m-pool');
  const btn  = document.getElementById('m-log-max-btn');
  if (log)  log.classList.remove('m-log-maximized');
  if (pool) pool.style.display = '';
  if (btn)  btn.textContent = '⤢';
}

function toggleLogMax() {
  _logMaximized = !_logMaximized;
  const log   = document.getElementById('m-log');
  const pool  = document.getElementById('m-pool');
  const btn   = document.getElementById('m-log-max-btn');
  if (log)  log.classList.toggle('m-log-maximized', _logMaximized);
  if (pool) pool.style.display = _logMaximized ? 'none' : '';
  if (btn)  btn.textContent = _logMaximized ? '⤡' : '⤢';
  if (_logMaximized && !_logOpen) toggleLog();
}
window.toggleLogMax = toggleLogMax;

function clearLog() {
  _collapseLogMax();
  _logLines = [];
  const el = document.getElementById('m-log-lines');
  if (el) el.innerHTML = '';
  const panel = document.getElementById('m-log');
  if (panel) panel.style.display = 'none';
  const meta = document.getElementById('m-log-meta');
  if (meta) meta.textContent = '';
}
window.clearLog = clearLog;

// ── Preview (NVENC stream) ────────────────────────────────────────────────────
async function previewTimeline() {
  if (!_jobId) return;
  const btn = document.getElementById('m-btn-preview');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Sequencing…'; }

  // Flush manual timeline to server so preview-sequence uses it instead of dry-run
  if (_timeline.length > 0) {
    await api.patch(`/api/jobs/${_jobId}/params`, {
      manual_timeline: _timeline,
      manual_overrides: _overrides,
    });
  }
  const data = await api.post(`/api/jobs/${_jobId}/preview-sequence`);
  if (btn) { btn.disabled = false; btn.textContent = '▶ Preview'; }
  if (!data?.sequence?.length) { alert('Preview failed — check server log.'); return; }
  if (!_timeline.length) _timeline = data.sequence;
  _timelineMusic = data.music || null;
  drawTimeline();

  const video = document.getElementById('m-preview-video');
  const modal = document.getElementById('m-preview-modal');
  if (video) {
    video.src = `/api/jobs/${_jobId}/preview-stream?t=${Date.now()}`;
    video.load();
    if (modal) modal.style.display = 'flex';
    const _onFirstData = () => {
      video.removeEventListener('progress', _onFirstData);
      setTimeout(() => video.play().catch(() => {}), 5000);
    };
    video.addEventListener('progress', _onFirstData);
  } else if (modal) {
    modal.style.display = 'flex';
  }
}
window.previewTimeline = previewTimeline;

function closePreviewModal() {
  const modal = document.getElementById('m-preview-modal');
  if (modal) modal.style.display = 'none';
  const video = document.getElementById('m-preview-video');
  if (video) { video.pause(); video.src = ''; }
}
window.closePreviewModal = closePreviewModal;

// ── Render ────────────────────────────────────────────────────────────────────
async function renderTimeline() {
  if (!_jobId || !_pinnedTrack) { alert('Pin a music track first.'); return; }
  _setRenderBusy(true);
  _showStatus('queuing…', '', null, 'running');

  const overridesPayload = {};
  Object.keys(_overrides).filter(s => _overrides[s] === 'ban')
    .forEach(s => { overridesPayload[s] = false; });

  _connectJobProgress(_jobId);

  const resp = await api.post(`/api/jobs/${_jobId}/render-music-driven`, {
    selected_track: _pinnedTrack,
    overrides: overridesPayload,
  });

  if (!resp) {
    _showStatus('error', '✗ failed to start', 100, 'error');
    _setRenderBusy(false);
  }
}
window.renderTimeline = renderTimeline;

async function cancelRender() {
  if (!_jobId) return;
  await fetch(`/api/jobs/${_jobId}`, { method: 'DELETE' });
}
window.cancelRender = cancelRender;

// ── Proxy generation ──────────────────────────────────────────────────────────
let _proxyPollTimer = null;

async function startProxy() {
  if (!_jobId) return;
  const btn    = document.getElementById('m-btn-proxy');
  const status = document.getElementById('m-proxy-status');
  if (btn) btn.disabled = true;
  if (status) { status.style.display = ''; status.textContent = 'Starting…'; }
  await fetch(`/api/jobs/${_jobId}/start-proxy`, { method: 'POST' });
  _pollProxyStatus();
}
window.startProxy = startProxy;

function _pollProxyStatus() {
  clearTimeout(_proxyPollTimer);
  if (!_jobId) return;
  const btn    = document.getElementById('m-btn-proxy');
  const status = document.getElementById('m-proxy-status');
  fetch(`/api/jobs/${_jobId}/proxy-status`)
    .then(r => r.ok ? r.json() : null)
    .then(st => {
      if (!st) return;
      if (st.done) {
        if (btn) btn.disabled = false;
        if (status) {
          status.textContent = st.error ? `✗ ${st.error}` : `✓ ${st.finished}/${st.total} done`;
          setTimeout(() => { status.style.display = 'none'; }, 5000);
        }
      } else {
        const pct = st.total > 0 ? `${st.finished}/${st.total}` : '…';
        const cur = st.current_file ? st.current_file.split('/').pop() : '';
        if (status) status.textContent = `${pct}${cur ? ' · ' + cur : ''}`;
        _proxyPollTimer = setTimeout(_pollProxyStatus, 2000);
      }
    })
    .catch(() => { _proxyPollTimer = setTimeout(_pollProxyStatus, 5000); });
}

function _checkProxyStatus() {
  if (!_jobId) return;
  const btn    = document.getElementById('m-btn-proxy');
  const status = document.getElementById('m-proxy-status');
  fetch(`/api/jobs/${_jobId}/proxy-status`)
    .then(r => r.ok ? r.json() : null)
    .then(st => {
      if (!st || st.not_started) { if (btn) btn.disabled = false; return; }
      if (!st.done) {
        if (status) status.style.display = '';
        _pollProxyStatus();
      } else {
        if (btn) btn.disabled = false;
        if (st.finished > 0 && status) {
          status.style.display = '';
          status.textContent = `✓ proxy ready (${st.finished} files)`;
        }
      }
    })
    .catch(() => {});
}
window._checkProxyStatus = _checkProxyStatus;


// ── Results modal ─────────────────────────────────────────────────────────────
let _resultsVideo = null;
let _resultsData  = {};       // cached {name: info}
let _resultsTab   = 'highlight'; // 'highlight' | 'shorts'

async function loadResults() {
  if (!_jobId) return;
  const data = await api.get(`/api/jobs/${_jobId}/result`);
  const entries = data ? Object.entries(data) : [];
  if (entries.length === 0) return;

  // fingerprint — skip rebuild if unchanged
  const fp = JSON.stringify(entries.map(([n, i]) => [n, i.size_mb]));
  if (_resultsData._fp === fp) return;
  _resultsData = data || {};
  _resultsData._fp = fp;

  // show Results button in topbar
  const btn = document.getElementById('m-btn-results');
  if (btn) btn.style.display = '';

  _renderResultsList();
}
window.loadResults = loadResults;

function _renderResultsList() {
  const list = document.getElementById('m-results-list');
  const meta = document.getElementById('m-results-meta');
  if (!list) return;

  const entries = Object.entries(_resultsData).filter(([k]) => k !== '_fp');
  const highlights = entries.filter(([n]) => !/short/i.test(n));
  const shorts     = entries.filter(([n]) => /short/i.test(n));

  const active = _resultsTab === 'shorts' ? shorts : highlights;
  const total  = entries.length;
  const hasMainYt = highlights.some(([, i]) => i.yt_url);
  if (meta) meta.textContent = `${total} file${total !== 1 ? 's' : ''}`;

  list.innerHTML = '';

  // tabs
  const tabs = document.createElement('div');
  tabs.className = 'm-results-tabs';
  ['highlight', 'shorts'].forEach(tab => {
    const count = tab === 'shorts' ? shorts.length : highlights.length;
    const t = document.createElement('button');
    t.className = 'm-results-tab' + (tab === _resultsTab ? ' active' : '');
    t.textContent = (tab === 'highlight' ? 'Highlight' : 'Shorts') + (count ? ` (${count})` : '');
    t.onclick = () => { _resultsTab = tab; _renderResultsList(); };
    tabs.appendChild(t);
  });
  list.appendChild(tabs);

  if (active.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'm-empty';
    empty.textContent = 'No files.';
    list.appendChild(empty);
    return;
  }

  active.forEach(([name, info]) => {
    const playUrl = info.preview_url || info.url;
    const row = document.createElement('div');
    row.className = 'm-rf';
    row.title = name;
    row.onclick = () => playResult(row, playUrl, name, info);

    const rfName = document.createElement('div');
    rfName.className = 'm-rf-name';
    rfName.textContent = name;

    const rfSize = document.createElement('span');
    rfSize.className = 'm-rf-size';
    rfSize.textContent = info.size_mb + ' MB';

    const rfDur = document.createElement('span');
    rfDur.className = 'm-rf-dur';
    rfDur.textContent = info.duration_sec ? fmtSec(info.duration_sec) : '';

    const rfMeta = document.createElement('div');
    rfMeta.className = 'm-rf-meta';
    rfMeta.appendChild(rfSize);
    rfMeta.appendChild(rfDur);

    const dlLink = document.createElement('a');
    dlLink.className = 'm-rf-dl';
    dlLink.textContent = '▼';
    dlLink.title = 'Download';
    dlLink.href = '/api/file?path=' + encodeURIComponent(info.url) + '&dl=1';
    dlLink.download = name;
    dlLink.onclick = e => e.stopPropagation();

    const isShort   = /short/i.test(name);
    const filePath  = info.url;
    const rfActions = document.createElement('div');
    rfActions.className = 'm-rf-actions';

    if (!isShort) {
      const ytBtn = document.createElement('button');
      ytBtn.className = 'm-rf-yt' + (info.yt_url ? ' linked' : '');
      ytBtn.textContent = info.yt_url ? '✓ YT' : '▲ YT';
      ytBtn.title = info.yt_url ? 'Uploaded: ' + info.yt_url : 'Upload to YouTube';
      ytBtn.onclick = e => { e.stopPropagation(); mYtOpen(filePath, name, info.yt_url || '', _jobId, _workDir); };
      rfActions.appendChild(ytBtn);
    } else {
      const ytsBtn = document.createElement('button');
      ytsBtn.className = 'm-rf-yt' + (info.yt_url ? ' linked' : '');
      ytsBtn.textContent = info.yt_url ? '✓ YT' : '▲ YT';
      if (!hasMainYt && !info.yt_url) {
        ytsBtn.disabled = true;
        ytsBtn.title = 'Upload main video to YouTube first';
      } else {
        ytsBtn.title = info.yt_url ? 'Uploaded: ' + info.yt_url : 'Upload Short to YouTube';
      }
      ytsBtn.onclick = e => { e.stopPropagation(); mYtsOpen(filePath, name, _jobId, _workDir); };
      rfActions.appendChild(ytsBtn);

      const igBtn = document.createElement('button');
      igBtn.className = 'm-rf-ig' + (info.ig_url ? ' linked' : '');
      igBtn.textContent = info.ig_url ? '✓ IG' : '▲ IG';
      igBtn.title = info.ig_url ? 'Uploaded to Instagram' : 'Upload Reel to Instagram';
      igBtn.onclick = e => { e.stopPropagation(); mIgOpen(filePath, name, info.ncs_attr || null, _jobId); };
      rfActions.appendChild(igBtn);
    }

    const delBtn = document.createElement('button');
    delBtn.className = 'm-rf-del';
    delBtn.textContent = '✕';
    delBtn.title = 'Delete file';
    delBtn.onclick = async e => {
      e.stopPropagation();
      if (!confirm(`Delete ${name} from disk?`)) return;
      const ok = await api.del(`/api/jobs/${_jobId}/result-file?filename=${encodeURIComponent(name)}`);
      if (ok) { row.remove(); } else { alert('Delete failed'); }
    };

    row.appendChild(rfName);
    if (info.music) {
      const rfMusic = document.createElement('div');
      rfMusic.className = 'm-rf-music';
      rfMusic.textContent = '♪ ' + info.music.replace(/\.[^.]+$/, '');
      rfMusic.title = info.music;
      row.appendChild(rfMusic);
    }
    row.appendChild(rfMeta);
    row.appendChild(dlLink);
    row.appendChild(delBtn);
    row.appendChild(rfActions);
    list.appendChild(row);
  });
}

function playResult(row, url, name, info) {
  const video = document.getElementById('m-results-video');
  const info_el = document.getElementById('m-results-playing-info');
  if (!video) return;
  document.querySelectorAll('.m-rf.playing').forEach(r => r.classList.remove('playing'));
  row.classList.add('playing');
  video.src = url.startsWith('/data/') ? url : '/api/file?path=' + encodeURIComponent(url);
  video.play().catch(() => {});
  _resultsVideo = video;
  if (info_el) {
    const parts = [name];
    if (info?.duration_sec) parts.push(fmtSec(info.duration_sec));
    if (info?.size_mb) parts.push(info.size_mb + ' MB');
    if (info?.music) parts.push('♪ ' + info.music.replace(/\.[^.]+$/, ''));
    info_el.textContent = parts.join('  ·  ');
  }
}

function openResultsModal() {
  _renderResultsList();
  document.getElementById('m-results-modal').style.display = 'flex';
}
window.openResultsModal = openResultsModal;

function closeResultsModal() {
  document.getElementById('m-results-modal').style.display = 'none';
  const video = document.getElementById('m-results-video');
  if (video) { video.pause(); video.src = ''; }
}
window.closeResultsModal = closeResultsModal;

// ── Helpers ───────────────────────────────────────────────────────────────────
function fmtSec(s) {
  const m = Math.floor(s / 60);
  return `${m}:${Math.floor(s % 60).toString().padStart(2, '0')}`;
}

function enableActions(on) {
  ['m-btn-rebuild', 'm-btn-preview', 'm-btn-render', 'm-btn-shorts', 'm-btn-proxy'].forEach(id => {
    const el = document.getElementById(id); if (el) el.disabled = !on; });
}

// ── Inline clip preview ───────────────────────────────────────────────────────
let _activePreviewThumb = null;

function _clipPath(frameUrl) {
  if (!frameUrl) return null;
  return frameUrl
    .replace(/\/frames\//, '/autocut/')
    .replace(/_f[012]\.jpg$/, '.mp4')
    .replace(/\.jpg$/, '.mp4');
}

function _showInlinePreview(thumb, frameUrl, delay = 500) {
  clearTimeout(thumb._pvTimer);
  thumb._pvTimer = setTimeout(() => {
    if (_drag) return;
    if (thumb.querySelector('video')) return;
    if (_activePreviewThumb && _activePreviewThumb !== thumb) _hideInlinePreview(_activePreviewThumb);
    _activePreviewThumb = thumb;
    const clip = _clipPath(frameUrl);
    if (!clip) return;
    const src = clip.startsWith('/data/') ? clip : `/api/file?path=${encodeURIComponent(clip)}`;
    const img = thumb.querySelector('img');
    const v = document.createElement('video');
    v.src = src;
    v.muted = true;
    v.loop = true;
    v.playsInline = true;
    v.preload = 'auto';
    v.style.cssText = 'pointer-events:none';
    if (img) { img.style.display = 'none'; img.insertAdjacentElement('afterend', v); }
    else thumb.querySelector('.m-thumb-img').prepend(v);
    v.load();
    v.play().catch(() => {});
  }, delay);
}

function _hideInlinePreview(thumb) {
  clearTimeout(thumb._pvTimer);
  const v = thumb.querySelector('video');
  if (!v) return;
  v.pause(); v.src = ''; v.load(); v.remove();
  if (_activePreviewThumb === thumb) _activePreviewThumb = null;
  const img = thumb.querySelector('img');
  if (img) img.style.display = '';
}

// ── HW monitor ────────────────────────────────────────────────────────────────
let _statsWs = null;

function _updateStats(s) {
  document.getElementById('m-hw-cpu-label').textContent = `CPU ${s.cpu_pct}%`;
  document.getElementById('m-hw-ram-label').textContent = `RAM ${s.ram_used_gb}/${s.ram_total_gb}G`;
  document.getElementById('m-bar-cpu').style.width = Math.min(s.cpu_pct, 100) + '%';
  document.getElementById('m-bar-ram').style.width = Math.min(s.ram_pct, 100) + '%';
  const gpuCol  = document.getElementById('m-hw-gpu-col');
  const vramCol = document.getElementById('m-hw-vram-col');
  if (s.gpu) {
    const vused = (s.gpu.vram_used_mb / 1024).toFixed(1);
    const vtot  = (s.gpu.vram_total_mb / 1024).toFixed(1);
    document.getElementById('m-hw-gpu-label').textContent  = `GPU ${s.gpu.pct}%`;
    document.getElementById('m-hw-vram-label').textContent = `VRAM ${vused}/${vtot}G`;
    document.getElementById('m-bar-gpu').style.width  = Math.min(s.gpu.pct, 100) + '%';
    document.getElementById('m-bar-vram').style.width = Math.min(s.gpu.vram_pct, 100) + '%';
    gpuCol.style.display  = '';
    vramCol.style.display = '';
  } else {
    gpuCol.style.display  = 'none';
    vramCol.style.display = 'none';
  }
  document.getElementById('m-hw-monitor').style.display = '';
}

function _connectStats() {
  if (_statsWs) return;
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  _statsWs = new WebSocket(`${proto}://${location.host}/ws/stats`);
  _statsWs.onmessage = e => _updateStats(JSON.parse(e.data));
  _statsWs.onclose = () => { _statsWs = null; setTimeout(_connectStats, 3000); };
}

// ── Tile size ─────────────────────────────────────────────────────────────────
function setTileSize(px) {
  px = Math.max(120, Math.min(480, parseInt(px) || 120));
  document.documentElement.style.setProperty('--tile-min', px + 'px');
  localStorage.setItem('tileSize', px);
  const sl = document.getElementById('m-tile-slider');
  if (sl) sl.value = px;
}
window.setTileSize = setTileSize;

// ── Navigation helpers ────────────────────────────────────────────────────────
function goToLegacy(hint) {
  location.href = 'index.html' + location.search;
}
window.goToLegacy = goToLegacy;

// ── Theme ─────────────────────────────────────────────────────────────────────
function modernToggleTheme() {
  const next = document.body.dataset.theme === 'light' ? 'dark' : 'light';
  document.body.dataset.theme = next;
  localStorage.setItem('theme', next);
  const btn = document.getElementById('m-theme-btn');
  if (btn) btn.textContent = next === 'dark' ? '☽' : '☀';
}
window.modernToggleTheme = modernToggleTheme;

// ── Boot ──────────────────────────────────────────────────────────────────────
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  const visible = id => { const el = document.getElementById(id); return el && el.style.display !== 'none'; };
  if (visible('m-preview-modal'))     { closePreviewModal();     return; }
  if (visible('m-appsettings-modal')) { closeAppSettingsModal(); return; }
  if (visible('m-project-modal'))     { closeProjectModal();     return; }
  if (visible('m-shorts-modal'))      { closeShortsModal();      return; }
  if (visible('m-music-modal'))       { closeMusicModal();       return; }
  if (visible('m-results-modal'))     { closeResultsModal();     return; }
});

document.addEventListener('DOMContentLoaded', async () => {
  const t = localStorage.getItem('theme') || 'dark';
  document.body.dataset.theme = t;
  const btn = document.getElementById('m-theme-btn');
  if (btn) btn.textContent = t === 'dark' ? '☽' : '☀';
  if (typeof applyI18nModern === 'function') applyI18nModern();
  const cfg = await api.get('/api/config');
  if (cfg?.browse_root) _browseRoot = cfg.browse_root;
  const savedTile = parseInt(localStorage.getItem('tileSize')) || 120;
  setTileSize(savedTile);
  _connectStats();
  _setupPoolDropTarget();
  await refreshProjectList();
  const lastId = localStorage.getItem('lastJobId');
  if (lastId) openProject(lastId);
  setInterval(refreshProjectList, 5000);
  let _resizeTimer;
  window.addEventListener('resize', () => { clearTimeout(_resizeTimer); _resizeTimer = setTimeout(drawTimeline, 100); });
});
