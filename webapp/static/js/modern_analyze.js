// modern_analyze.js — Analyze / New Project modal + Settings panel

// ── Project modal state ───────────────────────────────────────────────────────
let _analyzeBrowserOpen = false;
let _analyzeSubdirs = [];

// ── Open / close ──────────────────────────────────────────────────────────────
async function openProjectModal() {
  const modal = document.getElementById('m-project-modal');
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
        document.getElementById('m-analyze-description').value  = job.params?.description ?? cfg.description ?? '';
        const mgEl = document.getElementById('m-analyze-min-gap');
        if (mgEl) mgEl.value = cfg.clip_scan_min_gap ?? 15;
        const ivEl = document.getElementById('m-analyze-interval');
        if (ivEl) ivEl.value = cfg.clip_scan_interval ?? 3;
      }
      const scoreEl = document.getElementById('m-analyze-score-all');
      if (scoreEl) scoreEl.checked = job.params.score_all_cams ?? true;
      _analyzeSubdirs = await _fetchAnalyzeSubdirs(wd);
      const camList = document.getElementById('m-analyze-cam-list');
      camList.innerHTML = '';
      const cams = job.params.cameras
        || [job.params.cam_a, job.params.cam_b].filter(Boolean);
      const toLoad = cams.length ? cams : _analyzeSubdirs.slice(0, 2);
      const offsets = job.params.cam_offsets || {};
      for (const cam of toLoad)
        _appendAnalyzeCamRow(camList, cam, _analyzeSubdirs, offsets[cam] ?? 0);

      // Load settings fields
      const _titleParts = (job.params.title ?? cfg?.title ?? '').split('\n');
      const set = (id, val) => { const el = document.getElementById(id); if (el && val != null) el.value = val; };
      set('m-settings-title',        _titleParts[0] ?? '');
      set('m-settings-intro-card',   _titleParts.slice(1).join('\n'));
      set('m-settings-cam-pattern',  cfg?.cam_pattern ?? '');
      set('m-settings-beats-fast',   cfg?.beats_fast  ?? '');
      set('m-settings-beats-mid',    cfg?.beats_mid   ?? '');
      set('m-settings-beats-slow',   cfg?.beats_slow  ?? '');
      set('m-settings-shorts-music', cfg?.shorts_music_dir ?? '');
      set('m-analyze-photos-dir',    cfg?.photos_dir || (wd + '/photos'));
      set('m-settings-blur-speed',   cfg?.blur_speedometer_cams ?? '');
      const _bpEl = document.getElementById('m-settings-blur-plates');
      if (_bpEl) _bpEl.checked = cfg?.blur_plates === true || cfg?.blur_plates === 'true' || cfg?.blur_plates === '1';
    }
  }
  modal.style.display = 'flex';
}
window.openProjectModal = openProjectModal;
window.openAnalyzeModal  = openProjectModal;
window.openSettingsModal = openProjectModal;

async function closeProjectModal() {
  await saveProjectModal();
  document.getElementById('m-project-modal').style.display = 'none';
  _closeBrowser();
}
window.closeProjectModal  = closeProjectModal;
window.closeAnalyzeModal  = closeProjectModal;
window.closeSettingsModal = closeProjectModal;

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
      badge.style.cssText = 'font-size:10px;color:var(--green-hi)';
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
function _appendAnalyzeCamRow(container, selected, subdirs, offset = 0) {
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
  sel.onchange = _onCamListChange;

  const offLabel = document.createElement('span');
  offLabel.style.cssText = 'font-size:11px;color:var(--muted);flex-shrink:0';
  offLabel.textContent = '±';

  const offInput = document.createElement('input');
  offInput.type = 'number';
  offInput.className = 'm-input m-cam-offset';
  offInput.value = offset || 0;
  offInput.title = 'Time offset in seconds (positive = camera is ahead)';
  offInput.style.cssText = 'width:60px;text-align:right';

  const offSuffix = document.createElement('span');
  offSuffix.style.cssText = 'font-size:11px;color:var(--muted);flex-shrink:0';
  offSuffix.textContent = 's';

  const rm = document.createElement('button');
  rm.className = 'm-btn m-btn-ghost m-btn-sm';
  rm.textContent = '−'; rm.title = 'Remove camera';
  rm.onclick = () => { row.remove(); _relabelAnalyzeCams(container); _onCamListChange(); };

  row.append(label, sel, offLabel, offInput, offSuffix, rm);
  container.appendChild(row);
}

