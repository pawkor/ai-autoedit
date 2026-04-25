# Modern UI Phase 5 — Shorts Interface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a native Shorts generation modal to Modern UI — right-panel button opens a modal with settings, submitting closes the modal and shows progress in the top-bar status strip.

**Architecture:** New `modern_shorts.js` handles the modal; `modern.js` gains `shorts_status` / `shorts_batch_progress` WebSocket handling and a `_setShortsRenderBusy` helper; `jobs.py` PATCH allowlist extended for `shorts_*` params. No new backend routes.

**Tech Stack:** Vanilla JS, FastAPI (existing endpoints), `.m-*` CSS design tokens.

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Modify | `webapp/routers/jobs.py:1371` | Add `shorts_text/multicam/beat_sync/best` to PATCH allowlist |
| Modify | `webapp/static/modern.html` | Shorts modal HTML, `▶ Shorts` panel button, script tag |
| Modify | `webapp/static/css/modern.css` | `.m-shorts-count-row` rule |
| Create | `webapp/static/js/modern_shorts.js` | `openShortsModal`, `closeShortsModal`, `renderShorts` |
| Modify | `webapp/static/js/modern.js` | `shorts_status`/`shorts_batch_progress` WS handling, `_setShortsRenderBusy`, `enableActions` |

---

## Task 1: Extend PATCH allowlist in jobs.py

**Files:**
- Modify: `webapp/routers/jobs.py:1371`

- [ ] **Step 1: Open the file and find the allowlist**

The line to change is at line 1371:

```python
    allowed = {"threshold", "max_scene", "per_file", "music_dir", "min_gap_sec", "music_files", "selected_track", "manual_timeline", "manual_overrides"}
```

- [ ] **Step 2: Add shorts fields to the allowlist**

Replace that line with:

```python
    allowed = {"threshold", "max_scene", "per_file", "music_dir", "min_gap_sec", "music_files", "selected_track", "manual_timeline", "manual_overrides", "shorts_text", "shorts_multicam", "shorts_beat_sync", "shorts_best"}
```

- [ ] **Step 3: Verify the change**

Run:
```bash
grep -n "allowed = " webapp/routers/jobs.py
```

Expected output contains `shorts_text` in the set.

---

## Task 2: HTML — modal + panel button + script tag

**Files:**
- Modify: `webapp/static/modern.html`

Context: `modern.html` has a `<!-- Settings modal -->` block ending around line 255, then a `<!-- Results modal -->` block. The `#m-panel` aside has sections in order: Music, Build timeline, Analyze/New, Settings, Legacy UI. Scripts load at bottom: `modern_music.js` → `modern_analyze.js` → `modern.js`.

- [ ] **Step 1: Add `▶ Shorts` button to `#m-panel`**

Find in `#m-panel`:

```html
      <div class="m-panel-section">
        <button class="m-btn m-btn-ghost m-btn-full" onclick="openSettingsModal()">⚙ Settings</button>
      </div>
      <div class="m-panel-section" style="margin-top:auto;padding-top:8px">
        <button class="m-btn m-btn-ghost m-btn-full m-btn-sm"
                onclick="location.href='index.html'+location.search"
                title="Legacy UI">⚙ Legacy UI</button>
      </div>
```

Replace with:

```html
      <div class="m-panel-section">
        <button class="m-btn m-btn-ghost m-btn-full" onclick="openSettingsModal()">⚙ Settings</button>
      </div>
      <div class="m-panel-section">
        <button id="m-btn-shorts" class="m-btn m-btn-ghost m-btn-full" onclick="openShortsModal()" disabled>▶ Shorts</button>
      </div>
      <div class="m-panel-section" style="margin-top:auto;padding-top:8px">
        <button class="m-btn m-btn-ghost m-btn-full m-btn-sm"
                onclick="location.href='index.html'+location.search"
                title="Legacy UI">⚙ Legacy UI</button>
      </div>
```

- [ ] **Step 2: Add Shorts modal HTML**

Insert immediately after the closing `</div>` of `<!-- Settings modal -->` and before `<!-- Results modal -->`:

