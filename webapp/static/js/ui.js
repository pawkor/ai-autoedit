function setLang(lang) {
  if (!TRANS[lang]) return;
  currentLang = lang;
  localStorage.setItem('lang', lang);
  _saveUiPrefs();
  const t = TRANS[lang];
  // Tooltips
  document.querySelectorAll('[data-i18n-tip]').forEach(el => {
    el.dataset.tip = t.tips[el.dataset.i18nTip] || '';
  });
  // Text labels (innerHTML for keys that contain HTML like welcome_hint)
  document.querySelectorAll('[data-i18n]').forEach(el => {
    const txt = t.labels[el.dataset.i18n];
    if (txt === undefined) return;
    if (txt.includes('<')) el.innerHTML = txt; else el.textContent = txt;
  });
  // Placeholders
  document.querySelectorAll('[data-i18n-ph]').forEach(el => {
    const txt = t.placeholders?.[el.dataset.i18nPh];
    if (txt !== undefined) el.placeholder = txt;
  });
  // Sort button (dynamic state)
  const sortBtn = document.getElementById('sidebar-sort');
  if (sortBtn) sortBtn.textContent = t.labels[jobSortNewest ? 'misc.newest' : 'misc.oldest'];
  const langBtn = document.getElementById('sidebar-lang');
  if (langBtn) langBtn.textContent = lang;
}

function toggleLang() {
  const langs = Object.keys(TRANS);
  setLang(langs[(langs.indexOf(currentLang) + 1) % langs.length]);
}

function _saveUiPrefs() {
  api.put('/api/settings', { theme: currentTheme, lang: currentLang, sort_newest: jobSortNewest ? 'true' : 'false' });
}

const api = {
  async get(u)    { try { const r=await fetch(u); return r.ok?r.json():null; } catch{return null;} },
  async post(u,b) { const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}); return r.json(); },
  async patch(u,b) { try { const r=await fetch(u,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}); return r.ok?r.json():null; } catch{return null;} },
  async put(u,b)  { const r=await fetch(u,{method:'PUT', headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}); return r.json(); },
  async del(u)    { const r=await fetch(u,{method:'DELETE'}); return r.json(); },
};

let currentJobId=null, jobWs=null, statsWs=null, elapsedTimer=null;
let browsePath=null, framesData=[], manualOverrides={};
let jobSortNewest = localStorage.getItem('jobSortNewest') !== 'false';
let currentJobMaxScene=null, currentJobPerFile=null, currentJobMinTake=0.5;
let _overridesChangedSinceRender = false;
let _avgBackCamTakeSec = null; // avg back-cam clip take (pre-capped), loaded with /frames
let _perFileCuts = new Set();
let _balancedScenes = null; // Set<scene> when dual-cam active, null = single-cam / not needed
let _gapExcluded = new Set();
let _ytFilePath = null;
let _refreshing=false;
let musicTracks=[], musicSelected=new Set();
let _acrConfigured = false;
let jobPhase=null, analyzeResult=null, pinnedTrack=null, _sdTotal=0, _shortsCount=1;
let _suggestedTrack=null, _rerollIdx=0;
const _frameCache = new Map(); // original url → blob URL
let _galleryDirty = true;
let _musicSort = { key: null, asc: true };
let _galleryThreshold = null; // user-set threshold; null = not yet loaded
let _autoFillOverrides = new Set(); // scenes force-included by autoTargetThreshold fill
let _thresholdSaveTimer = null;
let _targetAbortController = null; // AbortController for find-threshold request
let _targetSearchActive = false;   // true while binary search is running
let _pendingTargetMin = null;      // deferred autoTargetThreshold arg (gallery not yet visible)

function _setGallerySearchStatus(iter, total) {
  // Render inline progress bar + label into gallery-stats-text.
  // iter=null → reset to plain '—'; iter=0 → "scanning…"; iter>0 → "N/M"
  const el = document.getElementById('gallery-stats-text');
  if (!el) return;
  if (iter === null) { el.textContent = '—'; return; }
  const pct   = total > 0 ? Math.round(iter / total * 100) : 0;
  const label = iter > 0 ? `${iter}/${total}` : 'scanning…';
  el.innerHTML =
    `<span style="display:inline-flex;align-items:center;gap:6px;width:100%">` +
    `<span style="flex:1;height:4px;border-radius:2px;overflow:hidden;background:var(--bg3);display:inline-block;vertical-align:middle">` +
    `<span style="display:block;height:100%;width:${pct}%;background:var(--accent);border-radius:2px;transition:width .25s ease"></span>` +
    `</span>` +
    `<span style="font-size:10px;color:var(--muted);white-space:nowrap">${label}</span>` +
    `</span>`;
}
let _renderTotalSec = null;
let _renderStepNum = 0;
let _renderStepName = '';
let _audioPlayer = null, _playingFile = null, _seekTimer = null;