function _onCamListChange() {
  const camList = document.getElementById('m-analyze-cam-list');
  if (!camList) return;
  const count = camList.querySelectorAll('.m-analyze-cam-row select')
    .length;
  const scoreEl = document.getElementById('m-analyze-score-all');
  if (scoreEl && count >= 2) scoreEl.checked = true;
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
  _onCamListChange();
}
window.analyzeAddCam = analyzeAddCam;

async function analyzeAutoDetectOffsets() {
  const btn    = document.getElementById('m-analyze-detect-btn');
  const status = document.getElementById('m-analyze-detect-status');
  const dir    = document.getElementById('m-analyze-dir')?.value.trim();
  if (!dir) { if (status) status.textContent = 'Set directory first'; return; }

  const camList = document.getElementById('m-analyze-cam-list');
  const rows    = camList ? [...camList.querySelectorAll('.m-analyze-cam-row')] : [];
  const cameras = rows.map(r => r.querySelector('select')?.value).filter(Boolean);
  if (cameras.length < 2) { if (status) status.textContent = 'Need ≥2 cameras'; return; }

  if (btn) btn.disabled = true;
  if (status) status.textContent = 'Detecting…';

  // Use existing job if available, otherwise create a temporary detect call via the first job endpoint
  const jobId = (typeof _jobId !== 'undefined') ? _jobId : null;
  if (!jobId) { if (status) status.textContent = 'Save project first'; if (btn) btn.disabled = false; return; }

  const r = await fetch(`/api/jobs/${jobId}/detect-cam-offsets`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ work_dir: dir, cameras }),
  });
  if (btn) btn.disabled = false;
  if (!r.ok) { if (status) status.textContent = '✗ Failed'; return; }
  const data = await r.json();
  const offsets = data.offsets || {};

  // Fill offset inputs in matching rows
  for (const row of rows) {
    const cam = row.querySelector('select')?.value;
    const inp = row.querySelector('.m-cam-offset');
    if (cam && inp && offsets[cam] != null) inp.value = Math.round(offsets[cam]);
  }
  if (status) {
    const parts = Object.entries(offsets).map(([k, v]) => `${k}:${Math.round(v)}s`).join(', ');
    status.textContent = parts ? `✓ ${parts}` : '✓ No offset detected';
    setTimeout(() => { status.textContent = ''; }, 4000);
  }
}
window.analyzeAutoDetectOffsets = analyzeAutoDetectOffsets;

async function analyzeRefreshCams(dir) {
  if (!dir) return;
  const camList = document.getElementById('m-analyze-cam-list');
  if (!camList || camList.querySelectorAll('.m-analyze-cam-row').length) return;
  _analyzeSubdirs = await _fetchAnalyzeSubdirs(dir);
  camList.innerHTML = '';
  for (const cam of _analyzeSubdirs.slice(0, 2))
    _appendAnalyzeCamRow(camList, cam, _analyzeSubdirs);
}
window.analyzeRefreshCams = analyzeRefreshCams;

