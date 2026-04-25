# Modern UI Phase 4 — Analyze / New Project + Settings

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add native Analyze/New Project modal and per-project Settings panel to Modern UI, eliminating the Legacy UI dependency for these two workflows.

**Architecture:** New `modern_analyze.js` handles analyze modal and settings panel. The analyze modal calls `POST /api/jobs?analyze_only=true` (new project) or `POST /api/jobs/{id}/rerun` (re-analyze existing). Settings panel reads via `GET /api/job-config` and writes via `PUT /api/job-config` + `PATCH /api/jobs/{id}/params`. Progress feeds into the existing top-bar status bar via `_connectJobProgress`. Pool auto-reloads when analyze completes.

**Tech Stack:** Vanilla JS (no frameworks), FastAPI (existing endpoints), existing `.m-*` CSS design tokens

---

## File Map

| Action | File |
|--------|------|
| Create | `webapp/static/js/modern_analyze.js` |
| Modify | `webapp/static/modern.html` — analyze modal HTML, settings section in `#m-panel`, add `<script>` tag |
| Modify | `webapp/static/css/modern.css` — minimal new rules |
| Modify | `webapp/static/js/modern.js` — `loadPool` call on analyze-done, expose `_connectJobProgress` |

---

## Task 1: Analyze modal HTML

**Files:**
- Modify: `webapp/static/modern.html`

- [ ] **Step 1: Add analyze modal HTML**

Insert immediately after the `<!-- Music modal -->` closing `</div>` (after line ~161):

```html
  <!-- Analyze modal -->
  <div id="m-analyze-modal" class="m-modal-overlay" style="display:none"
       onclick="if(event.target===this)closeAnalyzeModal()">
    <div class="m-modal m-analyze-modal-inner">
      <div class="m-modal-hd">
        <span class="m-modal-title">⚡ Analyze / New Project</span>
        <button class="m-btn m-btn-ghost m-btn-sm" onclick="closeAnalyzeModal()">×</button>
      </div>
      <div class="m-modal-dir">
        <input id="m-analyze-dir" class="m-input" type="text" placeholder="Project directory…">
        <button class="m-btn m-btn-ghost m-btn-sm" onclick="analyzeToggleBrowser()">📁</button>
      </div>
      <div id="m-analyze-browser" style="display:none">
        <div id="m-analyze-browser-path" class="m-section-meta"
             style="padding:4px 0;display:flex;align-items:center;gap:4px"></div>
        <div id="m-analyze-browser-entries" class="m-music-modal-list"
             style="max-height:180px"></div>
      </div>
      <div id="m-analyze-cam-list" class="m-analyze-cams"></div>
      <div style="margin-top:4px">
        <button class="m-btn m-btn-ghost m-btn-sm" onclick="analyzeAddCam()">+ Camera</button>
      </div>
      <div class="m-analyze-opts">
        <label class="m-analyze-check">
          <input type="checkbox" id="m-analyze-clip-first" checked>
          <span>CLIP-first</span>
        </label>
        <label class="m-analyze-check" style="margin-left:16px">
          <span>Clip dur:</span>
          <input id="m-analyze-clip-dur" class="m-input" type="number"
                 min="2" max="30" step="0.5" value="6"
                 style="width:56px;margin-left:4px">
          <span>s</span>
        </label>
      </div>
      <div style="margin-top:8px">
        <textarea id="m-analyze-positive" class="m-input" rows="2"
                  placeholder="Positive CLIP prompts (comma-separated)…"
                  style="width:100%;resize:vertical;box-sizing:border-box"></textarea>
      </div>
      <div style="margin-top:4px">
        <textarea id="m-analyze-negative" class="m-input" rows="2"
                  placeholder="Negative CLIP prompts (comma-separated)…"
                  style="width:100%;resize:vertical;box-sizing:border-box"></textarea>
      </div>
      <div class="m-modal-footer">
        <span id="m-analyze-status" class="m-section-meta"></span>
        <button class="m-btn m-btn-blue" id="m-analyze-btn"
                onclick="runAnalyze()" style="margin-left:auto">⚡ Analyze</button>
      </div>
    </div>
  </div>
```

- [ ] **Step 2: Add Settings section to `#m-panel`**

Replace the existing three-button legacy block in `#m-panel`:

Old:
```html
      <div class="m-panel-section">
        <div class="m-panel-actions">
          <button class="m-btn m-btn-ghost m-btn-sm" onclick="goToLegacy('analyze')" title="Analyze scenes (Legacy)">⚡ Analyze</button>
          <button class="m-btn m-btn-ghost m-btn-sm" onclick="goToLegacy('new')" title="New project (Legacy)">+ New</button>
        </div>
        <div style="margin-top:6px">
          <button class="m-btn m-btn-ghost m-btn-full m-btn-sm" onclick="goToLegacy('settings')" title="Settings (Legacy)">⚙ Settings</button>
        </div>
      </div>
```

New:
```html
      <div class="m-panel-section">
        <button class="m-btn m-btn-ghost m-btn-full" onclick="openAnalyzeModal()">⚡ Analyze / New</button>
      </div>
      <div class="m-panel-section" id="m-settings-section">
        <button class="m-btn m-btn-ghost m-btn-full" onclick="toggleSettingsPanel()"
                id="m-settings-toggle">⚙ Settings ▸</button>
        <div id="m-settings-body" style="display:none;margin-top:8px">
          <div class="m-settings-row">
            <span class="m-settings-label">Music dir</span>
            <input id="m-settings-music-dir" class="m-input m-settings-input"
                   type="text" placeholder="/data/music…">
          </div>
          <div class="m-settings-row">
            <span class="m-settings-label">Clip dur</span>
            <input id="m-settings-clip-dur" class="m-input"
                   type="number" min="2" max="30" step="0.5"
                   style="width:60px">
            <span class="m-section-meta" style="margin-left:4px">s</span>
          </div>
          <div class="m-settings-row" style="align-items:flex-start">
            <span class="m-settings-label" style="padding-top:4px">Positive</span>
            <textarea id="m-settings-positive" class="m-input m-settings-textarea"
                      rows="2" placeholder="Positive prompts…"></textarea>
          </div>
          <div class="m-settings-row" style="align-items:flex-start">
            <span class="m-settings-label" style="padding-top:4px">Negative</span>
            <textarea id="m-settings-negative" class="m-input m-settings-textarea"
                      rows="2" placeholder="Negative prompts…"></textarea>
          </div>
          <button class="m-btn m-btn-blue m-btn-full m-btn-sm"
                  onclick="saveSettings()" style="margin-top:6px">Save</button>
        </div>
      </div>
      <div class="m-panel-section" style="margin-top:auto;padding-top:8px">
        <button class="m-btn m-btn-ghost m-btn-full m-btn-sm"
                onclick="location.href='index.html'+location.search"
                title="Legacy UI">⚙ Legacy UI</button>
      </div>
```

- [ ] **Step 3: Add `modern_analyze.js` script tag**

Between `<script src="/js/modern_music.js"></script>` and `<script src="/js/modern.js"></script>`:

```html
  <script src="/js/modern_analyze.js"></script>
```

- [ ] **Step 4: Verify HTML loads** — open `modern.html` in browser, confirm no JS errors on load.

---

## Task 2: CSS additions

**Files:**
- Modify: `webapp/static/css/modern.css`

- [ ] **Step 1: Append new rules at end of file**

```css
/* ── Analyze modal ─────────────────────────────────────────────────────────── */
.m-analyze-modal-inner { max-width: 540px; width: 100%; }

.m-analyze-cams { display: flex; flex-direction: column; gap: 4px; margin-top: 8px; }

.m-analyze-cam-row { display: flex; align-items: center; gap: 6px; }
.m-analyze-cam-row select {
  flex: 1;
  background: var(--surface2);
  color: var(--fg);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 3px 6px;
  font-size: 12px;
}

.m-analyze-opts {
  display: flex; align-items: center; flex-wrap: wrap;
  gap: 4px; margin-top: 8px; font-size: 12px;
}
.m-analyze-check { display: flex; align-items: center; gap: 4px; cursor: pointer; }
.m-analyze-check input[type="checkbox"] { cursor: pointer; }

/* ── Settings panel ─────────────────────────────────────────────────────────── */
.m-settings-row { display: flex; align-items: center; gap: 6px; margin-bottom: 5px; }
.m-settings-label { font-size: 11px; color: var(--muted); width: 52px; flex-shrink: 0; }
.m-settings-input { flex: 1; font-size: 12px; }
.m-settings-textarea { flex: 1; font-size: 11px; resize: vertical; }
```

