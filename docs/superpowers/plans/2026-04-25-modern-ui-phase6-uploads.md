# Modern UI Phase 6 — YT + IG Upload Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add full YouTube and Instagram upload capability to the Modern UI Results modal, with complete feature parity to the legacy UI.

**Architecture:** New `modern_uploads.js` contains all upload logic parameterized by `jobId`/`workDir` (no global scope coupling with legacy). Three upload modals added to `modern.html`. Upload buttons added in `_renderResultsList()` in `modern.js`. No backend changes needed — all endpoints already exist.

**Tech Stack:** Vanilla JS, HTML, CSS — same patterns as `modern_music.js`, `modern_shorts.js`. Endpoints: `/api/youtube/*`, `/api/ig/*`, `/api/jobs/{id}/save-yt-meta`, `/api/jobs/{id}/generate-yt-meta`, `/api/jobs/{id}/generate-metadata`, `/api/jobs/{id}/youtube-url`.

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `webapp/static/js/modern_uploads.js` | **Create** | All YT + IG modal open/close/upload/poll logic |
| `webapp/static/modern.html` | **Modify** | Add 3 upload modals + `<script src="/js/modern_uploads.js">` |
| `webapp/static/js/modern.js` | **Modify** | Add upload buttons in `_renderResultsList()` |
| `webapp/static/css/modern.css` | **Modify** | Upload modal row styles + result row badge styles |

---

### Task 1: CSS — Upload modal and badge styles

**Files:**
- Modify: `webapp/static/css/modern.css`

- [ ] **Step 1: Append upload CSS to end of modern.css**

```css
/* ── Upload modals ─────────────────────────────────────────────────── */
.m-upload-row { display: flex; align-items: flex-start; gap: 8px; margin-bottom: 8px; }
.m-upload-label { width: 90px; flex-shrink: 0; padding-top: 5px;
                  color: var(--sub); font-size: 12px; }
.m-upload-row input[type="text"],
.m-upload-row select,
.m-upload-row textarea { flex: 1; }
.m-upload-row textarea { resize: vertical; min-height: 60px; }
.m-upload-privacy { display: flex; gap: 12px; align-items: center; padding-top: 4px; }
.m-upload-privacy label { display: flex; align-items: center; gap: 4px;
                           cursor: pointer; color: var(--text); }
.m-upload-status { font-size: 12px; color: var(--muted); min-height: 16px;
                   word-break: break-all; }
.m-upload-gen-row { display: flex; gap: 6px; align-items: center;
                    margin-top: -4px; margin-bottom: 8px; padding-left: 98px; }
.m-upload-existing-row { display: flex; gap: 6px; align-items: center; flex: 1; }
.m-upload-existing-row input { flex: 1; }
/* Result row upload badges */
.m-rf-actions { display: flex; gap: 4px; align-items: center; flex-shrink: 0; }
.m-rf-yt, .m-rf-ig { padding: 2px 7px; border-radius: 4px; font-size: 11px;
                      cursor: pointer; border: 1px solid var(--border);
                      background: var(--bg2); color: var(--text);
                      white-space: nowrap; line-height: 1.6; }
.m-rf-yt:hover:not(:disabled), .m-rf-ig:hover:not(:disabled) { background: var(--bg1); }
.m-rf-yt:disabled, .m-rf-ig:disabled { opacity: .4; cursor: not-allowed; }
.m-rf-yt.linked { color: var(--green-hi); border-color: var(--green-hi); background: transparent; }
.m-rf-ig.linked { color: var(--purple); border-color: var(--purple); background: transparent; }
```

- [ ] **Step 2: Verify — reload modern.html, DevTools console: no CSS errors, no regressions in Analyze/Shorts modals**

---

### Task 2: HTML — Three upload modals

**Files:**
- Modify: `webapp/static/modern.html`

- [ ] **Step 1: Insert 3 modals before `<script src="/js/mode_switcher.js">` (currently line 337)**

Insert immediately before that script tag:

