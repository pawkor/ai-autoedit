# Modern UI — NLE Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Revert Gemini's legacy UI pollution, clean dual-mode clutter, then rewrite Modern UI as an isolated NLE (pool + timeline) driven by the existing music-driven pipeline.

**Architecture:** Two independent phases — Phase 1 cleans legacy files surgically (revert bad Gemini additions, keep useful null guards); Phase 2 builds `modern.html` as a completely isolated page that shares no JS with legacy, uses existing `/api/jobs/{id}/preview-sequence` endpoint for music-driven timeline draft, and native HTML5 DnD for editing.

**Tech Stack:** Vanilla JS (ES2020), CSS custom properties, native HTML5 Drag & Drop, existing FastAPI backend (no new endpoints — `/preview-sequence` already exists in `jobs.py:981`).

---

## Phase 1: Legacy Cleanup

### Task 1: Revert ui.js Gemini additions

**Files:**
- Modify: `webapp/static/js/ui.js`

- [ ] **Step 1: Remove global `jobs` declaration, restore local scope**

Find the line Gemini added immediately before `async function refreshJobList()` and delete it:
```js
// DELETE this line:
let jobs = [];
```

Inside `refreshJobList`, restore `let` keyword:
```js
async function refreshJobList() {
  if (_refreshing) return;
  _refreshing = true;
  try {
    let jobs = await api.get('/api/jobs') || [];   // ← restore 'let'
```

- [ ] **Step 2: Remove isModern branching from refreshJobList**

Find and delete:
```js
// DELETE:
const isModern = !!document.getElementById('studio-app');
```

Restore the simple onclick:
```js
    list.querySelectorAll('.job-item').forEach((div, i) => {
      div.onclick = () => openJob(jobs[i].id);
    });
```
(Remove the `if (isModern && window.openModernJob) ... else if ...` branching.)

- [ ] **Step 3: Replace setUiMode with simplified version**

Gemini's version calls `api.put('/api/settings', ...)` which is unnecessary. Replace the entire `setUiMode` function with:
```js
function setUiMode(mode) {
  localStorage.setItem('uiMode', mode);
  if (mode === 'modern') window.location.href = 'modern.html' + window.location.search;
}
```

- [ ] **Step 4: Remove ui_mode from _readSettingsData**

In `_readSettingsData()`, delete this line:
```js
// DELETE:
ui_mode: iv('s-ui-mode') || 'legacy',
```

- [ ] **Step 5: Restore _saveSettingsData to simple form**

Replace the complex version (with oldMode comparison and location.reload) with:
```js
async function _saveSettingsData() { await api.put('/api/settings', _readSettingsData()); }
```

- [ ] **Step 6: Remove gallery tab label block from _applyTraditionalMode**

At the end of `_applyTraditionalMode()`, delete the block Gemini added:
```js
// DELETE this entire block:
// Update gallery tab label
// const galleryTab = document.querySelector('.tab[data-i18n="tab.gallery"]');
// if (galleryTab) { ... }
```

- [ ] **Step 7: Remove s-ui-mode sync from settings fetch block**

In the `api.get('/api/settings').then(s => { ... })` block near the bottom, delete:
```js
// DELETE:
// if (s.ui_mode) {
//   localStorage.setItem('uiMode', s.ui_mode);
//   const sel = document.getElementById('s-ui-mode');
//   if (sel) sel.value = s.ui_mode;
// }
```

- [ ] **Step 8: Verify null guards remain intact**

Confirm these two lines from Gemini ARE still present (useful bug fixes — keep them):
```js
// In setLogFilter() — KEEP:
if (!panel) return;

// Near end of setLogFilter() — KEEP:
const menu = document.getElementById('log-filter-menu');
if (menu) menu.classList.remove('open');
```

- [ ] **Step 9: Manual smoke test**