```html
  <!-- Shorts modal -->
  <div id="m-shorts-modal" class="m-modal-overlay" style="display:none"
       onclick="if(event.target===this)closeShortsModal()">
    <div class="m-modal" style="max-width:400px;width:100%">
      <div class="m-modal-hd">
        <span class="m-modal-title">▶ Shorts</span>
        <button class="m-btn m-btn-ghost m-btn-sm" onclick="closeShortsModal()">×</button>
      </div>
      <div style="padding:10px 0 4px">
        <div class="m-shorts-count-row">
          <span class="m-settings-label">Count</span>
          <input id="m-shorts-count" class="m-input" type="number"
                 min="1" max="9" step="1" value="1" style="width:56px">
        </div>
        <div class="m-analyze-opts" style="margin-top:10px">
          <label class="m-analyze-check">
            <input type="checkbox" id="m-shorts-text">
            <span>Text overlays</span>
          </label>
          <label class="m-analyze-check" id="m-shorts-multicam-row">
            <input type="checkbox" id="m-shorts-multicam">
            <span>Multicam</span>
          </label>
          <label class="m-analyze-check">
            <input type="checkbox" id="m-shorts-beat">
            <span>Beat sync</span>
          </label>
          <label class="m-analyze-check">
            <input type="checkbox" id="m-shorts-best">
            <span>Best of best</span>
          </label>
        </div>
      </div>
      <div class="m-modal-footer">
        <span id="m-shorts-status" class="m-section-meta"></span>
        <button class="m-btn m-btn-green" id="m-shorts-btn"
                onclick="renderShorts()" style="margin-left:auto">⬡ Make Shorts</button>
      </div>
    </div>
  </div>
```

- [ ] **Step 3: Add script tag**

Find:
```html
  <script src="/js/modern_analyze.js"></script>
  <script src="/js/modern.js"></script>
```

Replace with:
```html
  <script src="/js/modern_analyze.js"></script>
  <script src="/js/modern_shorts.js"></script>
  <script src="/js/modern.js"></script>
```

- [ ] **Step 4: Verify in browser**

Open `http://localhost/modern.html`. Confirm:
- Right panel has `▶ Shorts` button (disabled, no project selected)
- No JS errors in console

---

## Task 3: CSS — `.m-shorts-count-row`

**Files:**
- Modify: `webapp/static/css/modern.css`

The modal reuses `.m-settings-label`, `.m-analyze-opts`, `.m-analyze-check` from existing rules. Only one new rule needed.

- [ ] **Step 1: Append to end of `modern.css`**

```css
.m-shorts-count-row { display: flex; align-items: center; gap: 6px; }
```

- [ ] **Step 2: Verify**

Open Shorts modal (select any project, click ▶ Shorts). Confirm count label + input are aligned horizontally.

---

## Task 4: Create `modern_shorts.js`

**Files:**
- Create: `webapp/static/js/modern_shorts.js`

Context: `_jobId` is a global declared in `modern.js`. `window._modernApi` provides `.get(url)` and `.patch(url, body)` helpers. The PATCH endpoint is `PATCH /api/jobs/{id}/params`. The render endpoint is `POST /api/jobs/{id}/render-short` with body `{count, best}`.

- [ ] **Step 1: Create the file**

```javascript
// modern_shorts.js — Shorts generation modal

async function openShortsModal() {
  const modal = document.getElementById('m-shorts-modal');
  if (!modal) return;
  document.getElementById('m-shorts-status').textContent = '';
  document.getElementById('m-shorts-btn').disabled = false;

  if (typeof _jobId === 'undefined' || !_jobId) {
    modal.style.display = 'flex';
    return;
  }

  const job = await window._modernApi.get(`/api/jobs/${_jobId}`);
  if (job?.params) {
    const p = job.params;
    const setChk = (id, val) => {
      const el = document.getElementById(id);
      if (el) el.checked = !!val;
    };
    setChk('m-shorts-text',     p.shorts_text);
    setChk('m-shorts-multicam', p.shorts_multicam);
    setChk('m-shorts-beat',     p.shorts_beat_sync);
    setChk('m-shorts-best',     p.shorts_best);
    const countEl = document.getElementById('m-shorts-count');
    if (countEl && p.shorts_count) countEl.value = p.shorts_count;

    const cams = p.cameras || [p.cam_a, p.cam_b].filter(Boolean);
    const mcRow = document.getElementById('m-shorts-multicam-row');
    if (mcRow) mcRow.style.display = cams.length > 1 ? '' : 'none';
  }

  modal.style.display = 'flex';
}
window.openShortsModal = openShortsModal;

function closeShortsModal() {
  const modal = document.getElementById('m-shorts-modal');
  if (modal) modal.style.display = 'none';
}
window.closeShortsModal = closeShortsModal;

async function renderShorts() {
  if (typeof _jobId === 'undefined' || !_jobId) {
    alert('No project selected.'); return;
  }

  const count    = parseInt(document.getElementById('m-shorts-count')?.value) || 1;
  const text     = document.getElementById('m-shorts-text')?.checked ?? false;
  const multicam = document.getElementById('m-shorts-multicam')?.checked ?? false;
  const beat     = document.getElementById('m-shorts-beat')?.checked ?? false;
  const best     = document.getElementById('m-shorts-best')?.checked ?? false;

  const btn    = document.getElementById('m-shorts-btn');
  const status = document.getElementById('m-shorts-status');
  if (btn)    btn.disabled = true;
  if (status) status.textContent = 'Starting…';

  await window._modernApi.patch(`/api/jobs/${_jobId}/params`, {
    shorts_text:      text,
    shorts_multicam:  multicam,
    shorts_beat_sync: beat,
    shorts_best:      best,
  });

  let data = null;
  try {
    const r = await fetch(`/api/jobs/${_jobId}/render-short`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ count, best }),
    });
    data = r.ok ? await r.json() : null;
  } catch { data = null; }

  if (!data?.id) {
    if (btn)    btn.disabled = false;
    if (status) status.textContent = '✗ Failed to start';
    return;
  }

  closeShortsModal();
}
window.renderShorts = renderShorts;
```