```html
  <!-- YT Main Upload modal -->
  <div id="m-yt-modal" class="m-overlay" style="display:none"
       onclick="if(event.target===this)mYtClose()">
    <div class="m-modal" style="width:520px;max-width:96vw">
      <div class="m-modal-header">
        <span class="m-modal-title">▲ YouTube Upload</span>
        <button class="m-btn m-btn-ghost m-btn-sm" onclick="mYtClose()">×</button>
      </div>
      <div style="padding:12px 0 4px;overflow-y:auto;max-height:70vh">
        <div class="m-upload-row">
          <span class="m-upload-label">File</span>
          <span id="m-yt-filename" style="color:var(--sub);padding-top:5px;word-break:break-all"></span>
        </div>
        <div class="m-upload-row">
          <span class="m-upload-label">Title</span>
          <input id="m-yt-title" class="m-input" type="text"
                 oninput="mYtMetaDirty()" onblur="mYtMetaSave()">
        </div>
        <div class="m-upload-row">
          <span class="m-upload-label">Description</span>
          <textarea id="m-yt-desc" class="m-input" rows="5"
                    oninput="mYtMetaDirty()" onblur="mYtMetaSave()"></textarea>
        </div>
        <div class="m-upload-gen-row">
          <button class="m-btn m-btn-sm" id="m-yt-gen-btn" onclick="mYtGenDesc()">✦ Generate</button>
          <button class="m-btn m-btn-sm" id="m-yt-chapters-btn" onclick="mYtChapters()">✦ AI Chapters</button>
          <span id="m-yt-gen-status" class="m-upload-status"></span>
        </div>
        <div class="m-upload-row">
          <span class="m-upload-label">Notes</span>
          <textarea id="m-yt-notes" class="m-input" rows="2"
                    placeholder="Optional notes for Claude generation"
                    oninput="mYtMetaDirty()" onblur="mYtMetaSave()"></textarea>
        </div>
        <div class="m-upload-row">
          <span class="m-upload-label">Privacy</span>
          <div class="m-upload-privacy">
            <label><input type="radio" name="m-yt-privacy" value="public"> Public</label>
            <label><input type="radio" name="m-yt-privacy" value="unlisted" checked> Unlisted</label>
            <label><input type="radio" name="m-yt-privacy" value="private"> Private</label>
          </div>
        </div>
        <div class="m-upload-row">
          <span class="m-upload-label">Playlist</span>
          <div style="flex:1;display:flex;flex-direction:column;gap:4px">
            <select id="m-yt-playlist" class="m-input"></select>
            <button class="m-btn m-btn-sm" style="align-self:flex-start"
                    onclick="mYtToggleNewPlaylist()">+ New playlist</button>
            <input id="m-yt-new-playlist" class="m-input" type="text"
                   placeholder="New playlist name" style="display:none">
          </div>
        </div>
        <div class="m-upload-row">
          <span class="m-upload-label">Existing URL</span>
          <div class="m-upload-existing-row">
            <input id="m-yt-existing-url" class="m-input" type="text"
                   placeholder="https://youtu.be/…">
            <button class="m-btn m-btn-sm" onclick="mYtSaveUrl()">Save</button>
            <button class="m-btn m-btn-sm m-btn-ghost" onclick="mYtClearUrl()">Clear</button>
          </div>
        </div>
        <div id="m-yt-status" class="m-upload-status" style="margin-top:4px"></div>
      </div>
      <div class="m-modal-footer">
        <button class="m-btn m-btn-green" id="m-yt-upload-btn"
                onclick="mYtUpload()">▲ Upload</button>
      </div>
    </div>
  </div>

  <!-- YT Shorts Upload modal -->
  <div id="m-yts-modal" class="m-overlay" style="display:none"
       onclick="if(event.target===this)mYtsClose()">
    <div class="m-modal" style="width:520px;max-width:96vw">
      <div class="m-modal-header">
        <span class="m-modal-title">▲ YouTube Shorts Upload</span>
        <button class="m-btn m-btn-ghost m-btn-sm" onclick="mYtsClose()">×</button>
      </div>
      <div style="padding:12px 0 4px;overflow-y:auto;max-height:70vh">
        <div class="m-upload-row">
          <span class="m-upload-label">File</span>
          <span id="m-yts-filename" style="color:var(--sub);padding-top:5px;word-break:break-all"></span>
        </div>
        <div class="m-upload-row">
          <span class="m-upload-label">Title</span>
          <input id="m-yts-title" class="m-input" type="text">
        </div>
        <div class="m-upload-row">
          <span class="m-upload-label">Description</span>
          <textarea id="m-yts-desc" class="m-input" rows="4"></textarea>
        </div>
        <div class="m-upload-gen-row">
          <button class="m-btn m-btn-sm" id="m-yts-gen-btn" onclick="mYtsGenDesc()">✦ Generate</button>
          <span id="m-yts-gen-status" class="m-upload-status"></span>
        </div>
        <div class="m-upload-row" id="m-yts-fullvideo-row" style="display:none">
          <span class="m-upload-label">Full video</span>
          <select id="m-yts-fullvideo-select" class="m-input"
                  onchange="mYtsUpdateLink()"></select>
        </div>
        <div class="m-upload-row">
          <span class="m-upload-label">Privacy</span>
          <div class="m-upload-privacy">
            <label><input type="radio" name="m-yts-privacy" value="public" checked> Public</label>
            <label><input type="radio" name="m-yts-privacy" value="unlisted"> Unlisted</label>
            <label><input type="radio" name="m-yts-privacy" value="private"> Private</label>
          </div>
        </div>
        <div class="m-upload-row">
          <span class="m-upload-label">Playlist</span>
          <div style="flex:1;display:flex;flex-direction:column;gap:4px">
            <select id="m-yts-playlist" class="m-input"></select>
            <button class="m-btn m-btn-sm" style="align-self:flex-start"
                    onclick="mYtsToggleNewPlaylist()">+ New playlist</button>
            <input id="m-yts-new-playlist" class="m-input" type="text"
                   placeholder="New playlist name" style="display:none">
          </div>
        </div>
        <div id="m-yts-status" class="m-upload-status" style="margin-top:4px"></div>
      </div>
      <div class="m-modal-footer">
        <button class="m-btn m-btn-green" id="m-yts-upload-btn"
                onclick="mYtsUpload()">▲ Upload</button>
      </div>
    </div>
  </div>

  <!-- IG Reel Upload modal -->
  <div id="m-ig-modal" class="m-overlay" style="display:none"
       onclick="if(event.target===this)mIgClose()">
    <div class="m-modal" style="width:460px;max-width:96vw">
      <div class="m-modal-header">
        <span class="m-modal-title">▲ Instagram Reel Upload</span>
        <button class="m-btn m-btn-ghost m-btn-sm" onclick="mIgClose()">×</button>
      </div>
      <div style="padding:12px 0 4px">
        <div class="m-upload-row">
          <span class="m-upload-label">File</span>
          <span id="m-ig-filename" style="color:var(--sub);padding-top:5px;word-break:break-all"></span>
        </div>
        <div id="m-ig-token-warn" class="m-upload-status"
             style="color:var(--yellow);margin-bottom:6px;display:none"></div>
        <div id="m-ig-cooldown-warn" class="m-upload-status"
             style="color:var(--yellow);margin-bottom:6px;display:none"></div>
        <div class="m-upload-row">
          <span class="m-upload-label">Caption</span>
          <textarea id="m-ig-caption" class="m-input" rows="5"></textarea>
        </div>
        <div id="m-ig-status" class="m-upload-status" style="margin-top:4px"></div>
      </div>
      <div class="m-modal-footer">
        <button class="m-btn m-btn-green" id="m-ig-upload-btn"
                onclick="mIgUpload()">▲ Upload</button>
      </div>
    </div>
  </div>
```