Open `https://ai-autoedit.sad-panda.eu/` in browser. Verify:
- No JS errors in console
- Project list populates in sidebar
- Clicking a project opens it (Settings, Select, etc. tabs work)
- Settings → Interface Mode → "Modern (Studio)" → navigates to `modern.html`

---

### Task 2: Clean index.html

**Files:**
- Modify: `webapp/static/index.html`

- [ ] **Step 1: Remove mode_switcher.js script tag**

Delete this line (it causes redirect loops if modern.html breaks):
```html
<script src="/js/mode_switcher.js"></script>
```

- [ ] **Step 2: Remove candidate-pool-hint div**

Delete the entire div:
```html
<div id="candidate-pool-hint" style="margin:8px 0 0;...">
  <span data-i18n="misc.candidate_pool_hint">...</span>
  <a href="#" onclick="switchTab('preview')...">View planned order →</a>
</div>
```

- [ ] **Step 3: Remove Privacy section**

Delete the entire `<details class="insp-section" data-cat="privacy" ...>` block — from its opening tag to its closing `</details>` tag. This includes blur speedometer, blur plates, and consensus slider.

- [ ] **Step 4: Verify Interface Mode select is still present**

Confirm `<select id="s-ui-mode" ...>` with options "Legacy (Current)" / "Modern (Studio)" is still in the Settings panel. Do NOT remove it.

- [ ] **Step 5: Manual test**

Open legacy UI. Verify:
- No redirect on page load
- Privacy section is gone from Settings
- Interface Mode dropdown still visible in Settings
- Legacy UI fully functional

---

### Task 3: Remove isModern branching from gallery JS files

**Files:**
- Modify: `webapp/static/js/gallery/select.js`
- Modify: `webapp/static/js/gallery/jobs.js`
- Modify: `webapp/static/js/services/forms.js`

- [ ] **Step 1: Inspect diffs**

```bash
git diff HEAD -- webapp/static/js/gallery/select.js webapp/static/js/gallery/jobs.js webapp/static/js/services/forms.js
```

Read the output to identify exact lines Gemini added.

- [ ] **Step 2: Remove all isModern / openModernJob / studio-app references from each file**

In each file, delete any block that checks `isModern`, `window.openModernJob`, or `document.getElementById('studio-app')`. The gallery and forms logic must not branch on UI mode.

- [ ] **Step 3: Manual test**

Open a project in legacy UI → Select Scenes tab → verify gallery renders, include/ban clicking works, job settings populate.

---

### Task 4: Legacy dual-mode cleanup

**Files:**
- Modify: `webapp/static/index.html`

- [ ] **Step 1: Add Traditional mode separator in Advanced modal**

In `index.html`, find the Advanced scene detection modal. Locate the `threshold-bar` div (already hidden by default). Immediately before it, insert:

```html
<div style="margin:16px 0 8px;padding-top:12px;border-top:1px solid var(--border);font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em">Traditional mode</div>
```

- [ ] **Step 2: Manual test**

In legacy UI: Settings tab → click "⚙ Advanced" → verify "Traditional mode" label appears above threshold slider. Music-driven button still shows as primary green button.

---

### Task 5: Commit Phase 1

- [ ] **Commit**

```bash
git add webapp/static/js/ui.js \
         webapp/static/index.html \
         webapp/static/js/gallery/select.js \
         webapp/static/js/gallery/jobs.js \
         webapp/static/js/services/forms.js
git commit -m "fix: revert Gemini legacy UI pollution, clean dual-mode clutter"
```

---

## Phase 2: Modern UI v1

> **Key discovery:** `/api/jobs/{id}/preview-sequence` already exists (`jobs.py:981`) and runs `music_driven.py --dry-run`. Returns `{sequence: [{scene, duration, frame_url, energy, camera, clip_score, music_start, n_beats}]}`. No new backend endpoint needed.

### Task 6: modern.html — NLE skeleton

**Files:**
- Modify: `webapp/static/modern.html` (full rewrite)