- [ ] **Step 2: Verify file exists**

```bash
ls -lh webapp/static/js/modern_shorts.js
```

Expected: file exists, ~2 KB.

---

## Task 5: Wire WebSocket shorts messages in `modern.js`

**Files:**
- Modify: `webapp/static/js/modern.js`

The `_connectJobProgress` function has `ws.onmessage` ending with a `log` handler then `ws.onclose`. Add shorts handlers after the `log` block. Also add `_setShortsRenderBusy` after `_setRenderBusy`, and add `m-btn-shorts` to `enableActions`.

- [ ] **Step 1: Add shorts WebSocket handling**

Find (in `ws.onmessage` inside `_connectJobProgress`):
```javascript
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
    }
  };
  ws.onclose = () => { if (_jobWs === ws) _jobWs = null; };
```

Replace with:
```javascript
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
        if (msg.done) {
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
```

- [ ] **Step 2: Add `_setShortsRenderBusy` function**

Find:
```javascript
function _setRenderBusy(busy) {
  ['m-btn-render'].forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    el.disabled = busy;
    el.textContent = busy ? 'Rendering…' : '⬡ Render';
  });
}
```

Add immediately after the closing `}`:
```javascript

function _setShortsRenderBusy(busy) {
  const el = document.getElementById('m-btn-shorts');
  if (!el) return;
  el.disabled = busy;
  el.textContent = busy ? 'Generating…' : '▶ Shorts';
}
```

- [ ] **Step 3: Add `m-btn-shorts` to `enableActions`**

Find:
```javascript
function enableActions(on) {
  ['m-btn-rebuild', 'm-btn-preview', 'm-btn-render'].forEach(id => {
```

Replace with:
```javascript
function enableActions(on) {
  ['m-btn-rebuild', 'm-btn-preview', 'm-btn-render', 'm-btn-shorts'].forEach(id => {
```

- [ ] **Step 4: Smoke test**

1. Open `modern.html`, select a project with 2+ cameras
2. Click `▶ Shorts` — modal opens, Multicam checkbox visible, fields pre-filled
3. Select a project with 1 camera → Multicam row hidden
4. Submit (count=1) → modal closes, top bar shows "Generating short…"
5. On done → "✓ Short ready" flashes, Results modal Shorts tab shows new file

---

## Task 6: Commit

- [ ] **Step 1: Stage files**

```bash
git add webapp/routers/jobs.py \
        webapp/static/modern.html \
        webapp/static/css/modern.css \
        webapp/static/js/modern_shorts.js \
        webapp/static/js/modern.js
```

- [ ] **Step 2: Commit**

```bash
git commit -m "$(cat <<'EOF'
feat(modern-ui): Phase 5 — Shorts generation modal

- Right panel ▶ Shorts button opens modal with count, text overlays,
  multicam (hidden for single-cam jobs), beat sync, best of best
- renderShorts(): PATCHes settings to job then POSTs render-short
- shorts_status / shorts_batch_progress WS messages update top-bar
- _setShortsRenderBusy() manages button state during generation
- PATCH /api/jobs/{id}/params allowlist extended for shorts_* fields

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
- ✓ Right-panel `▶ Shorts` button → `openShortsModal()` — Task 2
- ✓ Modal: count, text overlays, multicam (conditional), beat sync, best of best — Task 2
- ✓ Pre-fill from `job.params` on open — Task 4
- ✓ Multicam hidden when ≤1 camera — Task 4
- ✓ PATCH persists settings before triggering — Task 4 + Task 1 (allowlist)
- ✓ `POST /api/jobs/{id}/render-short` with `{count, best}` — Task 4
- ✓ Modal closes immediately after submit — Task 4
- ✓ `shorts_status` WS → top bar "Generating short…" — Task 5
- ✓ `shorts_batch_progress` WS → top bar "Short N/M" — Task 5
- ✓ On done → "✓ Short ready" flash + `loadResults()` — Task 5
- ✓ On error → "✗ Shorts failed" flash — Task 5
- ✓ `m-btn-shorts` enabled/disabled via `enableActions` — Task 5

**Name consistency:**
- `m-shorts-beat` (HTML) → `document.getElementById('m-shorts-beat')` in JS ✓
- `m-shorts-multicam-row` (HTML) → `document.getElementById('m-shorts-multicam-row')` in JS ✓
- `shorts_beat_sync` (job.params key) → PATCH body key `shorts_beat_sync` ✓
- `_setShortsRenderBusy` defined and called in `modern.js` only ✓