- [ ] **Step 2: Verify styles** — click ⚡ Analyze / New, confirm modal layout correct. Click ⚙ Settings ▸, confirm fields render.

---

## Task 3: `modern_analyze.js`

**Files:**
- Create: `webapp/static/js/modern_analyze.js`

- [ ] **Step 1: Create the file with full implementation**

```javascript
// modern_analyze.js — Analyze / New Project modal + Settings panel

// ── Analyze modal state ───────────────────────────────────────────────────────
let _analyzeBrowserOpen = false;
let _analyzeSubdirs = [];

// ── Open / close ──────────────────────────────────────────────────────────────
async function openAnalyzeModal() {
  const modal = document.getElementById('m-analyze-modal');
  if (!modal) return;
  document.getElementById('m-analyze-status').textContent = '';
  document.getElementById('m-analyze-btn').disabled = false;
  _analyzeSubdirs = [];

  if (typeof _jobId !== 'undefined' && _jobId) {
    const job = await window._modernApi.get(`/api/jobs/${_jobId}`);
    if (job?.params?.work_dir) {
      const wd = job.params.work_dir;
      document.getElementById('m-analyze-dir').value = wd;
      const cfg = await window._modernApi.get(
        `/api/job-config?dir=${encodeURIComponent(wd)}`
      );
      if (cfg) {
        document.getElementById('m-analyze-clip-dur').value     = cfg.clip_scan_clip_dur ?? 6;
        document.getElementById('m-analyze-clip-first').checked = cfg.clip_first !== false;
        document.getElementById('m-analyze-positive').value     = cfg.positive ?? '';
        document.getElementById('m-analyze-negative').value     = cfg.negative ?? '';
      }
      _analyzeSubdirs = await _fetchAnalyzeSubdirs(wd);
      const camList = document.getElementById('m-analyze-cam-list');
      camList.innerHTML = '';
      const cams = job.params.cameras
        || [job.params.cam_a, job.params.cam_b].filter(Boolean);
      const toLoad = cams.length ? cams : _analyzeSubdirs.slice(0, 2);
      for (const cam of toLoad) _appendAnalyzeCamRow(camList, cam, _analyzeSubdirs);
    }
  }
  modal.style.display = 'flex';
}
window.openAnalyzeModal = openAnalyzeModal;

function closeAnalyzeModal() {
  document.getElementById('m-analyze-modal').style.display = 'none';
  _closeBrowser();
}
window.closeAnalyzeModal = closeAnalyzeModal;

// ── Directory browser ─────────────────────────────────────────────────────────
async function analyzeToggleBrowser() {
  if (_analyzeBrowserOpen) { _closeBrowser(); return; }
  _analyzeBrowserOpen = true;
  const dir = document.getElementById('m-analyze-dir').value.trim();
  await _loadBrowser(dir || null);
}
window.analyzeToggleBrowser = analyzeToggleBrowser;

function _closeBrowser() {
  _analyzeBrowserOpen = false;
  const el = document.getElementById('m-analyze-browser');
  if (el) el.style.display = 'none';
}

async function _loadBrowser(path) {
  const el = document.getElementById('m-analyze-browser');
  if (!el) return;
  el.style.display = '';
  const entries = document.getElementById('m-analyze-browser-entries');
  if (entries) entries.innerHTML =
    '<div style="padding:4px;color:var(--muted)">Loading…</div>';

  const url = path
    ? `/api/browse?path=${encodeURIComponent(path)}`
    : '/api/browse';
  const data = await window._modernApi.get(url);
  if (!data) {
    if (entries) entries.innerHTML =
      '<div style="padding:4px;color:var(--red)">Error loading directory</div>';
    return;
  }

  const pathEl = document.getElementById('m-analyze-browser-path');
  if (pathEl) {
    pathEl.innerHTML = '';
    if (data.parent) {
      const up = document.createElement('button');
      up.className = 'm-btn m-btn-ghost m-btn-sm';
      up.textContent = '↑ ..';
      up.onclick = () => _loadBrowser(data.parent);
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
  selBtn.onclick = () => _selectBrowserPath(data.path);
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

    row.appendChild(icon);
    row.appendChild(name);

    if (e.has_autoframe) {
      const badge = document.createElement('span');
      badge.style.cssText = 'font-size:10px;color:var(--green)';
      badge.textContent = '✓ analyzed';
      row.appendChild(badge);
    } else if (e.has_mp4) {
      const badge = document.createElement('span');
      badge.style.cssText = 'font-size:10px;color:var(--muted)';
      badge.textContent = 'has MP4';
      row.appendChild(badge);
    }

    if (e.has_mp4 || e.has_autoframe) {
      const pickBtn = document.createElement('button');
      pickBtn.className = 'm-btn m-btn-ghost m-btn-sm';
      pickBtn.textContent = 'Pick';
      pickBtn.onclick = async ev => {
        ev.stopPropagation();
        await _selectBrowserPath(e.path);
      };
      row.appendChild(pickBtn);
    }

    row.onclick = () => _loadBrowser(e.path);
    entries.appendChild(row);
  }
}

async function _selectBrowserPath(path) {
  document.getElementById('m-analyze-dir').value = path;
  _closeBrowser();
  _analyzeSubdirs = await _fetchAnalyzeSubdirs(path);
  const camList = document.getElementById('m-analyze-cam-list');
  if (!camList) return;
  camList.innerHTML = '';
  for (const cam of _analyzeSubdirs.slice(0, 2))
    _appendAnalyzeCamRow(camList, cam, _analyzeSubdirs);
}

async function _fetchAnalyzeSubdirs(dir) {
  if (!dir) return [];
  const data = await window._modernApi.get(
    `/api/subdirs?dir=${encodeURIComponent(dir)}`
  );
  return Array.isArray(data) ? data : [];
}

// ── Camera rows ───────────────────────────────────────────────────────────────
function _appendAnalyzeCamRow(container, selected, subdirs) {
  const row = document.createElement('div');
  row.className = 'm-analyze-cam-row';

  const idx = container.querySelectorAll('.m-analyze-cam-row').length;
  const label = document.createElement('span');
  label.style.cssText = 'font-size:11px;color:var(--muted);width:40px;flex-shrink:0';
  label.textContent = 'Cam ' + ('ABCDEFGH'[idx] || String.fromCharCode(65 + idx));

  const sel = document.createElement('select');
  const none = document.createElement('option');
  none.value = ''; none.textContent = '— none —';
  sel.appendChild(none);
  for (const d of (subdirs || _analyzeSubdirs)) {
    const o = document.createElement('option');
    o.value = o.textContent = d;
    if (d === selected) o.selected = true;
    sel.appendChild(o);
  }

  const rm = document.createElement('button');
  rm.className = 'm-btn m-btn-ghost m-btn-sm';
  rm.textContent = '−'; rm.title = 'Remove camera';
  rm.onclick = () => { row.remove(); _relabelAnalyzeCams(container); };

  row.append(label, sel, rm);
  container.appendChild(row);
}

function _relabelAnalyzeCams(container) {
  container.querySelectorAll('.m-analyze-cam-row').forEach((row, i) => {
    const lbl = row.querySelector('span');
    if (lbl) lbl.textContent = 'Cam ' + ('ABCDEFGH'[i] || String.fromCharCode(65 + i));
  });
}

async function analyzeAddCam() {
  const camList = document.getElementById('m-analyze-cam-list');
  if (!camList) return;
  if (!_analyzeSubdirs.length) {
    const dir = document.getElementById('m-analyze-dir').value.trim();
    if (dir) _analyzeSubdirs = await _fetchAnalyzeSubdirs(dir);
  }
  _appendAnalyzeCamRow(camList, '', _analyzeSubdirs);
}
window.analyzeAddCam = analyzeAddCam;

// ── Run analyze ───────────────────────────────────────────────────────────────
async function runAnalyze() {
  const dir = document.getElementById('m-analyze-dir').value.trim();
  if (!dir) { alert('Select a project directory first.'); return; }

  const cameras = [...document.getElementById('m-analyze-cam-list')
    .querySelectorAll('select')]
    .map(s => s.value.trim()).filter(Boolean);
  const clipFirst = document.getElementById('m-analyze-clip-first').checked;
  const clipDur   = parseFloat(document.getElementById('m-analyze-clip-dur').value) || 6;
  const positive  = document.getElementById('m-analyze-positive').value.trim() || null;
  const negative  = document.getElementById('m-analyze-negative').value.trim() || null;

  const btn    = document.getElementById('m-analyze-btn');
  const status = document.getElementById('m-analyze-status');
  if (btn)    btn.disabled = true;
  if (status) status.textContent = 'Starting…';

  const params = {
    work_dir:           dir,
    cameras:            cameras.length ? cameras : null,
    clip_first:         clipFirst,
    clip_scan_clip_dur: clipDur,
    positive,
    negative,
  };

  // Re-use existing job when dir matches current project
  let data = null;
  if (typeof _jobId !== 'undefined' && _jobId) {
    const cur = await window._modernApi.get(`/api/jobs/${_jobId}`);
    if (cur?.params?.work_dir === dir) {
      data = await window._modernApi.post(`/api/jobs/${_jobId}/rerun`, params);
    }
  }

  if (!data) {
    try {
      const r = await fetch('/api/jobs?analyze_only=true', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(params),
      });
      data = r.ok ? await r.json() : null;
    } catch { data = null; }
  }

  if (!data?.id) {
    if (btn)    btn.disabled = false;
    if (status) status.textContent = 'Failed — check server log';
    return;
  }

  closeAnalyzeModal();
  if (typeof openProject === 'function') await openProject(data.id);
  if (typeof _connectJobProgress === 'function') _connectJobProgress(data.id);
}
window.runAnalyze = runAnalyze;

// ── Settings panel ────────────────────────────────────────────────────────────
let _settingsOpen = false;

function toggleSettingsPanel() {
  _settingsOpen = !_settingsOpen;
  const body = document.getElementById('m-settings-body');
  const btn  = document.getElementById('m-settings-toggle');
  if (body) body.style.display = _settingsOpen ? '' : 'none';
  if (btn)  btn.textContent = `⚙ Settings ${_settingsOpen ? '▾' : '▸'}`;
  if (_settingsOpen) _loadSettingsPanel();
}
window.toggleSettingsPanel = toggleSettingsPanel;

async function _loadSettingsPanel() {
  if (typeof _jobId === 'undefined' || !_jobId) return;
  const job = await window._modernApi.get(`/api/jobs/${_jobId}`);
  if (!job?.params?.work_dir) return;
  const cfg = await window._modernApi.get(
    `/api/job-config?dir=${encodeURIComponent(job.params.work_dir)}`
  );
  if (!cfg) return;
  const set = (id, val) => {
    const el = document.getElementById(id);
    if (el && val != null) el.value = val;
  };
  set('m-settings-music-dir', cfg.music_dir);
  set('m-settings-clip-dur',  cfg.clip_scan_clip_dur);
  set('m-settings-positive',  cfg.positive);
  set('m-settings-negative',  cfg.negative);
}

async function saveSettings() {
  if (typeof _jobId === 'undefined' || !_jobId) {
    alert('No project selected.'); return;
  }
  const job = await window._modernApi.get(`/api/jobs/${_jobId}`);
  if (!job?.params?.work_dir) return;

  const musicDir = document.getElementById('m-settings-music-dir')?.value.trim() || null;
  const clipDur  = parseFloat(document.getElementById('m-settings-clip-dur')?.value) || null;
  const positive = document.getElementById('m-settings-positive')?.value.trim()  || null;
  const negative = document.getElementById('m-settings-negative')?.value.trim()  || null;

  const payload = { work_dir: job.params.work_dir };
  if (musicDir) payload.music_dir           = musicDir;
  if (clipDur)  payload.clip_scan_clip_dur  = clipDur;
  if (positive) payload.positive            = positive;
  if (negative) payload.negative            = negative;

  await fetch('/api/job-config', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  await window._modernApi.patch(`/api/jobs/${_jobId}/params`, payload);

  const btn = document.getElementById('m-settings-toggle');
  const orig = btn?.textContent;
  if (btn) btn.textContent = '⚙ Settings ✓';
  setTimeout(() => { if (btn && orig) btn.textContent = orig; }, 1500);
}
window.saveSettings = saveSettings;
```

