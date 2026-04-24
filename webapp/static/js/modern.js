// modern.js — AI-autoedit Studio NLE

// ── State ────────────────────────────────────────────────────────────────────
let _jobId       = null;
let _frames      = [];     // [{scene, score, duration, path}]
let _timeline    = [];     // [{scene, duration, clip_score, frame_url, energy}]
let _overrides   = {};     // {scene: 'ban'}
let _pinnedTrack = null;   // music file path (set by modern_music.js)

// ── API ───────────────────────────────────────────────────────────────────────
const api = {
  async get(url) {
    try { const r = await fetch(url); return r.ok ? r.json() : null; } catch { return null; }
  },
  async post(url, body = {}) {
    try {
      const r = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      return r.ok ? r.json() : null;
    } catch { return null; }
  },
};
window._modernApi = api;

// ── Project list ──────────────────────────────────────────────────────────────
async function refreshProjectList() {
  const data = await api.get('/api/jobs') || [];
  const sorted = [...data].sort((a, b) => (b.work_dir || '').localeCompare(a.work_dir || ''));
  const list = document.getElementById('m-project-list');
  if (!list) return;
  list.innerHTML = sorted.map(j => {
    const name = (j.work_dir || '').split('/').pop() || j.id;
    return `<div class="m-proj-item${j.id === _jobId ? ' active' : ''}"
                 data-id="${j.id}" onclick="openProject('${j.id}')">${name}</div>`;
  }).join('');
}

async function openProject(id) {
  if (_jobId === id) return;
  _jobId = id;
  _frames = [];
  _timeline = [];
  _overrides = {};
  _pinnedTrack = null;

  document.querySelectorAll('.m-proj-item')
    .forEach(el => el.classList.toggle('active', el.dataset.id === id));

  const job = await api.get(`/api/jobs/${id}`);
  if (!job) return;
  const name = (job.work_dir || '').split('/').pop() || id;
  document.getElementById('m-project-name').textContent = name;
  document.title = `Studio — ${name}`;

  await loadPool(id);
  if (typeof loadMusicList === 'function') await loadMusicList(id);
}
window.openProject = openProject;

// ── Pool ──────────────────────────────────────────────────────────────────────
async function loadPool(id) {
  const grid = document.getElementById('m-pool-grid');
  grid.innerHTML = '<div class="m-empty"><span class="m-spinner"></span></div>';

  const data = await api.get(`/api/jobs/${id}/frames`);
  _frames = (data?.frames ?? data ?? []).sort((a, b) => b.score - a.score);
  renderPool();
}

function scoreClass(score) {
  if (score >= 0.85) return 'm-score-hi';
  if (score >= 0.70) return 'm-score-mid';
  if (score >= 0.50) return 'm-score-low';
  return '';
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
  const banned = new Set(Object.keys(_overrides).filter(s => _overrides[s] === 'ban'));
  if (count) count.textContent = `${_frames.length - banned.size} / ${_frames.length}`;
  grid.innerHTML = '';
  _frames.forEach(f => {
    const div = document.createElement('div');
    div.className = `m-thumb ${scoreClass(f.score)}${banned.has(f.scene) ? ' banned' : ''}`;
    div.dataset.scene = f.scene;
    div.draggable = true;
    div.innerHTML = `
      <div class="m-thumb-img">
        <img src="/api/file?path=${encodeURIComponent(f.path)}" loading="lazy" draggable="false">
        <span class="m-thumb-score">${f.score.toFixed(3)}</span>
      </div>
      <div class="m-thumb-label"></div>`;
    div.querySelector('.m-thumb-label').textContent = f.scene;
    div.addEventListener('dragstart', onPoolDragStart);
    grid.appendChild(div);
  });
}

// ── Timeline ──────────────────────────────────────────────────────────────────
async function rebuildTimeline() {
  if (!_jobId || !_pinnedTrack) { alert('Pin a music track first.'); return; }
  const meta = document.getElementById('m-timeline-meta');
  if (meta) meta.textContent = 'building…';

  await api.post(`/api/jobs/${_jobId}/params`, { selected_track: _pinnedTrack });
  const data = await api.post(`/api/jobs/${_jobId}/preview-sequence`);

  if (!data?.sequence) {
    if (meta) meta.textContent = 'failed — check server log';
    return;
  }
  _timeline = data.sequence;
  drawTimeline();
  enableActions(true);
}
window.rebuildTimeline = rebuildTimeline;

const PX_PER_SEC = 18;

