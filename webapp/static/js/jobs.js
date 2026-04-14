function updateStats(s) {
  document.getElementById('hw-cpu-label').textContent = `CPU ${s.cpu_pct}%`;
  document.getElementById('hw-ram-label').textContent = `RAM ${s.ram_used_gb}/${s.ram_total_gb}G`;
  document.getElementById('bar-cpu').style.width = Math.min(s.cpu_pct, 100) + '%';
  document.getElementById('bar-ram').style.width = Math.min(s.ram_pct, 100) + '%';

  const gpuCol  = document.getElementById('hw-gpu-col');
  const vramCol = document.getElementById('hw-vram-col');
  if (s.gpu) {
    const vused = (s.gpu.vram_used_mb/1024).toFixed(1);
    const vtot  = (s.gpu.vram_total_mb/1024).toFixed(1);
    document.getElementById('hw-gpu-label').textContent  = `GPU ${s.gpu.pct}%`;
    document.getElementById('hw-vram-label').textContent = `VRAM ${vused}/${vtot}G`;
    document.getElementById('bar-gpu').style.width  = Math.min(s.gpu.pct, 100) + '%';
    document.getElementById('bar-vram').style.width = Math.min(s.gpu.vram_pct, 100) + '%';
    gpuCol.style.display  = '';
    vramCol.style.display = '';
  } else {
    gpuCol.style.display  = 'none';
    vramCol.style.display = 'none';
  }
  document.getElementById('hw-monitor').style.display = '';

  const qRunning = s.running_jobs, qQueued = s.queued_jobs;
  const sqEl = document.getElementById('sidebar-queue');
  if (sqEl) {
    document.getElementById('sq-running').textContent = qRunning;
    document.getElementById('sq-queued').textContent  = qQueued;
    sqEl.style.display = (qRunning > 0 || qQueued > 0) ? '' : 'none';
    sqEl.classList.toggle('sq-active', qRunning > 0);
  }
}

// ── Prompts ───────────────────────────────────────────────────────────────────
async function generateJobPrompts() {
  if (!currentJobId) return;
  const desc = document.getElementById('js-description').value.trim();
  if (!desc) { alert('Enter a description first'); return; }
  const job = await api.get(`/api/jobs/${currentJobId}`);
  if (!job) return;
  const btn = document.getElementById('btn-gen-prompts');
  const status = document.getElementById('gen-prompts-status');
  btn.disabled = true;
  status.textContent = 'Generating…';
  const resp = await api.post('/api/about', { description: desc, work_dir: job.params.work_dir });
  btn.disabled = false;
  if (resp?.ok) {
    if (resp.positive) document.getElementById('js-positive').value = resp.positive;
    if (resp.negative) document.getElementById('js-negative').value = resp.negative;
    status.textContent = 'Done — review and Save';
    status.style.color = 'var(--green)';
  } else {
    status.textContent = 'Error: ' + (resp?.output || 'unknown');
    status.style.color = 'var(--red)';
  }
  setTimeout(() => { status.textContent = ''; status.style.color = ''; }, 5000);
}

async function saveJobPrompts() {
  if (!currentJobId) return;
  const description = document.getElementById('js-description').value.trim();
  const positive    = document.getElementById('js-positive').value.trim();
  const negative    = document.getElementById('js-negative').value.trim();
  const resp = await api.post(`/api/jobs/${currentJobId}/save-prompts`,
                               { description, positive, negative });
  const status = document.getElementById('gen-prompts-status');
  status.textContent = resp?.ok ? 'Saved' : 'Save failed';
  status.style.color = resp?.ok ? 'var(--green)' : 'var(--red)';
  setTimeout(() => { status.textContent = ''; status.style.color = ''; }, 3000);
}