- [ ] **Step 2: Verify file created**

```bash
ls -lh webapp/static/js/modern_analyze.js
```

Expected: file exists, size > 0.

---

## Task 4: Wire analyze completion → pool reload

**Files:**
- Modify: `webapp/static/js/modern.js`

Currently `_connectJobProgress` calls `loadResults()` on `done` but not `loadPool()`. When analyze completes, the pool needs to refresh.

- [ ] **Step 1: Add `loadPool` to the `done` branch**

Find (around line 445):
```javascript
      } else if (st === 'done') {
        _showStatus('done', '✓ complete', 100, 'done');
        _setRenderBusy(false);
        ws.close();
        setTimeout(_hideStatus, 4000);
        loadResults();
```

Replace with:
```javascript
      } else if (st === 'done') {
        _showStatus('done', '✓ complete', 100, 'done');
        _setRenderBusy(false);
        ws.close();
        setTimeout(_hideStatus, 4000);
        loadResults();
        if (_jobId) loadPool(_jobId);
```

- [ ] **Step 2: Expose `_connectJobProgress` on `window`**

Find the closing `}` of the `_connectJobProgress` function definition (the line after `ws.onclose = () => { if (_jobWs === ws) _jobWs = null; };`):

```javascript
  ws.onclose = () => { if (_jobWs === ws) _jobWs = null; };
}
```