// ── Run analyze ───────────────────────────────────────────────────────────────
async function runAnalyze() {
  const dir = document.getElementById('m-analyze-dir').value.trim();
  if (!dir) { alert('Select a project directory first.'); return; }

  const camRows = [...document.getElementById('m-analyze-cam-list')
    .querySelectorAll('.m-analyze-cam-row')];
  const cameras = camRows.map(r => r.querySelector('select')?.value.trim()).filter(Boolean);
  const camOffsets = {};
  camRows.forEach(r => {
    const name = r.querySelector('select')?.value.trim();
    const off  = parseFloat(r.querySelector('.m-cam-offset')?.value) || 0;
    if (name) camOffsets[name] = off;
  });
  const clipFirst  = document.getElementById('m-analyze-clip-first').checked;
  const clipDur    = parseFloat(document.getElementById('m-analyze-clip-dur').value)     || 6;
  const interval   = parseFloat(document.getElementById('m-analyze-interval')?.value)   || 3;
  const minGap     = parseFloat(document.getElementById('m-analyze-min-gap')?.value)    || 15;
  const scoreAll   = document.getElementById('m-analyze-score-all')?.checked ?? true;
  const positive   = document.getElementById('m-analyze-positive').value.trim() || null;
  const negative   = document.getElementById('m-analyze-negative').value.trim() || null;

  const btn    = document.getElementById('m-analyze-btn');
  const status = document.getElementById('m-analyze-status');
  if (btn)    btn.disabled = true;
  if (status) status.textContent = 'Starting…';

  const params = {
    work_dir:              dir,
    cameras:               cameras.length ? cameras : null,
    cam_offsets:           Object.keys(camOffsets).length ? camOffsets : null,
    clip_first:            clipFirst,
    clip_scan_clip_dur:    clipDur,
    clip_scan_interval:    interval,
    clip_scan_min_gap:     minGap,
    score_all_cams:        scoreAll,
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
  if (typeof refreshProjectList === 'function') refreshProjectList();
  if (typeof openProject === 'function') await openProject(data.id);
  if (typeof _connectJobProgress === 'function') _connectJobProgress(data.id);
}
window.runAnalyze = runAnalyze;

// ── Save settings from Analyze modal ─────────────────────────────────────────
async function saveAnalyzeSettings() {
  const dir = document.getElementById('m-analyze-dir')?.value.trim();
  if (!dir) return;

  const positive    = document.getElementById('m-analyze-positive')?.value.trim()    || null;
  const negative    = document.getElementById('m-analyze-negative')?.value.trim()    || null;
  const description = document.getElementById('m-analyze-description')?.value.trim() || null;
  const clipFirst   = document.getElementById('m-analyze-clip-first')?.checked;
  const clipDur     = parseFloat(document.getElementById('m-analyze-clip-dur')?.value)  || null;
  const interval    = parseFloat(document.getElementById('m-analyze-interval')?.value)  || null;
  const minGap      = parseFloat(document.getElementById('m-analyze-min-gap')?.value)   || null;

  const saves = [
    fetch('/api/job-config', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        work_dir: dir, positive, negative,
        clip_first: clipFirst, clip_scan_clip_dur: clipDur,
        clip_scan_interval: interval, clip_scan_min_gap: minGap,
      }),
    }).catch(() => {}),
  ];

  const jobId = (typeof _jobId !== 'undefined') ? _jobId : null;
  if (jobId) {
    const job = await window._modernApi.get(`/api/jobs/${jobId}`).catch(() => null);
    if (job && job.params?.work_dir === dir) {
      saves.push(fetch(`/api/jobs/${jobId}/save-prompts`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ description, positive, negative }),
      }).catch(() => {}));
    }
  }

  await Promise.all(saves);

  const status = document.getElementById('m-analyze-status');
  if (status) { status.textContent = '✓ Saved'; setTimeout(() => { status.textContent = ''; }, 1500); }
}
window.saveAnalyzeSettings = saveAnalyzeSettings;