- [ ] **Step 1: Replace entire modern.html**

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AI-autoedit Studio</title>
  <link rel="stylesheet" href="/css/modern.css">
</head>
<body data-theme="dark">

  <div id="studio">

    <aside id="m-sidebar">
      <div class="m-sidebar-header">Projects</div>
      <div id="m-project-list"></div>
    </aside>

    <div id="m-main">

      <header id="m-topbar">
        <span id="m-project-name" class="m-project-name">No project selected</span>
        <div class="m-topbar-actions">
          <button id="m-btn-preview" class="m-btn m-btn-blue" onclick="previewTimeline()" disabled>▶ Preview</button>
          <button id="m-btn-render"  class="m-btn m-btn-green" onclick="renderTimeline()" disabled>⬡ Render</button>
          <button class="m-btn m-btn-ghost" onclick="location.href='index.html'+location.search">← Legacy</button>
        </div>
      </header>

      <section id="m-pool">
        <div class="m-section-header">
          <span class="m-section-title">POOL</span>
          <span id="m-pool-count" class="m-section-meta"></span>
          <span class="m-section-meta" style="margin-left:auto">drag ↓ to timeline</span>
        </div>
        <div id="m-pool-grid"></div>
      </section>

      <section id="m-timeline-wrap">
        <div class="m-section-header">
          <span class="m-section-title">TIMELINE</span>
          <span id="m-timeline-meta" class="m-section-meta"></span>
          <button id="m-btn-rebuild" class="m-btn m-btn-ghost m-btn-sm" onclick="rebuildTimeline()" disabled style="margin-left:auto">↺ rebuild</button>
        </div>
        <div id="m-timeline">
          <div class="m-track-row">
            <div class="m-track-label">clips</div>
            <div id="m-clip-track" class="m-track-clips"></div>
          </div>
          <div class="m-track-row">
            <div class="m-track-label">music</div>
            <div id="m-music-bar" class="m-track-music">
              <span id="m-music-label" class="m-music-label">no track selected</span>
            </div>
          </div>
        </div>
      </section>

    </div>

    <aside id="m-panel">
      <div class="m-panel-section">
        <div class="m-panel-title">MUSIC</div>
        <div id="m-music-list"></div>
      </div>
      <div class="m-panel-section m-panel-bottom">
        <button id="m-render-btn" class="m-btn m-btn-green m-btn-full" onclick="renderTimeline()" disabled>⬡ Render</button>
      </div>
    </aside>

  </div>

  <script src="/js/mode_switcher.js"></script>
  <script src="/js/modern_music.js"></script>
  <script src="/js/modern.js"></script>
</body>
</html>
```

- [ ] **Step 2: Verify page loads without JS errors**

Open `https://ai-autoedit.sad-panda.eu/modern.html`. Console should show no errors.

---

### Task 7: modern.css — NLE layout

**Files:**
- Modify: `webapp/static/css/modern.css` (full rewrite)

- [ ] **Step 1: Replace entire modern.css**