- [ ] **Step 2: Add script tag after `<script src="/js/modern.js">` (line 341):**

```html
  <script src="/js/modern_uploads.js"></script>
```

- [ ] **Step 3: Verify — reload, DevTools → Elements confirms 3 modal divs exist with correct IDs. 404 on modern_uploads.js is expected (not created yet).**

---

### Task 3: modern_uploads.js — All upload logic

**Files:**
- Create: `webapp/static/js/modern_uploads.js`

- [ ] **Step 1: Create the file**

```javascript
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
  const poll = setInterval(async () => {
    const s = await api.get(`/api/youtube/upload/${uploadId}`);
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
  await api.post(`/api/jobs/${_mYtJobId}/save-yt-meta`, { title, desc, notes });
  _mYtMetaSaved = true;
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
  const res = await api.post(`/api/jobs/${_mYtJobId}/generate-yt-meta`,
    { project_name: projectName, footer, notes });
  btn.disabled = false;
  if (res?.ok) {
    document.getElementById('m-yt-desc').value = res.description;
    st.textContent = ''; _mYtMetaSaved = false; mYtMetaSave();
  } else {
    st.textContent = '⚠ ' + (res?.error || 'failed'); st.style.color = 'var(--red)';
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
  const res = await api.post(`/api/jobs/${_mYtsJobId}/generate-yt-meta`,
    { project_name: projectName, footer });
  btn.disabled = false;
  if (res?.ok) {
    document.getElementById('m-yts-desc').value = res.description;
    st.textContent = '';
  } else {
    st.textContent = '⚠ ' + (res?.error || 'failed'); st.style.color = 'var(--red)';
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

  const poll = setInterval(async () => {
    const s = await api.get(`/api/ig/upload/${res.upload_id}`);
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
```