Add immediately after:
```javascript
window._connectJobProgress = _connectJobProgress;
```

- [ ] **Step 3: Manual smoke test**
  - Select a project in Modern UI
  - Click ⚡ Analyze / New — modal opens, pre-filled with current project dir and cameras
  - Browse to a different directory with MP4s — cameras auto-populate
  - Close modal with ×
  - Click ⚙ Settings ▸ — panel expands, shows music_dir, clip_dur, prompts
  - Edit clip_dur, click Save — button flashes ✓
  - Click ⚡ Analyze / New again for current project → click ⚡ Analyze:
    - Modal closes
    - Status bar shows "analyzing"
    - Pool reloads when done

---

## Task 5: Commit

- [ ] **Step 1: Stage files**

```bash
git add webapp/static/modern.html \
        webapp/static/js/modern_analyze.js \
        webapp/static/css/modern.css \
        webapp/static/js/modern.js
```

- [ ] **Step 2: Commit**

```bash
git commit -m "$(cat <<'EOF'
feat(modern-ui): Phase 4 — Analyze/New modal + Settings panel

- openAnalyzeModal(): dir browser, camera rows, CLIP prompts, analyze trigger
- Reuses existing job (rerun) when same dir; creates new job otherwise
- toggleSettingsPanel(): music_dir, clip_dur, prompts — reads/writes job-config
- Pool auto-reloads on analyze done via loadPool in _connectJobProgress
- _connectJobProgress exposed as window property for cross-file use
- Legacy UI buttons replaced with native modal + panel

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 3: Verify**

```bash
git log --oneline -3
```

---

## Self-Review

**Spec coverage:**
- ✓ Analyze new project — `POST /api/jobs?analyze_only=true` path
- ✓ Re-analyze existing project — `POST /api/jobs/{id}/rerun` when dir matches `_jobId`
- ✓ Directory browser — inline panel inside modal, `GET /api/browse`, navigable
- ✓ Camera selection — auto-loads `GET /api/subdirs`, up to 2 cams pre-populated
- ✓ CLIP prompts + clip_dur in modal
- ✓ Progress feedback — `_connectJobProgress` wired after `runAnalyze`
- ✓ Pool auto-reload — `loadPool(_jobId)` added to `done` branch
- ✓ Settings panel — music_dir, clip_dur, positive, negative; reads `GET /api/job-config`, writes `PUT + PATCH`
- ✓ Legacy buttons replaced — no `goToLegacy` calls for analyze/settings

**API param names verified:**
- `GET /api/job-config` uses `dir=` query param (confirmed in `config.py:163`) — plan uses `?dir=` ✓
- `POST /api/jobs` sends `work_dir`, `cameras`, `clip_first`, `clip_scan_clip_dur`, `positive`, `negative` — all match `JobParams` fields ✓

**Load order:** `modern_analyze.js` loads before `modern.js`. Functions in `modern_analyze.js` reference `_jobId`, `openProject`, `_connectJobProgress` via `typeof` guards or `window.*` — safe because all calls are event-driven, not at parse time ✓