```css
/* ── Tokens ─────────────────────────────────────────────────────────── */
:root, [data-theme="dark"] {
  --bg0: #0f172a; --bg1: #1e293b; --bg2: #293548;
  --border: rgba(255,255,255,.08);
  --text: #f1f5f9; --muted: #64748b; --sub: #94a3b8;
  --green-hi: #22c55e; --green-mid: #4ade80; --yellow: #facc15;
  --blue: #3b82f6; --blue-dark: #1d4ed8; --purple: #a855f7;
  --red: #ef4444;
  --font: system-ui, -apple-system, sans-serif;
}
[data-theme="light"] {
  --bg0: #f1f5f9; --bg1: #ffffff; --bg2: #e2e8f0;
  --border: #cbd5e1;
  --text: #0f172a; --muted: #94a3b8; --sub: #475569;
}

*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg0); color: var(--text); font-family: var(--font);
       font-size: 13px; overflow: hidden; -webkit-font-smoothing: antialiased; }

/* ── Root layout ─────────────────────────────────────────────────────── */
#studio { display: flex; height: 100dvh; width: 100vw; }

/* ── Sidebar ─────────────────────────────────────────────────────────── */
#m-sidebar { width: 148px; flex-shrink: 0; background: var(--bg1);
             border-right: 1px solid var(--border); display: flex; flex-direction: column; }
.m-sidebar-header { padding: 12px 14px 8px; font-size: 10px; font-weight: 600;
                    text-transform: uppercase; letter-spacing: .06em; color: var(--muted); }
#m-project-list { flex: 1; overflow-y: auto; padding: 0 6px 8px; }
.m-proj-item { padding: 7px 8px; border-radius: 5px; cursor: pointer;
               border-left: 2px solid transparent; margin-bottom: 2px;
               white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: var(--sub); }
.m-proj-item:hover { background: var(--bg2); color: var(--text); }
.m-proj-item.active { background: var(--bg2); border-left-color: var(--green-hi); color: var(--text); }

/* ── Main column ─────────────────────────────────────────────────────── */
#m-main { flex: 1; display: flex; flex-direction: column; min-width: 0; overflow: hidden; }

/* ── Top bar ─────────────────────────────────────────────────────────── */
#m-topbar { height: 48px; flex-shrink: 0; display: flex; align-items: center; gap: 8px;
            padding: 0 14px; background: var(--bg1); border-bottom: 1px solid var(--border); }
.m-project-name { flex: 1; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.m-topbar-actions { display: flex; gap: 6px; }

/* ── Pool ────────────────────────────────────────────────────────────── */
#m-pool { flex: 1; min-height: 0; display: flex; flex-direction: column;
          border-bottom: 2px solid var(--border); overflow: hidden; }
.m-section-header { flex-shrink: 0; display: flex; align-items: center; gap: 8px;
                    padding: 6px 12px; background: var(--bg1); border-bottom: 1px solid var(--border); }
.m-section-title { font-size: 10px; font-weight: 700; letter-spacing: .06em; color: var(--sub); }
.m-section-meta { font-size: 10px; color: var(--muted); }
#m-pool-grid { flex: 1; overflow-y: auto; display: flex; flex-wrap: wrap;
               gap: 8px; padding: 10px; align-content: flex-start; }

/* ── Pool thumbnail ──────────────────────────────────────────────────── */
.m-thumb { width: 80px; flex-shrink: 0; cursor: grab; }
.m-thumb:active { cursor: grabbing; }
.m-thumb-img { position: relative; height: 54px; border-radius: 4px; overflow: hidden;
               border: 2px solid var(--bg2); transition: transform .15s, border-color .15s; }
.m-thumb:hover .m-thumb-img { transform: translateY(-2px); }
.m-thumb-img img { width: 100%; height: 100%; object-fit: cover; display: block; pointer-events: none; }
.m-thumb-score { position: absolute; bottom: 2px; right: 3px; background: rgba(0,0,0,.7);
                 color: #fff; font-size: 8px; padding: 1px 4px; border-radius: 2px; font-weight: 600; }
.m-thumb-label { font-size: 9px; color: var(--muted); text-align: center; margin-top: 3px;
                 white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.m-score-hi  .m-thumb-img { border-color: var(--green-hi); }
.m-score-mid .m-thumb-img { border-color: var(--green-mid); }
.m-score-low .m-thumb-img { border-color: var(--yellow); }
.m-thumb.banned .m-thumb-img { opacity: .3; border-color: var(--red); }

/* ── Timeline ────────────────────────────────────────────────────────── */
#m-timeline-wrap { height: 200px; flex-shrink: 0; display: flex; flex-direction: column; background: var(--bg0); }
#m-timeline { flex: 1; overflow-x: auto; overflow-y: hidden; padding: 8px 12px;
              display: flex; flex-direction: column; gap: 6px; }
.m-track-row { display: flex; align-items: stretch; gap: 8px; }
.m-track-label { width: 44px; flex-shrink: 0; font-size: 9px; color: var(--muted);
                 text-align: right; padding-top: 4px; }
.m-track-clips { display: flex; gap: 3px; align-items: stretch; min-height: 44px; flex: 1; }
.m-clip { flex-shrink: 0; background: var(--blue-dark); border: 1px solid var(--blue);
          border-radius: 4px; cursor: grab; position: relative; display: flex;
          align-items: center; justify-content: center; min-width: 32px;
          transition: opacity .15s; user-select: none; }
.m-clip:active { cursor: grabbing; opacity: .7; }
.m-clip-name { font-size: 8px; color: #93c5fd; overflow: hidden; text-overflow: ellipsis;
               white-space: nowrap; padding: 0 4px; }
.m-clip-score-bar { position: absolute; bottom: 0; left: 0; right: 0; height: 3px;
                    border-radius: 0 0 3px 3px; }
.m-clip.drag-over { border-color: var(--green-hi); box-shadow: 0 0 0 2px var(--green-hi); }
.m-clip-drop-end { flex-shrink: 0; width: 44px; min-height: 44px; border: 1px dashed var(--bg2);
                   border-radius: 4px; display: flex; align-items: center; justify-content: center; }
.m-clip-drop-end.drag-over { border-color: var(--green-hi); }
.m-track-music { flex: 1; min-width: 200px; height: 28px; background: var(--bg1);
                 border-radius: 3px; border: 1px solid var(--border);
                 display: flex; align-items: center; padding: 0 8px; }
.m-music-label { font-size: 9px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

/* ── Right panel ─────────────────────────────────────────────────────── */
#m-panel { width: 180px; flex-shrink: 0; background: var(--bg1);
           border-left: 1px solid var(--border); display: flex; flex-direction: column; }
.m-panel-section { padding: 12px 10px; border-bottom: 1px solid var(--border); }
.m-panel-title { font-size: 10px; font-weight: 700; letter-spacing: .06em; color: var(--muted); margin-bottom: 8px; }
.m-panel-bottom { margin-top: auto; border-bottom: none; border-top: 1px solid var(--border); }
#m-music-list { display: flex; flex-direction: column; gap: 2px; overflow-y: auto; max-height: 380px; }
.m-track-row-item { padding: 6px 7px; border-radius: 4px; cursor: pointer;
                    border: 1px solid transparent; transition: background .1s; }
.m-track-row-item:hover { background: var(--bg2); }
.m-track-row-item.pinned { background: var(--bg0); border-color: var(--blue); }
.m-track-row-title { font-size: 10px; font-weight: 500; white-space: nowrap;
                     overflow: hidden; text-overflow: ellipsis; }
.m-track-row-meta { font-size: 9px; color: var(--muted); margin-top: 2px; }
.m-track-pinned-dot { color: var(--blue); font-size: 9px; margin-top: 2px; }

/* ── Buttons ─────────────────────────────────────────────────────────── */
.m-btn { border: none; border-radius: 5px; padding: 5px 12px; font-size: 11px;
         font-weight: 600; cursor: pointer; transition: opacity .15s; }
.m-btn:disabled { opacity: .4; cursor: default; }
.m-btn-blue  { background: var(--blue);  color: #fff; }
.m-btn-green { background: #16a34a; color: #fff; }
.m-btn-ghost { background: var(--bg2); color: var(--sub); border: 1px solid var(--border); }
.m-btn-sm    { padding: 3px 9px; font-size: 10px; }
.m-btn-full  { width: 100%; }

/* ── Empty / loading ─────────────────────────────────────────────────── */
.m-empty { padding: 24px; text-align: center; color: var(--muted); font-size: 12px; }
.m-spinner { display: inline-block; width: 14px; height: 14px; border: 2px solid var(--bg2);
             border-top-color: var(--blue); border-radius: 50%; animation: spin .7s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
```