- [ ] **Step 2: Verify — reload modern.html, DevTools console: `typeof mYtOpen` → `"function"`, `typeof mIgOpen` → `"function"`, no 404 on modern_uploads.js**

---

### Task 4: Upload buttons in _renderResultsList()

**Files:**
- Modify: `webapp/static/js/modern.js` (lines ~665–735)

- [ ] **Step 1: Add `hasMainYt` check**

Find in `_renderResultsList()`:

```javascript
  const active = _resultsTab === 'shorts' ? shorts : highlights;
  const total  = entries.length;
  if (meta) meta.textContent = `${total} file${total !== 1 ? 's' : ''}`;
```

Replace with:

```javascript
  const active = _resultsTab === 'shorts' ? shorts : highlights;
  const total  = entries.length;
  const hasMainYt = highlights.some(([, i]) => i.yt_url);
  if (meta) meta.textContent = `${total} file${total !== 1 ? 's' : ''}`;
```

- [ ] **Step 2: Insert rfActions build block after dlLink.onclick**

Find in `active.forEach`:

```javascript
    dlLink.onclick = e => e.stopPropagation();

    row.appendChild(rfName);
```

Replace with:

```javascript
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

      if (info.is_ncs) {
        const igBtn = document.createElement('button');
        igBtn.className = 'm-rf-ig' + (info.ig_url ? ' linked' : '');
        igBtn.textContent = info.ig_url ? '✓ IG' : '▲ IG';
        igBtn.title = info.ig_url ? 'Uploaded to Instagram' : 'Upload Reel to Instagram';
        igBtn.onclick = e => { e.stopPropagation(); mIgOpen(filePath, name, info.ncs_attr || null, _jobId); };
        rfActions.appendChild(igBtn);
      }
    }

    row.appendChild(rfName);
```

- [ ] **Step 3: Append rfActions to row**

Find:

```javascript
    row.appendChild(rfMeta);
    row.appendChild(dlLink);
    list.appendChild(row);
```

Replace with:

```javascript
    row.appendChild(rfMeta);
    row.appendChild(dlLink);
    row.appendChild(rfActions);
    list.appendChild(row);
```

- [ ] **Step 4: Verify upload buttons render**

1. Reload `https://<host>/modern.html`
2. Open a project with rendered files → click Results
3. Highlight tab: each row shows `▲ YT` button
4. Shorts tab: `▲ YT` present; disabled if no main has yt_url; `▲ IG` for is_ncs=true shorts
5. Already-uploaded: green `✓ YT` / purple `✓ IG`
6. DevTools console: no JS errors

- [ ] **Step 5: Verify YT main upload end-to-end**

1. Click `▲ YT` on highlight → modal opens, title/desc/notes pre-filled from saved config or project name
2. Privacy = Unlisted default, playlist dropdown loads
3. `✦ Generate` → desc updated by Claude
4. `✦ AI Chapters` → chapter block prepended
5. Title/desc/notes save on blur (network: POST `/api/jobs/{id}/save-yt-meta`)
6. `▲ Upload` → `Uploading… X%` progress
7. Done → `✓ <url>`, results refreshes, button changes to `✓ YT`
8. Existing URL: paste → Save → `✓ Linked`, Clear → field empties

- [ ] **Step 6: Verify YT Shorts upload end-to-end**

1. Short with no uploaded main → modal opens with warning, `▲ Upload` disabled
2. Short with uploaded main → desc has `Full video: <url>`, privacy = Public
3. If >1 main has yt_url → full video dropdown visible; changing selection updates desc link
4. Upload completes → results refreshes, button → `✓ YT`

- [ ] **Step 7: Verify IG Reel upload end-to-end**

1. `▲ IG` on NCS short → modal opens, caption has `Music: <attr> (NCS Release)\n\n<hashtags>`
2. Token expiry warning if `days_until_expiry ≤ 5`
3. Cooldown warning + disabled button if `!status.ready`
4. Upload completes → `✓ <ig-url>`, results refreshes

---

## Notes

- Static files served by nginx no-cache — changes visible immediately on reload, no server restart needed.
- `loadResults()` is exposed as `window.loadResults` in modern.js — safely callable from modern_uploads.js.
- `_jobId` and `_workDir` are module-level vars in modern.js, accessible within `_renderResultsList()`.
- Script load order in modern.html: `modern_music.js` → `modern_analyze.js` → `modern_shorts.js` → `modern.js` → `modern_uploads.js`.