function _stopAudio() {
  if (_audioPlayer) { _audioPlayer.pause(); _audioPlayer.src = ''; }
  _playingFile = null;
  if (_seekTimer) { clearInterval(_seekTimer); _seekTimer = null; }
  document.querySelectorAll('.mt.mt-playing').forEach(row => row.classList.remove('mt-playing'));
  document.querySelectorAll('.mt-play').forEach(b => b.textContent = '▶');
  document.querySelectorAll('.mt-seek').forEach(s => { s.value = 0; });
}

function _playTrack(file, btn, seek) {
  if (_playingFile === file) { _stopAudio(); return; }
  _stopAudio();
  _playingFile = file;
  btn.closest('.mt').classList.add('mt-playing');
  btn.textContent = '■';
  _audioPlayer = new Audio('/api/file?path=' + encodeURIComponent(file));
  _audioPlayer.addEventListener('loadedmetadata', () => { seek.max = Math.floor(_audioPlayer.duration); });
  _audioPlayer.addEventListener('ended', _stopAudio);
  _audioPlayer.play().catch(() => {});
  _seekTimer = setInterval(() => {
    if (_audioPlayer && !_audioPlayer.paused) seek.value = Math.floor(_audioPlayer.currentTime);
  }, 300);
  seek.oninput = () => { if (_audioPlayer) _audioPlayer.currentTime = +seek.value; };
}