// drawTimeline — builds clip DOM elements in the timeline track
function drawTimeline() {
  const clipTrack = document.getElementById('m-clip-track');
  const meta      = document.getElementById('m-timeline-meta');
  if (!clipTrack) return;

  const totalDur = _timeline.reduce((s, c) => s + c.duration, 0);
  if (meta) meta.textContent = `${_timeline.length} clips · ${fmtSec(totalDur)}`;

  clipTrack.innerHTML = '';
  _timeline.forEach((slot, idx) => {
    const w = Math.max(32, Math.round(slot.duration * PX_PER_SEC));
    const scoreColor = slot.clip_score >= 0.85 ? '#22c55e'
                     : slot.clip_score >= 0.70 ? '#4ade80'
                     : slot.clip_score >= 0.50 ? '#facc15' : '#475569';
    const div = document.createElement('div');
    div.className = 'm-clip';
    div.style.width = w + 'px';
    div.dataset.idx = idx;
    div.dataset.scene = slot.scene;
    div.draggable = true;
    div.title = `${slot.scene} · ${slot.duration.toFixed(1)}s · score ${slot.clip_score?.toFixed(3)}`;
    div.innerHTML = `
      <span class="m-clip-name"></span>
      <div class="m-clip-score-bar" style="background:${scoreColor}"></div>`;
    div.querySelector('.m-clip-name').textContent = slot.scene;
    div.addEventListener('dragstart', onClipDragStart);
    div.addEventListener('dragover',  e => { e.preventDefault(); div.classList.add('drag-over'); });
    div.addEventListener('dragleave', () => div.classList.remove('drag-over'));
    div.addEventListener('drop',      e => { e.preventDefault(); e.stopPropagation(); div.classList.remove('drag-over'); handleDrop(idx); });
    div.addEventListener('dblclick',  () => removeClip(idx));
    clipTrack.appendChild(div);
  });

  // Drop zone at end of timeline
  const end = document.createElement('div');
  end.className = 'm-clip-drop-end';
  end.innerHTML = '<span style="color:var(--muted);font-size:9px">+</span>';
  end.addEventListener('dragover',  e => { e.preventDefault(); end.classList.add('drag-over'); });
  end.addEventListener('dragleave', () => end.classList.remove('drag-over'));
  end.addEventListener('drop',      e => { e.preventDefault(); end.classList.remove('drag-over'); handleDrop(_timeline.length); });
  clipTrack.appendChild(end);
}

function removeClip(idx) {
  const scene = _timeline[idx]?.scene;
  if (scene) _overrides[scene] = 'ban';
  _timeline.splice(idx, 1);
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

function handleDrop(targetIdx) {
  if (!_drag) return;
  if (_drag.from === 'pool') {
    const frame = _frames.find(f => f.scene === _drag.scene);
    if (!frame) return;
    _timeline.splice(targetIdx, 0, {
      scene: frame.scene, duration: frame.duration,
      clip_score: frame.score, frame_url: null, energy: 0.5,
    });
    if (_overrides[frame.scene] === 'ban') delete _overrides[frame.scene];
  } else {
    const from = _drag.idx;
    if (from === targetIdx) return;
    const [moved] = _timeline.splice(from, 1);
    _timeline.splice(targetIdx > from ? targetIdx - 1 : targetIdx, 0, moved);
  }
  _drag = null;
  drawTimeline();
  renderPool();
}

// ── Render ────────────────────────────────────────────────────────────────────
// renderTimeline — async action: POSTs to render-music-driven (called by Render buttons)
async function renderTimeline() {
  if (!_jobId || !_pinnedTrack) { alert('Pin a music track first.'); return; }
  const btn = document.getElementById('m-render-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Rendering…'; }

  const overridesPayload = {};
  Object.keys(_overrides).filter(s => _overrides[s] === 'ban')
    .forEach(s => { overridesPayload[s] = false; });

  await api.post(`/api/jobs/${_jobId}/render-music-driven`, {
    selected_track: _pinnedTrack,
    overrides: overridesPayload,
  });

  if (btn) { btn.disabled = false; btn.textContent = '⬡ Render'; }
  alert('Render started — check results in Legacy UI (Results tab).');
}
window.renderTimeline = renderTimeline;

window.previewTimeline = () => alert('Preview not available in v1 — use Legacy UI Preview tab.');

// ── Helpers ───────────────────────────────────────────────────────────────────
function fmtSec(s) {
  const m = Math.floor(s / 60);
  return `${m}:${Math.floor(s % 60).toString().padStart(2, '0')}`;
}

function enableActions(on) {
  ['m-btn-preview', 'm-btn-render', 'm-render-btn', 'm-btn-rebuild']
    .forEach(id => { const el = document.getElementById(id); if (el) el.disabled = !on; });
}

// ── Boot ──────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  refreshProjectList();
  setInterval(refreshProjectList, 5000);
});