- [ ] **Step 2: Hard-reload modern.html, verify layout**

4-column layout visible: sidebar | pool area | timeline | right panel. No console errors.

---

### Task 8: modern.js — project list, pool, timeline, drag & drop, render

**Files:**
- Modify: `webapp/static/js/modern.js` (full rewrite)

- [ ] **Step 1: Replace entire modern.js**

```js
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
      return r.json();
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
      <div class="m-thumb-label">${f.scene}</div>`;
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
  renderTimeline();
  enableActions(true);
}
window.rebuildTimeline = rebuildTimeline;

const PX_PER_SEC = 18;

function renderTimeline() {
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
      <span class="m-clip-name">${slot.scene}</span>
      <div class="m-clip-score-bar" style="background:${scoreColor}"></div>`;
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
  renderTimeline();
  renderPool();
}

// ── Drag & Drop ───────────────────────────────────────────────────────────────
let _drag = null;   // { from: 'pool'|'timeline', scene, idx }

function onPoolDragStart(e) {
  _drag = { from: 'pool', scene: e.currentTarget.dataset.scene };
  e.dataTransfer.effectAllowed = 'copy';
  e.dataTransfer.setData('text/plain', _drag.scene);
}

function onClipDragStart(e) {
  const idx = parseInt(e.currentTarget.dataset.idx);
  _drag = { from: 'timeline', scene: _timeline[idx]?.scene, idx };
  e.dataTransfer.effectAllowed = 'move';
  e.dataTransfer.setData('text/plain', _drag.scene || '');
  e.stopPropagation();
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
  renderTimeline();
  renderPool();
}

// ── Render ────────────────────────────────────────────────────────────────────
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
  return `${m}:${Math.round(s % 60).toString().padStart(2, '0')}`;
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
```

- [ ] **Step 2: Open modern.html, verify project list populates**

Click a project — pool should load thumbnails. Console: no errors.

- [ ] **Step 3: Verify score border colours**

Scenes ≥0.85 → green border. 0.70–0.84 → lighter green. 0.50–0.69 → yellow.

---

### Task 9: modern_music.js — music selection + timeline rebuild

**Files:**
- Modify: `webapp/static/js/modern_music.js` (full rewrite)

- [ ] **Step 1: Replace entire modern_music.js**

```js
// modern_music.js — music list, pin, rebuild trigger

async function loadMusicList(jobId) {
  const [musicData, jobData] = await Promise.all([
    window._modernApi.get(`/api/music?job_id=${jobId}`),
    window._modernApi.get(`/api/jobs/${jobId}`),
  ]);
  const tracks = musicData?.tracks || [];

  // Restore pinned track from saved job params
  if (jobData?.params?.selected_track) {
    _pinnedTrack = jobData.params.selected_track;
  }

  renderMusicList(tracks);

  if (_pinnedTrack) {
    const t = tracks.find(t => t.file === _pinnedTrack);
    const label = document.getElementById('m-music-label');
    if (label) label.textContent = t?.title || _pinnedTrack.split('/').pop();
    const rebuild = document.getElementById('m-btn-rebuild');
    if (rebuild) rebuild.disabled = false;
    rebuildTimeline();
  }
}
window.loadMusicList = loadMusicList;

function renderMusicList(tracks) {
  const list = document.getElementById('m-music-list');
  if (!list) return;
  if (tracks.length === 0) {
    list.innerHTML = '<div class="m-empty">No tracks found</div>';
    return;
  }
  list.innerHTML = tracks.map(t => {
    const isPinned = t.file === _pinnedTrack;
    const dur = fmtSec(t.duration || 0);
    const bpm = t.bpm ? `· ${Math.round(t.bpm)} BPM` : '';
    // JSON.stringify tracks for onclick — escape quotes for HTML attribute
    const tracksAttr = encodeURIComponent(JSON.stringify(tracks));
    return `<div class="m-track-row-item${isPinned ? ' pinned' : ''}"
                 onclick="pinTrack('${t.file.replace(/'/g, "\\'")}', decodeAndParseTracks('${tracksAttr}'))">
      <div class="m-track-row-title">${t.title || t.file.split('/').pop()}</div>
      <div class="m-track-row-meta">${dur} ${bpm}</div>
      ${isPinned ? '<div class="m-track-pinned-dot">● pinned</div>' : ''}
    </div>`;
  }).join('');
}

function decodeAndParseTracks(encoded) {
  try { return JSON.parse(decodeURIComponent(encoded)); } catch { return []; }
}
window.decodeAndParseTracks = decodeAndParseTracks;

async function pinTrack(file, tracks) {
  _pinnedTrack = (_pinnedTrack === file) ? null : file;
  renderMusicList(tracks);

  const label = document.getElementById('m-music-label');
  if (label) {
    const t = tracks.find(t => t.file === _pinnedTrack);
    label.textContent = _pinnedTrack ? (t?.title || _pinnedTrack.split('/').pop()) : 'no track selected';
  }

  const rebuild = document.getElementById('m-btn-rebuild');
  if (rebuild) rebuild.disabled = !_pinnedTrack;

  if (_pinnedTrack) await rebuildTimeline();
}
window.pinTrack = pinTrack;
```