// Strip BROWSE_ROOT prefix so paths show as  moto/2025/...  not  /data/moto/2025/...
let _browseRoot = '';
fetch('/api/config').then(r=>r.ok?r.json():null).then(cfg=>{
  if (!cfg) return;
  _browseRoot = cfg.browse_root || '';
  if (!cfg.data_root_configured) _showDataRootModal();
}).catch(()=>{});
function trimPath(p) {
  if (!p) return p || '';
  if (_browseRoot && p.startsWith(_browseRoot + '/')) return p.slice(_browseRoot.length + 1);
  return p.replace(/^\/home\/[^/]+\//, '');
}
function _esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ── Views ──────────────────────────────────────────────────────────────────────
function showView(id) { document.querySelectorAll('.view').forEach(v=>v.classList.remove('active')); document.getElementById(id).classList.add('active'); }
function showWelcome() { showView('welcome'); }

// ── Sidebar ───────────────────────────────────────────────────────────────────
function toggleSortOrder() {
  jobSortNewest = !jobSortNewest;
  localStorage.setItem('jobSortNewest', jobSortNewest);
  _saveUiPrefs();
  const t = TRANS[currentLang]?.labels;
  document.getElementById('sidebar-sort').textContent = t?.[jobSortNewest ? 'misc.newest' : 'misc.oldest'] || (jobSortNewest ? 'newest' : 'oldest');
  refreshJobList();
}

const THEMES = ['dark', 'light'];
let currentTheme = localStorage.getItem('theme') || 'dark';
// Coerce legacy theme names to dark
if (!THEMES.includes(currentTheme)) currentTheme = 'dark';

let _logFilter = localStorage.getItem('logFilter') || 'info';
function setLogFilter(f) {
  _logFilter = f;
  localStorage.setItem('logFilter', f);
  const panel = document.getElementById('log-panel');
  if (!panel.classList.contains('lf-' + f)) {
    panel.classList.remove('lf-steps', 'lf-info', 'lf-all');
    panel.classList.add('lf-' + f);
  }
  const label = {steps: 'Steps', info: 'Info', all: 'All'}[f] || f;
  const trigger = document.getElementById('log-filter-trigger');
  if (trigger) trigger.textContent = label + ' ▾';
  document.querySelectorAll('.lfm-item').forEach(el => el.classList.toggle('active', el.dataset.lf === f));
  document.getElementById('log-filter-menu')?.classList.remove('open');
}
function _toggleLogFilterMenu(e) {
  e.stopPropagation();
  document.getElementById('log-filter-menu').classList.toggle('open');
}
document.addEventListener('click', () => document.getElementById('log-filter-menu')?.classList.remove('open'));

function applyTheme(name) {
  if (!THEMES.includes(name)) name = 'dark';
  currentTheme = name;
  document.documentElement.classList.toggle('theme-light', name === 'light');
  const btn = document.getElementById('sidebar-theme');
  if (btn) btn.textContent = name === 'dark' ? '☽' : '☀';
  const sel = document.getElementById('s-theme');
  if (sel) sel.value = name;
  localStorage.setItem('theme', name);
  _saveUiPrefs();
}
function toggleTheme() {
  applyTheme(currentTheme === 'dark' ? 'light' : 'dark');
}

async function refreshJobList() {
  if (_refreshing) return;
  _refreshing = true;
  try {
    let jobs = await api.get('/api/jobs') || [];
    jobs = [...jobs].sort((a, b) => {
      const ka = a.work_dir || '', kb = b.work_dir || '';
      return jobSortNewest ? ka.localeCompare(kb) : kb.localeCompare(ka);
    });
    const list = document.getElementById('job-list');
    // Build new HTML string first — only touch DOM if content changed
    const rows = jobs.map(j => {
      const dir = trimPath(j.work_dir);
      const dur = j.ended_at ? formatDur(j.ended_at - j.started_at) : '';
      const statusLabel = j.status === 'idle' ? 'new' : j.status;
      const active = j.id === currentJobId ? ' active' : '';
      return `<div class="job-item${active}" data-id="${j.id}">` +
        `<div class="ji-dir"><span class="dot s-${j.status}"></span><span class="ji-dir-txt">${_esc(dir)}</span></div>` +
        `<div class="ji-meta">${statusLabel}${dur?' · '+dur:''}</div>` +
        `<button class="ji-del" title="Remove job" onclick="event.stopPropagation();removeJob('${j.id}')">×</button>` +
        `</div>`;
    }).join('');
    if (list.dataset.lastHtml !== rows) {
      list.innerHTML = rows;
      list.dataset.lastHtml = rows;
      list.querySelectorAll('.job-item').forEach((div, i) => {
        div.onclick = () => openJob(jobs[i].id);
      });
    }
  } finally {
    _refreshing = false;
  }
}
setInterval(refreshJobList, 3000);
refreshJobList();
setLang(currentLang);
applyTheme(currentTheme);
setLogFilter(_logFilter);
// Sync UI prefs from server (authoritative across all devices)
api.get('/api/settings').then(s => {
  if (!s) return;
  if (s.theme && s.theme !== currentTheme) { applyTheme(s.theme); localStorage.setItem('theme', s.theme); }
  if (s.lang  && s.lang  !== currentLang)  { setLang(s.lang);     localStorage.setItem('lang', s.lang); }
  if (s.sort_newest !== '') {
    const newest = s.sort_newest === 'true';
    if (newest !== jobSortNewest) {
      jobSortNewest = newest;
      localStorage.setItem('jobSortNewest', jobSortNewest);
      const t = TRANS[currentLang]?.labels;
      const btn = document.getElementById('sidebar-sort');
      if (btn) btn.textContent = t?.[jobSortNewest ? 'misc.newest' : 'misc.oldest'] || (jobSortNewest ? 'newest' : 'oldest');
    }
  }
});

// Hide S3 UI rows if S3 is not configured
(async () => {
  const s3 = await api.get('/api/s3/status').catch(() => null);
  if (!s3?.configured) {
    document.getElementById('music-s3-row').style.display = 'none';
    // Results S3 buttons are created dynamically — handled in s3ModalOpen guard
  }
})();

// ── Stats WebSocket ───────────────────────────────────────────────────────────
function connectStats() {
  if (statsWs) return;
  const proto = location.protocol==='https:'?'wss':'ws';
  statsWs = new WebSocket(`${proto}://${location.host}/ws/stats`);
  statsWs.onmessage = e => updateStats(JSON.parse(e.data));
  statsWs.onclose = () => { statsWs=null; setTimeout(connectStats, 3000); };
}
connectStats();

function switchTab(name) {
  // Settings and summary no longer have center tabs — redirect to gallery.
  if (name === 'settings' || name === 'summary') name = 'gallery';
  if (name !== 'results') stopVideo();
  if (name !== 'music') _stopAudio();
  document.querySelectorAll('.tab').forEach((t,i) => t.classList.toggle('active', TAB_NAMES[i] === name));
  document.getElementById('log-split').style.display          = name==='log'      ? 'flex' : 'none';
  document.getElementById('log-filter-widget').style.display  = name==='log'      ? ''     : 'none';
  document.getElementById('btn-clear-log').style.display      = name==='log'      ? ''     : 'none';
  document.getElementById('gallery-panel').classList.toggle('active', name==='gallery');
  document.getElementById('music-panel').classList.toggle('active', name==='music');
  document.getElementById('results-panel').classList.toggle('active', name==='results');
  if (name==='gallery' && currentJobId) {
    if (_galleryThreshold !== null)
      document.getElementById('threshold-val').value = _galleryThreshold.toFixed(3);
    _syncThresholdDisplay();
    if (!framesData.length) {
      loadFrames(currentJobId);
    } else if (_pendingTargetMin && !_targetSearchActive) {
      // Binary search was deferred while gallery was hidden — run it now.
      const t = _pendingTargetMin; _pendingTargetMin = null;
      autoTargetThreshold(t);
    } else if (_targetSearchActive) {
      // Binary search already running (e.g. triggered by setTargetFromSelectedTrack)
      // — just ensure progress indicator is visible.
      _setGallerySearchStatus(0, 12);
      if (_galleryDirty) renderGallery();
    } else if (_galleryDirty) {
      renderGallery(); calculateGalleryStats();
    }
  }
  if (name==='music' && currentJobId) {
    _musicSort = { key: null, asc: true };  // always re-sort by estimated duration on tab switch
    const _doMusic = () => { if (!musicTracks.length) loadMusicTracks(); else renderMusicList(); };
    if (!analyzeResult?.estimated_duration_sec)
      loadAnalyzeResult(currentJobId).then(_doMusic);
    else
      _doMusic();
  }
  if (name==='results' && currentJobId) loadResults(currentJobId);
  // Footer always visible — update track display when gallery opens.
  if (name==='gallery' && currentJobId) {
    if (!analyzeResult?.estimated_duration_sec)
      loadAnalyzeResult(currentJobId).then(() => _updateSummaryTrack());
    else
      _updateSummaryTrack();
  }
}

// ── Gallery ───────────────────────────────────────────────────────────────────
async function loadFrames(jobId) {
  const [data, job] = await Promise.all([
    api.get(`/api/jobs/${jobId}/frames`),
    api.get(`/api/jobs/${jobId}`),
  ]);
  const frames = data?.frames ?? data;  // handle {frames,back_cam} or legacy array
  if (!frames?.length) return;
  // Guard: ignore stale response if user switched to another job
  if (currentJobId !== jobId) return;
  framesData = frames;
  _avgBackCamTakeSec = data?.back_cam?.avg_take_sec ?? null;
  // Ensure caps are set from job params (guards against populateJobSettings race)
  if (job?.params?.max_scene != null) currentJobMaxScene = parseFloat(job.params.max_scene);
  if (job?.params?.per_file  != null) currentJobPerFile  = parseFloat(job.params.per_file);
  if (job?.params?.min_take  != null) currentJobMinTake  = parseFloat(job.params.min_take);
  await loadOverrides();
  _sdTotal = 0;
  document.getElementById('sd-progress-wrap').style.display = 'none';
  document.getElementById('gallery-stats-text').style.display = '';
  document.getElementById('gallery-stats-text').textContent = '—';

  const threshold = job?.params?.threshold;
  const targetMin = job?.params?.target_minutes ? parseFloat(job.params.target_minutes) : null;
  const galleryNowActive = document.getElementById('gallery-panel').classList.contains('active');
  if (targetMin && targetMin > 0) {
    // Apply saved threshold immediately for instant display, then refine via binary search.
    if (threshold) {
      _galleryThreshold = parseFloat(threshold);
      document.getElementById('threshold-val').value = _galleryThreshold.toFixed(3);
      _syncThresholdDisplay(); // computes _computeGapExclusions() before first renderGallery()
    }
    if (galleryNowActive) {
      _pendingTargetMin = null; // clear any deferred request — running now
      // Only fire if no search is running AND none has completed yet for this job.
      // Prevents WS done/analyzed events from re-running an already-finished search.
      if (!_targetSearchActive && !analyzeResult?.auto_threshold) autoTargetThreshold(targetMin);
    } else {
      // Gallery not visible — defer binary search until user opens the tab.
      _pendingTargetMin = targetMin;
    }
  } else if (threshold) {
    _galleryThreshold = parseFloat(threshold);
    document.getElementById('threshold-val').value = _galleryThreshold.toFixed(3);
    _syncThresholdDisplay();
  }
  _galleryDirty = true;
  const galleryActive = document.getElementById('gallery-panel').classList.contains('active');
  if (galleryActive) {
    renderGallery();
    calculateGalleryStats();
  } else {
    _syncThresholdDisplay();
  }
  _checkAnalyzeMode(job);
}

function _checkAnalyzeMode(job) {
  const warn = document.getElementById('analyze-warn');
  if (!warn) return;
  const clipFirst = document.getElementById('js-clip-first')?.checked;
  const hasClipScenes = framesData.some(f => /-(clip)-\d+/.test(f.scene));
  const hasSceneScenes = framesData.some(f => /-(scene)-\d+/.test(f.scene));
  let msg = '';
  if (clipFirst && hasSceneScenes && !hasClipScenes) {
    msg = '⚠ Re-analyze needed — CLIP-first not used';
  } else if (!clipFirst && hasClipScenes && !hasSceneScenes) {
    msg = '⚠ Re-analyze needed — CLIP-first was used, now disabled';
  }
  warn.textContent = msg;
  warn.style.display = msg ? '' : 'none';
}

// ── Queue modal ───────────────────────────────────────────────────────────────

async function openQueueModal() {
  document.getElementById('queue-modal').classList.add('open');
  await _refreshQueueModal();
}

function closeQueueModal() {
  document.getElementById('queue-modal').classList.remove('open');
}

async function _refreshQueueModal() {
  const body = document.getElementById('queue-modal-body');
  const data = await api.get('/api/queue');
  if (!data) { body.innerHTML = '<div style="color:var(--muted);font-size:12px">Failed to load queue.</div>'; return; }

  const { running, queued } = data;
  body.innerHTML = '';

  if (!running.length && !queued.length) {
    body.innerHTML = '<div style="color:var(--muted);font-size:12px">No running or queued jobs.</div>';
    return;
  }

  if (running.length) {
    const sec = document.createElement('div');
    sec.innerHTML = '<div class="qm-section-label">Running</div>';
    running.forEach(j => sec.appendChild(_queueRow(j, false)));
    body.appendChild(sec);
  }

  if (queued.length) {
    const sec = document.createElement('div');
    sec.innerHTML = '<div class="qm-section-label">Queued</div>';
    queued.forEach(j => sec.appendChild(_queueRow(j, true)));
    body.appendChild(sec);
  }
}

function _queueRow(j, canDequeue) {
  const row = document.createElement('div');
  row.className = 'qm-row';
  row.dataset.jobId = j.id;

  const name = document.createElement('div');
  name.className = 'qm-name';
  name.textContent = trimPath(j.work_dir);
  row.appendChild(name);

  const phase = document.createElement('span');
  phase.className = 'qm-phase';
  phase.textContent = j.phase || '';
  row.appendChild(phase);

  if (!canDequeue && j.started_at) {
    const bar = document.createElement('div');
    bar.className = 'qm-bar-track';
    bar.innerHTML = '<div class="qm-bar"></div>';
    row.appendChild(bar);
    // Pulse animation — can't know real progress without WS, show indeterminate bar
    bar.querySelector('.qm-bar').style.width = '60%';
  }

  if (canDequeue) {
    const btn = document.createElement('button');
    btn.className = 'qm-dequeue-btn';
    btn.textContent = '✕';
    btn.title = 'Remove from queue';
    btn.onclick = async (e) => {
      e.stopPropagation();
      await api.del(`/api/jobs/${j.id}/dequeue`);
      await _refreshQueueModal();
    };
    row.appendChild(btn);
  }

  row.onclick = () => { openJob(j.id); closeQueueModal(); };
  return row;
}

let _confirmResolve = null;
function showConfirm(heading, msg, files, okLabel = 'Delete') {
  return new Promise(resolve => {
    _confirmResolve = resolve;
    document.getElementById('confirm-heading').textContent = heading;
    document.getElementById('confirm-msg').textContent = msg;
    const fd = document.getElementById('confirm-files');
    if (files) { fd.textContent = files; fd.style.display = ''; }
    else fd.style.display = 'none';
    document.getElementById('btn-confirm-ok').textContent = okLabel;
    document.getElementById('confirm-modal').classList.add('open');
  });
}
function _confirmOk()     { document.getElementById('confirm-modal').classList.remove('open'); _confirmResolve?.(true);  _confirmResolve = null; }
function _confirmCancel() { document.getElementById('confirm-modal').classList.remove('open'); _confirmResolve?.(false); _confirmResolve = null; }

// ── Files browser modal ────────────────────────────────────────────────────────
let _fileBrowserPath = '';

function closeSettings() { document.getElementById('settings-modal').classList.remove('open'); }
async function saveSettings() {
  const data = {
    max_concurrent_jobs:  parseInt(document.getElementById('s-max-jobs').value),
    max_detect_workers:   parseInt(document.getElementById('s-max-detect').value),
    clip_batch_size:      parseInt(document.getElementById('s-clip-batch').value),
    clip_workers:         parseInt(document.getElementById('s-clip-workers').value),
  };
  await api.put('/api/settings', data);
  closeSettings();
}

// ── YouTube ───────────────────────────────────────────────────────────────────
async function ytCheckStatus() {
  const data = await api.get('/api/youtube/status');
  const dot          = document.getElementById('yt-status-dot');
  const btnConnect   = document.getElementById('btn-yt-connect');
  const btnDisconnect= document.getElementById('btn-yt-disconnect');
  const hint         = document.getElementById('yt-secrets-hint');
  if (!data) return;
  if (!data.has_secrets) {
    dot.textContent = 'no credentials file';
    dot.style.color = 'var(--red)';
    hint.style.display = '';
    btnConnect.style.display = 'none';
    btnDisconnect.style.display = 'none';
  } else if (data.authenticated) {
    dot.textContent = '● connected';
    dot.style.color = 'var(--green)';
    hint.style.display = 'none';
    btnConnect.style.display = 'none';
    btnDisconnect.style.display = '';
  } else {
    dot.textContent = '○ not connected';
    dot.style.color = 'var(--muted)';
    hint.style.display = 'none';
    btnConnect.style.display = '';
    btnDisconnect.style.display = 'none';
  }
}

async function ytConnect() {
  const win = window.open('', 'yt-auth', 'width=620,height=720');
  const data = await api.get(`/api/youtube/auth?origin=${encodeURIComponent(window.location.origin)}`);
  if (!data?.url) { win.close(); alert('Failed to get auth URL — is youtube_client_secrets.json in place?'); return; }
  win.location.href = data.url;
  let winClosedAt = null;
  const timer = setInterval(async () => {
    if (win.closed && !winClosedAt) winClosedAt = Date.now();
    const data = await api.get('/api/youtube/status');
    if (data?.authenticated) {
      clearInterval(timer);
      ytCheckStatus();
    } else if (winClosedAt && Date.now() - winClosedAt > 5000) {
      clearInterval(timer);
    }
  }, 800);
}

async function ytDisconnect() {
  if (!await showConfirm('Disconnect YouTube', 'Disconnect YouTube account?', null, 'Disconnect')) return;
  await fetch('/api/youtube/disconnect', { method: 'DELETE' });
  ytCheckStatus();
}

async function ytLoadPlaylists(selId = 'yt-playlist') {
  const sel = document.getElementById(selId);
  sel.innerHTML = '<option value="">— no playlist —</option>';
  const data = await api.get('/api/youtube/playlists');
  if (!data?.length) return;
  for (const p of data) {
    const o = document.createElement('option');
    o.value = p.id;
    o.textContent = p.title;
    sel.appendChild(o);
  }
}

function _applyTraditionalMode(on) {
  const ids = ['traditional-scene-params', 'threshold-bar', 'js-no-music-label', 'btn-render'];
  ids.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.style.display = on ? '' : 'none';
  });
  const cb = document.getElementById('js-traditional-mode');
  if (cb) cb.checked = !!on;
  localStorage.setItem('traditional_mode', on ? '1' : '0');
}

// Init traditional mode from localStorage
_applyTraditionalMode(localStorage.getItem('traditional_mode') === '1');

function formatDur(sec) {
  const s=Math.floor(sec), m=Math.floor(s/60);
  return m>0?`${m}m${s%60}s`:`${s}s`;
}
function fmtDur(sec, prefix='') {
  const d = Math.round(sec);
  return `${prefix}${Math.floor(d/60)}:${String(d%60).padStart(2,'0')}`;
}