// ── Generate prompts for Analyze modal ───────────────────────────────────────
async function generateAnalyzePrompts() {
  const dir = document.getElementById('m-analyze-dir').value.trim();
  const description = document.getElementById('m-analyze-description')?.value.trim() || '';
  const btn    = document.getElementById('m-analyze-gen-btn');
  const status = document.getElementById('m-analyze-gen-status');
  if (btn)    btn.disabled = true;
  if (status) status.textContent = 'Generating…';

  let data = null;
  try {
    const r = await fetch('/api/about', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ description, work_dir: dir || undefined }),
    });
    data = r.ok ? await r.json() : null;
  } catch { data = null; }

  if (btn) btn.disabled = false;
  if (!data?.ok) {
    if (status) status.textContent = '✗ Failed';
    return;
  }
  if (status) status.textContent = '✓ Done';
  const pos = document.getElementById('m-analyze-positive');
  const neg = document.getElementById('m-analyze-negative');
  if (pos && data.positive) pos.value = data.positive;
  if (neg && data.negative) neg.value = data.negative;
}
window.generateAnalyzePrompts = generateAnalyzePrompts;

// ── Unified save (Project modal) ──────────────────────────────────────────────
async function saveProjectModal() {
  if (typeof _jobId === 'undefined' || !_jobId) return;
  const job = await window._modernApi.get(`/api/jobs/${_jobId}`);
  if (!job?.params?.work_dir) return;
  const status = document.getElementById('m-analyze-status');

  // Analyze settings
  await saveAnalyzeSettings();

  // Output / Render / Shorts / Privacy
  const _titleLine  = document.getElementById('m-settings-title')?.value.trim() || '';
  const _cardLine   = document.getElementById('m-settings-intro-card')?.value.trim() || '';
  const title       = _titleLine ? (_cardLine ? `${_titleLine}\n${_cardLine}` : _titleLine) : null;
  const cfgR = await fetch('/api/job-config', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      work_dir:              job.params.work_dir,
      title,
      shorts_music_dir:      document.getElementById('m-settings-shorts-music')?.value.trim() || null,
      photos_dir:            document.getElementById('m-analyze-photos-dir')?.value.trim()    || null,
      blur_speedometer_cams: document.getElementById('m-settings-blur-speed')?.value.trim()   || null,
      blur_plates:           document.getElementById('m-settings-blur-plates')?.checked ?? false,
      cam_pattern:           document.getElementById('m-settings-cam-pattern')?.value.trim()  || '',
      beats_fast:            parseInt(document.getElementById('m-settings-beats-fast')?.value)  || null,
      beats_mid:             parseInt(document.getElementById('m-settings-beats-mid')?.value)   || null,
      beats_slow:            parseInt(document.getElementById('m-settings-beats-slow')?.value)  || null,
    }),
  });
  if (!cfgR.ok) { if (status) status.textContent = '✗ Save failed'; return; }
  if (status) { status.textContent = '✓ Saved'; setTimeout(() => { status.textContent = ''; }, 1500); }
}
window.saveProjectModal = saveProjectModal;
window.saveSettings     = saveProjectModal;

async function generateSettingsPrompts() {
  if (typeof _jobId === 'undefined' || !_jobId) {
    alert('No project selected.'); return;
  }
  const job = await window._modernApi.get(`/api/jobs/${_jobId}`);
  if (!job?.params?.work_dir) return;

  const description = document.getElementById('m-settings-description')?.value.trim() || '';
  const btn    = document.getElementById('m-settings-gen-btn');
  const gstatus = document.getElementById('m-settings-gen-status');
  if (btn) btn.disabled = true;
  if (gstatus) gstatus.textContent = 'Generating…';

  let data = null;
  try {
    const r = await fetch('/api/about', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ description, work_dir: job.params.work_dir }),
    });
    data = r.ok ? await r.json() : null;
  } catch { data = null; }

  if (btn) btn.disabled = false;
  if (!data?.ok) {
    if (gstatus) gstatus.textContent = '✗ Failed';
    return;
  }
  if (gstatus) gstatus.textContent = '✓ Done';
  const pos = document.getElementById('m-settings-positive');
  const neg = document.getElementById('m-settings-negative');
  if (pos && data.positive) pos.value = data.positive;
  if (neg && data.negative) neg.value = data.negative;
}
window.generateSettingsPrompts = generateSettingsPrompts;