- [ ] **Step 2: Test music selection**

Open a project in modern.html. Right panel should list music tracks. Click a track → it pins (blue border) → timeline builds → clips appear.

- [ ] **Step 3: Test music switch**

With timeline built, click a different track → timeline rebuilds with new music. Old timeline is replaced.

- [ ] **Step 4: Test timeline drag reorder**

Drag a clip within the timeline to a new position. Verify order changes. Verify `m-timeline-meta` updates with correct clip count.

- [ ] **Step 5: Test remove clip (double-click)**

Double-click a clip → it disappears from timeline → pool thumbnail becomes faded (banned state).

- [ ] **Step 6: Test drag from pool to timeline**

Drag a faded (banned) thumbnail from pool onto a timeline slot → it appears in timeline → pool thumbnail returns to normal opacity.

---

### Task 10: Integration test + Render

- [ ] **Step 1: End-to-end test**

1. Navigate to `https://ai-autoedit.sad-panda.eu/modern.html`
2. Click a project with analyzed scenes and music files
3. Verify pool loads with thumbnails and score colours
4. Pin a music track in right panel
5. Verify timeline builds automatically
6. Drag a clip to a new position in timeline
7. Double-click a clip to remove it
8. Click "⬡ Render" → alert shows "Render started"
9. Switch to Legacy UI → verify render job is running