// ── Open job ──────────────────────────────────────────────────────────────────
async function openJob(jobId) {
  _closeJobWs();
  if (elapsedTimer) { clearInterval(elapsedTimer); elapsedTimer=null; }
  if (_proxyPollTimer) { clearInterval(_proxyPollTimer); _proxyPollTimer=null; }
  currentJobId = jobId;
  jobPhase = null;
  analyzeResult = null;
  pinnedTrack = null;
  _suggestedTrack = null; _rerollIdx = 0;
  _shortsCount = 1;
  framesData = [];
  manualOverrides = {};
  _autoFillOverrides.clear();
  _galleryDirty = true;
  _galleryThreshold = null;
  _pendingTargetMin = null;
  currentJobMaxScene = null;
  currentJobPerFile  = null;
  currentJobMinTake  = 0.5;
  _perFileCuts = new Set();
  _balancedScenes = null;
  _avgBackCamTakeSec = null;
  _filterScore = ''; _filterTime = '';
  const _fsi = document.getElementById('filter-score'); if (_fsi) _fsi.value = '';
  const _fti = document.getElementById('filter-time');  if (_fti) _fti.value = '';
  musicTracks = [];
  musicSelected = new Set();
  document.getElementById('js-cam-list').innerHTML = '';
  document.getElementById('music-list').innerHTML = '';
  document.getElementById('music-count').textContent = '—';
  _sdTotal = 0;
  document.getElementById('sd-progress-wrap').style.display = 'none';
  _updateProxyUI({not_started: true});
  document.getElementById('gallery-stats-text').style.display = '';
  document.getElementById('gallery-stats-text').textContent = '—';
  document.getElementById('js-description').value = '';
  document.getElementById('js-positive').value = '';
  document.getElementById('js-negative').value = '';
  document.getElementById('sum-track').textContent = t_('misc.no_pin') || 'No track pinned — will auto-select.';
  document.getElementById('render-progress-wrap').style.display = 'none';
  document.getElementById('render-progress-bar').style.width = '0%';
  showView('job-view');
  stopVideo();
  document.getElementById('log-panel').innerHTML = '';
  document.getElementById('frames-grid').innerHTML = '';
  document.getElementById('rf-files-main').innerHTML = '';
  document.getElementById('rf-files-short').innerHTML = '';
  document.getElementById('btn-kill').style.display = 'none';
  document.getElementById('btn-kill-log').style.display = 'none';
  document.getElementById('jh-path').textContent = '…';
  document.getElementById('jh-status').textContent = '';
  document.getElementById('jh-elapsed').textContent = '';
  resetSteps();

  const job = await api.get(`/api/jobs/${jobId}`);
  if (!job) return;

  jobPhase = job.phase || 'done';
  document.getElementById('jh-path').textContent = trimPath(job.params.work_dir);
  setStatusDot(job.status);
  currentJobMaxScene = job.params.max_scene != null ? parseFloat(job.params.max_scene) : null;
  currentJobPerFile  = job.params.per_file  != null ? parseFloat(job.params.per_file)  : null;
  _overridesChangedSinceRender = false;
  pinnedTrack = null;
  await populateJobSettings(job.params);
  updatePhaseUI();

  // Tab selection on open — gallery is the default center view.
  if (job.status === 'running' || job.status === 'queued') {
    switchTab('log');
  } else {
    switchTab('gallery');
  }

  if (job.status === 'running') {
    document.getElementById('btn-kill').style.display = '';
  document.getElementById('btn-kill-log').style.display = '';
    startElapsedTimer(job.started_at);
  } else if (job.status === 'queued') {
    document.getElementById('jh-status').textContent = 'queued';
  } else if (job.ended_at) {
    document.getElementById('jh-elapsed').textContent = formatDur(job.ended_at - job.started_at);
  }

  // Load log from dedicated .log file (not from JSON)
  api.get(`/api/jobs/${jobId}/log`).then(data => {
    if (!data?.lines?.length) return;
    const panel = document.getElementById('log-panel');
    const frag = document.createDocumentFragment();
    for (const line of data.lines) {
      const div = document.createElement('div');
      div.className = 'log-line';
      const kind = _classifyLogLine(div, line);
      if (kind === 'step') activateStep(parseInt(line.match(/^\[(\d+[a-z]?)/)[1]));
      frag.appendChild(div);
    }
    panel.appendChild(frag);
    panel.scrollTop = panel.scrollHeight;
  });
  // Restore shorts button state if shorts are already running (e.g. after page reload)
  if (job.shorts_running) {
    const btnS = document.getElementById('btn-render-short');
    if (btnS) { btnS.disabled = true; btnS.textContent = 'Generating…'; }
    const wrap = document.getElementById('shorts-progress-wrap');
    if (wrap) wrap.style.display = '';
    const slbl = document.getElementById('shorts-status-label');
    if (slbl) slbl.textContent = 'Generating Short…';
  }
  connectJobWs(jobId, job.started_at);

  if (job.status === 'done' || job.status === 'failed' || jobPhase === 'analyzed') {
    // Binary search is deferred until the gallery tab is opened.
    // _pendingTargetMin is consumed by switchTab('gallery') → autoTargetThreshold.
    const tm = parseFloat(job.params.target_minutes);
    if (tm > 0) _pendingTargetMin = tm;
  }
  if (job.status === 'done' && jobPhase === 'done') {
    loadResults(jobId);
  }
  refreshJobList();
  resumeProxyIfRunning();
}

function _closeJobWs() {
  if (!jobWs) return;
  jobWs.onclose = null;  // prevent auto-reconnect from firing on intentional close
  jobWs.close();
  jobWs = null;
}

function connectJobWs(jobId, startedAt) {
  const proto = location.protocol==='https:'?'wss':'ws';
  jobWs = new WebSocket(`${proto}://${location.host}/ws/${jobId}`);
  jobWs.onmessage = e => {
    const msg = JSON.parse(e.data);
    if (msg.type==='log') {
      appendLog(msg.line);
      // Scene detection progress bar
      const sdStart = msg.line.match(/\[2\/6\] Scene detection \((\d+) files\)/);
      if (sdStart) {
        _sdTotal = parseInt(sdStart[1]);
        document.getElementById('gallery-stats-text').style.display = 'none';
        const w = document.getElementById('sd-progress-wrap');
        w.style.display = 'flex';
        document.getElementById('sd-progress-bar').style.width = '0%';
        document.getElementById('sd-progress-pct').textContent = '0%';
      } else if (_sdTotal > 0) {
        if (msg.line.match(/^\s*\[3\/6\]/)) {
          _sdTotal = 0;
          document.getElementById('sd-progress-wrap').style.display = 'none';
          document.getElementById('gallery-stats-text').style.display = '';
        } else {
          const m = msg.line.match(/^\s*\[(\d+)\/(\d+)\]/);
          if (m && parseInt(m[2]) === _sdTotal) {
            const pct = Math.round(parseInt(m[1]) / _sdTotal * 100);
            document.getElementById('sd-progress-bar').style.width = pct + '%';
            document.getElementById('sd-progress-pct').textContent = pct + '%';
          }
        }
      }
      if (jobPhase === 'shorts' && _shortsCount === 1) {
        // Single-short mode: derive progress from make_shorts.py clip log lines "[X/Y]"
        const m = msg.line.match(/^\s*\[\s*(\d+)\/(\d+)\]/);
        if (m) {
          const pct = Math.round(parseInt(m[1]) / parseInt(m[2]) * 100);
          const bar = document.getElementById('shorts-progress-bar');
          const lbl = document.getElementById('shorts-pct');
          if (bar) bar.style.width = pct + '%';
          if (lbl) lbl.textContent = pct + '%';
        }
      }
    } else if (msg.type === 'shorts_status') {
      const btnS = document.getElementById('btn-render-short');
      if (msg.running) {
        if (btnS) { btnS.disabled = true; btnS.textContent = 'Generating…'; }
        const wrap = document.getElementById('shorts-progress-wrap');
        if (wrap) wrap.style.display = '';
      } else {
        if (btnS) {
          btnS.disabled = false;
          _updateShortsBtn();
        }
        const slbl = document.getElementById('shorts-status-label');
        const sbar = document.getElementById('shorts-progress-bar');
        const spct = document.getElementById('shorts-pct');
        if (msg.status === 'done') {
          if (sbar) sbar.style.width = '100%';
          if (spct) spct.textContent = '100%';
          if (slbl) slbl.textContent = '✓ Short ready';
          loadResults(currentJobId);
        } else {
          if (slbl) slbl.textContent = '✗ Failed';
        }
      }
    } else if (msg.type === 'shorts_batch_progress') {
      const bar = document.getElementById('shorts-progress-bar');
      const lbl = document.getElementById('shorts-pct');
      const slbl = document.getElementById('shorts-status-label');
      if (bar)  bar.style.width = msg.pct + '%';
      if (lbl)  lbl.textContent = msg.pct + '%';
      if (slbl) slbl.textContent = `${msg.done} / ${msg.total} done`;
    } else if (msg.type==='status') {
      const prevPhase = jobPhase;
      if (msg.phase) { jobPhase = msg.phase; updatePhaseUI(); }
      setStatusDot(msg.status);
      if (msg.status==='running') {
        document.getElementById('btn-kill').style.display = '';
  document.getElementById('btn-kill-log').style.display = '';
        document.getElementById('jh-status').textContent = '';
        startElapsedTimer(startedAt);
      } else {
        document.getElementById('btn-kill').style.display = 'none';
  document.getElementById('btn-kill-log').style.display = 'none';
        if (elapsedTimer) { clearInterval(elapsedTimer); elapsedTimer=null; }
        if (msg.phase === 'analyzed') {
          loadFrames(jobId);
          loadAnalyzeResult(jobId);
        } else if (msg.status==='done' || msg.status==='failed') {
          loadFrames(jobId);
          loadResults(jobId);
          loadAnalyzeResult(jobId);
          if (msg.phase === 'shorts' || prevPhase === 'shorts') {
            const btnS = document.getElementById('btn-render-short');
            if (btnS) { btnS.disabled = false; _updateShortsBtn(); }
            const sbar = document.getElementById('shorts-progress-bar');
            if (sbar) sbar.style.width = msg.status === 'done' ? '100%' : '0%';
            const slbl = document.getElementById('shorts-status-label');
            if (slbl) slbl.textContent = msg.status === 'done' ? '✓ Short ready' : '✗ Failed';
            const spct = document.getElementById('shorts-pct');
            if (spct) spct.textContent = msg.status === 'done' ? '100%' : '';
          } else {
            const btnR = document.getElementById('btn-render');
            if (btnR) { btnR.disabled = false; btnR.textContent = '▶ Render Highlight'; }
            const btnMD = document.getElementById('btn-render-md');
            if (btnMD) { btnMD.disabled = false; btnMD.textContent = '♪ Music-driven'; }
            const lbl = document.getElementById('render-status-label');
            if (lbl) lbl.textContent = msg.status === 'done' ? '✓ Done' : '✗ Failed';
            const etaEl = document.getElementById('render-eta');
            if (etaEl) etaEl.textContent = '';
            const stepLbl = document.getElementById('render-step-label');
            if (stepLbl) stepLbl.textContent = '';
            if (msg.status === 'done') {
              document.getElementById('render-progress-bar').style.width = '100%';
              document.getElementById('render-pct').textContent = '100%';
            }
          }
        }
        refreshJobList();
      }
    }
  };
  jobWs.onclose = () => {
    jobWs = null;
    // Reconnect if job is still active
    if (currentJobId) {
      setTimeout(() => {
        if (currentJobId && !jobWs) connectJobWs(currentJobId, startedAt);
      }, 2000);
    }
  };
}

// ── Phase UI ─────────────────────────────────────────────────────────────────
function t_(key) { return TRANS[currentLang]?.labels?.[key] || ''; }

function updatePhaseUI() {
  const analyzed = jobPhase === 'analyzed' || jobPhase === 'done' || jobPhase === 'failed';
  const rendering = jobPhase === 'rendering';

  const btnGallery = document.getElementById('btn-gallery-to-music');
  if (btnGallery) btnGallery.style.display = analyzed ? '' : 'none';

  const btnRender = document.getElementById('btn-render');
  if (btnRender) btnRender.disabled = !analyzed || rendering;

  // btn-render-short: only blocked while shorts are running (not by main render)
  // shorts_running state is managed via shorts_status WS messages

  if (rendering) {
    document.getElementById('render-progress-wrap').style.display = '';
  }

  // Tab badges — TAB_NAMES = ['gallery', 'music', 'results', 'log']
  const badges = { gallery: '○', music: '○', results: '○', log: '' };
  if (jobPhase === 'analyzing') badges.gallery = '●';
  if (analyzed) badges.gallery = '✓';
  if (pinnedTrack) badges.music = '✓';
  if (jobPhase === 'done') badges.results = '✓';
  if (btnRender) btnRender.setAttribute('data-rendering', rendering ? '1' : '');

  TAB_NAMES.forEach((name, i) => {
    const el = document.querySelectorAll('.tab')[i];
    if (!el) return;
    const b = badges[name];
    if (b) el.setAttribute('data-badge', b);
    else el.removeAttribute('data-badge');
  });
}

async function loadAnalyzeResult(jobId) {
  const data = await api.get(`/api/jobs/${jobId}/analyze-result`);
  if (!data || currentJobId !== jobId) return;
  analyzeResult = data;
  _overridesChangedSinceRender = false;
  document.getElementById('sum-scene-count').textContent = data.scene_count ?? '—';

  // If actual render results are available, show them directly (no threshold needed).
  // This ensures the Summary tab always reflects the most recent completed render.
  if (data.actual_selected_scenes != null && data.actual_duration_sec != null) {
    const durStr = fmtDur(data.actual_duration_sec);
    document.getElementById('sum-duration').textContent = durStr;
    document.getElementById('sum-scene-selected').textContent = data.actual_selected_scenes;
    document.getElementById('music-est-duration').textContent = durStr;
    document.getElementById('music-est-scenes').textContent = data.actual_selected_scenes;
  } else if (_galleryThreshold !== null) {
    // Threshold loaded → use live balanced estimate from gallery
    _syncThresholdDisplay();
    calculateGalleryStats();
  } else {
    // Analysis phase, threshold not yet set → use crude analysis estimate
    const dur = data.estimated_duration_sec || 0;
    const durStr = dur ? fmtDur(dur, '~') : '—';
    document.getElementById('sum-duration').textContent = durStr;
    document.getElementById('sum-scene-selected').textContent = data.estimated_scenes ?? '—';
    document.getElementById('music-est-duration').textContent = durStr;
    document.getElementById('music-est-scenes').textContent = data.estimated_scenes ?? data.scene_count ?? '—';
  }
  const infoEl = document.getElementById('music-analyze-info');
  if (infoEl) infoEl.style.display = 'flex';
}

function _updateShortsBtn() {
  const count = Math.max(1, parseInt(document.getElementById('js-shorts-count')?.value || '1') || 1);
  const btn = document.getElementById('btn-render-short');
  if (btn && !btn.disabled) btn.textContent = count > 1 ? '▶ Render Shorts' : '▶ Render Short';
}

async function startRenderShort() {
  if (!currentJobId) return;
  const count = Math.max(1, parseInt(document.getElementById('js-shorts-count')?.value || '1') || 1);
  _shortsCount = count;
  const btn = document.getElementById('btn-render-short');
  btn.disabled = true;
  btn.textContent = count > 1 ? `Queuing ${count}…` : 'Generating…';
  const wrap = document.getElementById('shorts-progress-wrap');
  wrap.style.display = '';
  const slbl = document.getElementById('shorts-status-label');
  const spct = document.getElementById('shorts-pct');
  const sbar = document.getElementById('shorts-progress-bar');
  if (slbl) slbl.textContent = count > 1 ? `0 / ${count} done` : 'Generating Short…';
  if (spct) spct.textContent = '';
  if (sbar) sbar.style.width = '0%';
  const best = document.getElementById('js-shorts-best')?.checked || false;
  const resp = await api.post(`/api/jobs/${currentJobId}/render-short`, {count, best});
  if (!resp?.id) {
    alert('Render Short failed to start: ' + JSON.stringify(resp));
    btn.disabled = false;
    btn.textContent = '▶ Render Short';
    wrap.style.display = 'none';
    return;
  }
  btn.disabled = false;
  btn.textContent = '▶ Render Short';
  switchTab('log');
  // Reconnect WS if not already connected, or if main render is not active
  const job = await api.get(`/api/jobs/${currentJobId}`);
  if (!jobWs || (job?.status !== 'running' && job?.status !== 'queued')) {
    _closeJobWs();
    document.getElementById('log-panel').innerHTML = '';
    connectJobWs(currentJobId, Date.now() / 1000);
  }
}

async function startRender() {
  if (!currentJobId) return;
  document.getElementById('render-progress-wrap').style.display = '';
  document.getElementById('render-progress-bar').style.width = '0%';
  const _btnRender = document.getElementById('btn-render');
  _btnRender.disabled = true;
  _btnRender.textContent = 'Rendering...';
  const resp = await api.post(`/api/jobs/${currentJobId}/render`, {
    selected_track: pinnedTrack || _suggestedTrack || null,
    music_files: [...musicSelected],
    threshold: _galleryThreshold,
    max_scene: currentJobMaxScene || null,
    per_file:  currentJobPerFile  || null,
  });
  if (!resp?.id) {
    alert('Render failed to start: ' + JSON.stringify(resp));
    _btnRender.disabled = false;
    _btnRender.textContent = t_('btn.render') || '▶ Render Highlight';
    return;
  }
  // WS was closed by server after analyze — reconnect to stream render log
  _closeJobWs();
  document.getElementById('log-panel').innerHTML = '';
  _renderTotalSec = null;
  _renderStepNum = 0;
  _renderStepName = '';
  document.getElementById('render-pct').textContent = '';
  document.getElementById('render-eta').textContent = '';
  document.getElementById('render-step-label').textContent = '';
  document.getElementById('render-status-label').textContent = 'Rendering…';
  connectJobWs(currentJobId, Date.now() / 1000);
}


async function startMusicDrivenRender() {
  if (!currentJobId) return;
  const btn = document.getElementById('btn-render-md');
  btn.disabled = true;
  btn.textContent = '♪ Working…';
  document.getElementById('render-progress-wrap').style.display = '';
  document.getElementById('render-status-label').textContent = 'Music-driven render…';
  const resp = await api.post(`/api/jobs/${currentJobId}/render-music-driven`, {
    music_file: pinnedTrack || _suggestedTrack || null,
  });
  if (!resp?.id) {
    alert('Music-driven render failed to start: ' + JSON.stringify(resp));
    btn.disabled = false;
    btn.textContent = '♪ Music-driven';
    return;
  }
  btn.disabled = false;
  btn.textContent = '♪ Music-driven';
  _closeJobWs();
  document.getElementById('log-panel').innerHTML = '';
  switchTab('log');
  connectJobWs(currentJobId, Date.now() / 1000);
}

// ── Log ───────────────────────────────────────────────────────────────────────
const PROGRESS_RE = /^\s*\d+%\||\s*\[[\u2588\u2591 ]+\]\s+\d+%|\b\d+%\|/;

// Render step helpers
const _RENDER_STEP_ORDER = ['Clips', 'Encoding', 'Intro/outro', 'Music', 'Preview'];
function _enterRenderStep(name) {
  _renderStepName = name;
  _renderStepNum = _RENDER_STEP_ORDER.indexOf(name) + 1;
  const total = _RENDER_STEP_ORDER.length;
  document.getElementById('render-step-label').textContent = `${name} (${_renderStepNum}/${total})`;
  document.getElementById('render-progress-bar').style.width = '0%';
  document.getElementById('render-pct').textContent = '0%';
  document.getElementById('render-eta').textContent = '';
}
function _setRenderStepPct(pct, etaLabel) {
  document.getElementById('render-progress-bar').style.width = pct + '%';
  document.getElementById('render-pct').textContent = pct + '%';
  if (etaLabel !== undefined) document.getElementById('render-eta').textContent = etaLabel;
}


function _classifyLogLine(div, line) {
  if (PROGRESS_RE.test(line)) {
    div.classList.add('log-progress');
    div.style.color = 'var(--muted)';
    div.textContent = line;
    return 'progress';
  }
  const stepM = line.match(/^\[(\d+[a-z]?)(?:\/\d+)?\]/);
  if (stepM) {
    div.classList.add('log-step');
    const ts = new Date().toLocaleTimeString('pl-PL', {hour:'2-digit', minute:'2-digit', second:'2-digit'});
    div.textContent = `${ts}  ${line}`;
    return 'step';
  }
  if (/✓|cached/i.test(line) || /→.*_v\d+\.mp4/.test(line) || /\[\d+\/\d+\] checked/.test(line)) {
    div.classList.add('log-ok');
    div.textContent = line;
    return 'ok';
  }
  if (/error|failed/i.test(line)) {
    div.classList.add('log-err');
    div.textContent = line;
    return 'err';
  }
  if (/^\[DBG\]/.test(line)) {
    div.classList.add('log-debug');
    div.textContent = line;
    return 'debug';
  }
  div.classList.add('log-info');
  div.textContent = line;
  return 'info';
}

function appendLog(line) {
  const panel = document.getElementById('log-panel');
  const atBottom = panel.scrollHeight - panel.scrollTop - panel.clientHeight < 60;

  if (PROGRESS_RE.test(line)) {
    // Update existing progress line in place, or create one
    let last = panel.lastElementChild;
    // Skip past any non-visible (display:none) elements to find last visible progress line
    while (last && last.classList.contains('log-progress') && getComputedStyle(last).display === 'none') {
      last = last.previousElementSibling;
    }
    if (last && last.classList.contains('log-progress')) {
      last.textContent = line;
    } else {
      const div = document.createElement('div');
      div.className = 'log-line log-progress';
      div.style.color = 'var(--muted)';
      div.textContent = line;
      panel.appendChild(div);
    }
    if (atBottom) panel.scrollTop = panel.scrollHeight;
    if (jobPhase === 'rendering' && _renderStepName === 'Encoding') {
      const pm = line.match(/\]\s*(\d+)%\s+([\d.]+)\//);
      if (pm) {
        const pct = parseInt(pm[1]);
        const cur = parseFloat(pm[2]);
        _setRenderStepPct(pct);
        const total = _renderTotalSec || 1;
        const remSec = Math.max(0, Math.round((total - cur) / 2));
        document.getElementById('render-eta').textContent =
          remSec > 0 ? `ETA ${remSec < 60 ? remSec + 's' : Math.floor(remSec/60) + 'm' + String(remSec%60).padStart(2,'0') + 's'}` : '';
      }
    }
    return;
  }

  const div = document.createElement('div');
  div.className = 'log-line';
  const kind = _classifyLogLine(div, line);
  if (kind === 'step') activateStep(parseInt(line.match(/^\[(\d+[a-z]?)/)[1]));
  panel.appendChild(div);
  if (atBottom) panel.scrollTop = panel.scrollHeight;

  if (jobPhase === 'rendering') {
    // ── Step: Clips ──
    const clipM = line.match(/\[(\d+)\/(\d+)\]\s+\S.*\((trim|enc)\)/);
    if (clipM) {
      const n = parseInt(clipM[1]), tot = parseInt(clipM[2]);
      if (n === 1) _enterRenderStep('Clips');
      _setRenderStepPct(Math.round(n / tot * 100), `${n}/${tot}`);
      return;
    }
    // ── Step: Encoding ──
    const totM = line.match(/Encoding highlight \(([\d.]+)s\)/);
    if (totM) {
      _renderTotalSec = parseFloat(totM[1]);
      _enterRenderStep('Encoding');
      return;
    }
    // ── Step: Intro/outro ──
    if (line.includes('Adding intro/outro')) {
      _enterRenderStep('Intro/outro');
      return;
    }
    const ioM = line.match(/intro\/outro \[(\d)\/3\]/);
    if (ioM) {
      _setRenderStepPct(Math.round(parseInt(ioM[1]) / 3 * 100));
      return;
    }
    // ── Step: Music ──
    if (line.includes('Adding music')) {
      _enterRenderStep('Music');
      return;
    }
    if (line.match(/→.*\.mp4\s+\d+:\d+/)) {
      _setRenderStepPct(100);
      return;
    }
    // ── Step: Preview ──
    if (line.includes('Generating preview')) {
      _enterRenderStep('Preview');
      return;
    }
    if (line.match(/✓.*_preview\.mp4/)) {
      _setRenderStepPct(100);
      return;
    }
  }
}

// ── Steps ─────────────────────────────────────────────────────────────────────
function resetSteps() { document.querySelectorAll('.step').forEach(s=>s.className='step'); }
function activateStep(n) {
  document.querySelectorAll('.step').forEach(s => {
    const sn = parseInt(s.dataset.step);
    s.className = 'step' + (sn<n?' done': sn===n?' active':'');
  });
}

function setStatusDot(status) {
  document.getElementById('jh-dot').className = `dot s-${status}`;
  const label = status === 'idle' ? 'new' : status;
  document.getElementById('jh-status').textContent = status !== 'running' ? label : '';
  if (status === 'done') document.querySelectorAll('.step').forEach(s=>s.className='step done');
}

// ── Elapsed ───────────────────────────────────────────────────────────────────
function startElapsedTimer(startedAt) {
  if (elapsedTimer) clearInterval(elapsedTimer);
  elapsedTimer = setInterval(()=>{
    document.getElementById('jh-elapsed').textContent = formatDur(Date.now()/1000 - startedAt);
  }, 1000);
}

// ── Kill / Remove ──────────────────────────────────────────────────────────────
async function killJob() {
  if (!currentJobId) return;
  if (!await showConfirm('Stop job', 'Stop the running job?\nProgress so far will be lost.', null, 'Stop')) return;
  await api.del(`/api/jobs/${currentJobId}`);
  document.getElementById('btn-kill').style.display = 'none';
  document.getElementById('btn-kill-log').style.display = 'none';
}

async function removeJob(jobId) {
  const confirmed = await showConfirm(
    'Usuń projekt / Delete project',
    'Usunięcie projektu jest nieodwracalne. Pliki wideo i muzyka nie zostaną dotknięte — usuwany jest tylko stan zadania.\n\nDeleting the project is irreversible. Video files and music are not affected — only the job state is removed.',
    null, 'Usuń / Delete'
  );
  if (!confirmed) return;
  await api.post(`/api/jobs/${jobId}/remove`, {});
  if (jobId === currentJobId) { currentJobId = null; showWelcome(); }
  refreshJobList();
}

// ── Tabs ──────────────────────────────────────────────────────────────────────
const TAB_NAMES = ['gallery', 'music', 'results', 'log'];

function stopVideo() {
  const v = document.getElementById('video-player');
  if (v) { v.pause(); v.removeAttribute('src'); v.load(); }
  const vw = document.getElementById('video-wrap');
  if (vw) vw.style.display = 'none';
  document.querySelectorAll('.rf').forEach(c=>c.classList.remove('playing'));
}