- [ ] **Step 2: Test Legacy → Modern switch**

In Legacy UI → Settings → Interface Mode → "Modern (Studio)" → verify navigation to `modern.html`.

- [ ] **Step 3: Test Modern → Legacy switch**

In Modern UI → "← Legacy" button → verify navigation to `index.html`. Legacy UI works normally.

---

### Task 11: Commit Phase 2

- [ ] **Commit**

```bash
git add webapp/static/modern.html \
         webapp/static/css/modern.css \
         webapp/static/js/modern.js \
         webapp/static/js/modern_music.js
git commit -m "feat: Modern UI v1 — NLE layout with music-driven timeline, pool drag & drop"
```

---

## Post-implementation checklist

- [ ] Legacy UI loads without JS errors
- [ ] Legacy project open → Select Scenes gallery works → render works
- [ ] Modern UI loads without JS errors
- [ ] Modern project open → pool shows thumbnails with score colours
- [ ] Pin track → timeline builds from music-driven preview
- [ ] Drag pool → timeline: inserts clip
- [ ] Drag timeline → timeline: reorders clip
- [ ] Double-click timeline clip: removes, marks banned in pool
- [ ] Render button: posts to render-music-driven, shows confirmation
- [ ] No `isModern` / `openModernJob` / `studio-app` references in legacy JS files
