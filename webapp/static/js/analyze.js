// modern_analyze.js — Analyze / New Project modal + Settings panel

// ── Project modal state ───────────────────────────────────────────────────────
let _analyzeBrowserOpen = false;
let _analyzeSubdirs = [];
let _projectModalMode = 'edit';   // 'edit' = settings of current project, 'new' = blank form
let _projectModalGen = 0;         // bumped on every open — stale async saves must not touch UI
let _projectModalPrefillOk = false; // edit-mode saves allowed only after a successful prefill

function _resetProjectModalFields() {
  const set = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
  set('m-analyze-dir', '');
  set('m-analyze-positive', '');
  set('m-analyze-negative', '');
  set('m-analyze-description', '');
  set('m-analyze-clip-dur', 6);
  set('m-analyze-interval', 3);
  set('m-analyze-min-gap', 15);
  const dm = document.getElementById('m-analyze-detect-method');
  if (dm) { dm.value = 'clip-first'; if (typeof _applyDetectMethod === 'function') _applyDetectMethod('clip-first'); }
  const sc = document.getElementById('m-analyze-score-all');
  if (sc) sc.checked = true;
  const camList = document.getElementById('m-analyze-cam-list');
  if (camList) camList.innerHTML = '';
  ['m-settings-title', 'm-settings-intro-card', 'm-settings-cam-pattern',
   'm-settings-beats-fast', 'm-settings-beats-mid', 'm-settings-beats-slow',
   'm-settings-shorts-music', 'm-analyze-photos-dir',
   'm-settings-gps-weight', 'm-settings-gps-alt-threshold',
   'm-settings-adjacent-gap'].forEach(id => set(id, ''));
  const bm = document.getElementById('m-settings-beats-method');
  if (bm) bm.value = 'segments';
  const tm = document.getElementById('m-settings-timeline-method');
  if (tm) { tm.value = 'music-driven'; if (typeof _applyTimelineMethod === 'function') _applyTimelineMethod('music-driven'); }
}

// The settings fields are shared with the live Render/Build controls of the
// open project. After a 'new' form is abandoned, restore them so Render does
// not pick up values typed for the never-created project.
async function _restoreProjectSettingsFields() {
  if (typeof _jobId === 'undefined' || !_jobId) return;
  const job = await window._modernApi.get(`/api/jobs/${_jobId}`).catch(() => null);
  const wd = job?.params?.work_dir;
  if (!wd) return;
  const cfg = await window._modernApi.get(`/api/job-config?dir=${encodeURIComponent(wd)}`).catch(() => null);
  if (!cfg) return;
  const set = (id, v) => { const el = document.getElementById(id); if (el && v != null) el.value = v; };
  set('m-settings-cam-pattern', cfg.cam_pattern ?? '');
  const bm2 = document.getElementById('m-settings-beats-method');
  if (bm2 && cfg.beats_method) bm2.value = cfg.beats_method;
  const tm2 = document.getElementById('m-settings-timeline-method');
  const _tmv = cfg.ui_timeline_method ?? 'music-driven';
  if (tm2) { tm2.value = _tmv; if (typeof _applyTimelineMethod === 'function') _applyTimelineMethod(_tmv); }
}

// ── Open / close ──────────────────────────────────────────────────────────────
async function openProjectModal(mode = 'edit') {
  const modal = document.getElementById('m-project-modal');
  if (!modal) return;
  // No open project = nothing to edit — behave as a blank new-project form.
  const _hasJob = typeof _jobId !== 'undefined' && !!_jobId;
  _projectModalMode = (mode === 'new' || !_hasJob) ? 'new' : 'edit';
  _projectModalGen++;
  const _helpOn = localStorage.getItem('projectHelp') === '1';
  modal.classList.toggle('show-help', _helpOn);
  const _hb = document.getElementById('m-help-btn');
  if (_hb) _hb.classList.toggle('active', _helpOn);
  document.getElementById('m-analyze-status').textContent = '';
  document.getElementById('m-analyze-btn').disabled = false;
  const _saveBtn0 = document.getElementById('m-analyze-save-btn');
  if (_saveBtn0) _saveBtn0.disabled = false;
  _analyzeSubdirs = [];

  // Always start from a clean form — stale values from the previous open
  // must never leak into another project.
  _resetProjectModalFields();

  // In edit mode the directory is fixed to the open project: a changed dir
  // combined with per-job saves is exactly the split-brain overwrite bug.
  const _dirEl = document.getElementById('m-analyze-dir');
  const _browseBtn = document.getElementById('m-analyze-browse-btn');
  if (_dirEl) _dirEl.readOnly = _projectModalMode === 'edit';
  if (_browseBtn) _browseBtn.disabled = _projectModalMode === 'edit';
  // Photo browser operates on the OPEN project (_jobId) — in 'new' mode it
  // would silently save selections into the previous project.
  const _photosBtn = document.getElementById('m-analyze-photos-btn');
  if (_photosBtn) _photosBtn.disabled = _projectModalMode === 'new';
  const _titleEl = modal.querySelector('.m-modal-title');
  if (_titleEl) _titleEl.textContent = _projectModalMode === 'new' ? '+ New project' : '⚙ Project';

  // New mode has nothing to prefill; edit mode may save only after prefill.
  _projectModalPrefillOk = _projectModalMode === 'new';
  if (_projectModalMode === 'edit' && typeof _jobId !== 'undefined' && _jobId) {
    const _pgen = _projectModalGen;
    const job = await window._modernApi.get(`/api/jobs/${_jobId}`);
    // A newer open owns the form now — a late prefill must not write into it.
    if (_pgen !== _projectModalGen) return;
    if (job?.params?.work_dir) {
      const wd = job.params.work_dir;
      document.getElementById('m-analyze-dir').value = wd;
      const cfg = await window._modernApi.get(
        `/api/job-config?dir=${encodeURIComponent(wd)}`
      );
      if (_pgen !== _projectModalGen) return;
      if (cfg) {
        document.getElementById('m-analyze-clip-dur').value     = cfg.clip_scan_clip_dur ?? 6;
        const _dm = (cfg.clip_first !== false) ? 'clip-first' : 'traditional';
        const _dmEl = document.getElementById('m-analyze-detect-method');
        if (_dmEl) { _dmEl.value = _dm; _applyDetectMethod(_dm); }
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
      if (_pgen !== _projectModalGen) return;
      const camList = document.getElementById('m-analyze-cam-list');
      camList.innerHTML = '';
      const cams = job.params.cameras
        || [job.params.cam_a, job.params.cam_b].filter(Boolean);
      const toLoad = cams.length ? cams : [];
      const offsets  = job.params.cam_offsets  || {};
      const crops    = job.params.cam_crop_16x9 || {};
      const noTrims  = job.params.cam_no_trim   || {};
      for (const cam of toLoad)
        _appendAnalyzeCamRow(camList, cam, _analyzeSubdirs, offsets[cam] ?? 0, crops[cam] ?? false, !!noTrims[cam]);

      // Load settings fields
      const _titleParts = (job.params.title ?? cfg?.title ?? '').split('\n');
      const set = (id, val) => { const el = document.getElementById(id); if (el && val != null) el.value = val; };
      set('m-settings-title',        _titleParts[0] ?? '');
      set('m-settings-intro-card',   _titleParts.slice(1).join('\n'));
      set('m-settings-cam-pattern',  cfg?.cam_pattern ?? '');
      const _bmEl = document.getElementById('m-settings-beats-method');
      if (_bmEl && cfg?.beats_method) _bmEl.value = cfg.beats_method;
      set('m-settings-beats-fast',   cfg?.beats_fast  ?? '');
      set('m-settings-beats-mid',    cfg?.beats_mid   ?? '');
      set('m-settings-beats-slow',   cfg?.beats_slow  ?? '');
      set('m-settings-shorts-music',      cfg?.shorts_music_dir ?? '');
      set('m-analyze-photos-dir',         cfg?.photos_dir || (wd + '/photos'));
      set('m-settings-gps-weight',        cfg?.gps_weight                 ?? '');
      set('m-settings-gps-alt-threshold', cfg?.gps_altitude_threshold_m   ?? '');
      set('m-settings-adjacent-gap',      cfg?.adjacent_time_gap_sec      ?? '');
      const _tm = cfg?.ui_timeline_method ?? 'music-driven';
      const _tmEl = document.getElementById('m-settings-timeline-method');
      if (_tmEl) { _tmEl.value = _tm; _applyTimelineMethod(_tm); }
      // Saving with an empty form after a failed prefill would wipe the
      // project's config — allow edit-mode saves only when cfg loaded.
      _projectModalPrefillOk = !!cfg;
    }
  }
  modal.style.display = 'flex';
}
window.openProjectModal = openProjectModal;
window.openAnalyzeModal  = openProjectModal;
window.openSettingsModal = openProjectModal;

async function closeProjectModal() {
  // 'new' mode saves nothing on close — the project config is written only
  // by Analyze (runAnalyze) once the user actually creates it.
  if (_projectModalMode === 'edit') await saveProjectModal();
  else _restoreProjectSettingsFields();   // shared fields back to the open project
  document.getElementById('m-project-modal').style.display = 'none';
  _closeBrowser();
}
window.closeProjectModal  = closeProjectModal;
window.closeAnalyzeModal  = closeProjectModal;
window.closeSettingsModal = closeProjectModal;

// ── Directory browser ─────────────────────────────────────────────────────────
async function analyzeToggleBrowser() {
  // On macOS desktop app, use native folder picker (handles TCC permissions).
  if (typeof window.pickFolder === 'function') {
    const path = await window.pickFolder();
    if (path) await _selectBrowserPath(path);
    return;
  }
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

  let data;
  if (window.aeBrowse) {
    try { data = JSON.parse(await window.aeBrowse(path || '')); } catch { data = null; }
  }
  if (!data) {
    const url = path ? `/api/browse?path=${encodeURIComponent(path)}` : '/api/browse';
    data = await window._modernApi.get(url);
  }
  if (!data) {
    if (entries) entries.innerHTML =
      '<div style="padding:4px;color:var(--red)">Error loading directory</div>';
    return;
  }

  const pathEl = document.getElementById('m-analyze-browser-path');
  if (pathEl) {
    pathEl.innerHTML = '';
    const parentPath = data.parent ?? (data.path && data.path !== '/' ? (data.path.replace(/\/[^/]+\/?$/, '') || '/') : null);
    if (parentPath) {
      const up = document.createElement('button');
      up.className = 'm-btn m-btn-ghost m-btn-sm';
      up.textContent = '↑ ..';
      up.onclick = () => _loadBrowser(parentPath);
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
  // Re-adding a previously deleted project: its config.ini still holds the
  // prompts/params — prefill instead of overwriting them with blanks on Save.
  if (_projectModalMode === 'new') {
    const cfg = await window._modernApi.get(
      `/api/job-config?dir=${encodeURIComponent(path)}`).catch(() => null);
    if (cfg && document.getElementById('m-analyze-dir')?.value.trim() === path) {
      const set = (id, v) => { const el = document.getElementById(id); if (el && v != null && v !== '') el.value = v; };
      set('m-analyze-positive',    cfg.positive);
      set('m-analyze-negative',    cfg.negative);
      set('m-analyze-description', cfg.description);
      set('m-analyze-clip-dur',    cfg.clip_scan_clip_dur);
      set('m-analyze-interval',    cfg.clip_scan_interval);
      set('m-analyze-min-gap',     cfg.clip_scan_min_gap);
      const _tparts = (cfg.title || '').split('\n');
      set('m-settings-title',      _tparts[0]);
      set('m-settings-intro-card', _tparts.slice(1).join('\n'));
      set('m-analyze-photos-dir',  cfg.photos_dir);
      set('m-settings-cam-pattern', cfg.cam_pattern);
    }
  }
}

async function _fetchAnalyzeSubdirs(dir) {
  if (!dir) return [];
  if (window.aeSubdirs) {
    try {
      const result = JSON.parse(await window.aeSubdirs(dir));
      if (Array.isArray(result) && result.length > 0) return result;
    } catch {}
  }
  const data = await window._modernApi.get(
    `/api/subdirs?dir=${encodeURIComponent(dir)}`
  );
  return Array.isArray(data) ? data : [];
}

// ── Camera rows ───────────────────────────────────────────────────────────────
function _appendAnalyzeCamRow(container, selected, subdirs, offset = 0, crop = false, noTrim = false) {
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

  const cropCb = document.createElement('input');
  cropCb.type = 'checkbox';
  cropCb.className = 'm-cam-crop';
  cropCb.checked = !!crop;
  cropCb.title = 'Crop 4:3 → 16:9 (center crop, loses ~12% top/bottom)';
  cropCb.style.cssText = 'margin-left:8px;cursor:pointer';

  const cropLabel = document.createElement('span');
  cropLabel.style.cssText = 'font-size:11px;color:var(--muted);flex-shrink:0';
  cropLabel.textContent = '4:3→16:9';

  const noTrimCb = document.createElement('input');
  noTrimCb.type = 'checkbox';
  noTrimCb.className = 'm-cam-no-trim';
  noTrimCb.checked = !!noTrim;
  noTrimCb.title = 'No trim — use original source files as-is (no cutting)';
  noTrimCb.style.cssText = 'margin-left:8px;cursor:pointer';

  const noTrimLabel = document.createElement('span');
  noTrimLabel.style.cssText = 'font-size:11px;color:var(--muted);flex-shrink:0';
  noTrimLabel.textContent = 'No trim';

  const rm = document.createElement('button');
  rm.className = 'm-btn m-btn-ghost m-btn-sm';
  rm.textContent = '−'; rm.title = 'Remove camera';
  rm.onclick = () => { row.remove(); _relabelAnalyzeCams(container); _onCamListChange(); };

  row.append(label, sel, offLabel, offInput, offSuffix, cropCb, cropLabel, noTrimCb, noTrimLabel, rm);
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
  const detected = _analyzeSubdirs.slice(0, 2);
  for (const cam of detected)
    _appendAnalyzeCamRow(camList, cam, _analyzeSubdirs);
  // Auto-fill params for new project
  _autoSuggestOnDirSelect(dir, detected);
}
window.analyzeRefreshCams = analyzeRefreshCams;

async function _autoSuggestOnDirSelect(dir, cams) {
  try {
    const r = await fetch(`/api/suggest-clip-params?work_dir=${encodeURIComponent(dir)}`);
    const data = r.ok ? await r.json() : null;
    if (data?.clip_dur != null) {
      document.getElementById('m-analyze-clip-dur').value = data.clip_dur;
      document.getElementById('m-analyze-interval').value = data.interval;
      document.getElementById('m-analyze-min-gap').value  = data.min_gap;
    }
  } catch {}
  if (cams.length === 2) {
    const patEl = document.getElementById('m-settings-cam-pattern');
    if (patEl && !patEl.value.trim()) patEl.value = 'abab';
  }
}

// ── Collect the Source form into job params (shared by Analyze and Save) ────
function _collectProjectParams(dir) {
  const camRows = [...(document.getElementById('m-analyze-cam-list')
    ?.querySelectorAll('.m-analyze-cam-row') || [])];
  const cameras = camRows.map(r => r.querySelector('select')?.value.trim()).filter(Boolean);
  const camOffsets = {};
  const camCrops   = {};
  const camNoTrim  = {};
  camRows.forEach(r => {
    const name   = r.querySelector('select')?.value.trim();
    const off    = parseFloat(r.querySelector('.m-cam-offset')?.value) || 0;
    const crop   = r.querySelector('.m-cam-crop')?.checked    ?? false;
    const noTrim = r.querySelector('.m-cam-no-trim')?.checked ?? false;
    // Always send explicit 0/1 for every camera — omitting unchecked ones
    // left stale `cam = 1` entries in config.ini and made no-trim impossible
    // to disable via rerun.
    if (name) { camOffsets[name] = off; camCrops[name] = crop ? 1 : 0; camNoTrim[name] = noTrim ? 1 : 0; }
  });
  const clipFirst  = document.getElementById('m-analyze-detect-method')?.value !== 'traditional';
  const clipDur    = parseFloat(document.getElementById('m-analyze-clip-dur')?.value)   || 6;
  const interval   = parseFloat(document.getElementById('m-analyze-interval')?.value)   || 3;
  const minGap     = parseFloat(document.getElementById('m-analyze-min-gap')?.value)    || 15;
  const scoreAll   = document.getElementById('m-analyze-score-all')?.checked ?? true;
  const positive   = document.getElementById('m-analyze-positive')?.value.trim() || null;
  const negative   = document.getElementById('m-analyze-negative')?.value.trim() || null;
  const description = document.getElementById('m-analyze-description')?.value.trim() || null;
  const camPattern = document.getElementById('m-settings-cam-pattern')?.value.trim() || undefined;
  return {
    work_dir:              dir,
    description,
    cameras:               cameras.length ? cameras : null,
    cam_offsets:           Object.keys(camOffsets).length ? camOffsets : null,
    cam_crop_16x9:         Object.keys(camCrops).length  ? camCrops  : null,
    cam_no_trim:           cameras.length ? camNoTrim : null,
    clip_first:            clipFirst,
    clip_scan_clip_dur:    clipDur,
    clip_scan_interval:    interval,
    clip_scan_min_gap:     minGap,
    score_all_cams:        scoreAll,
    positive,
    negative,
    cam_pattern:           camPattern,
  };
}

// ── Run analyze ───────────────────────────────────────────────────────────────
async function runAnalyze() {
  const dir = document.getElementById('m-analyze-dir').value.trim();
  if (!dir) { alert('Select a project directory first.'); return; }

  const btn    = document.getElementById('m-analyze-btn');
  const saveBtn = document.getElementById('m-analyze-save-btn');
  const status = document.getElementById('m-analyze-status');
  if (btn)     btn.disabled = true;
  // Save during a starting Analyze would race it into a duplicate draft.
  if (saveBtn) saveBtn.disabled = true;
  if (status) status.textContent = 'Starting…';

  const params = _collectProjectParams(dir);

  // Persist the Output/Render block BEFORE starting the analysis so the
  // pipeline reads the just-typed title/beats/photos_dir from config.ini —
  // direct Analyze (without Save) must not lose these fields.
  await _putProjectConfig(dir, _collectOutputConfig());

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
    if (btn)     btn.disabled = false;
    if (saveBtn) saveBtn.disabled = false;
    if (status)  status.textContent = 'Failed — check server log';
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
  const description = document.getElementById('m-analyze-description')?.value.trim() || undefined;
  const clipFirst   = document.getElementById('m-analyze-detect-method')?.value !== 'traditional';
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
  // Snapshot cameras BEFORE the await below — no DOM reads after awaits.
  const _snap = _collectProjectParams(dir);
  // Per-job saves only when the form's directory IS the open project —
  // otherwise cameras/prompts would land in a different project (split-brain).
  const _job = jobId ? await window._modernApi.get(`/api/jobs/${jobId}`).catch(() => null) : null;
  const _dirMatchesJob = !!(_job && _job.params?.work_dir === dir);
  if (jobId && _dirMatchesJob) {
    if (_snap.cameras?.length) {
      saves.push(fetch(`/api/jobs/${jobId}/params`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          cameras:       _snap.cameras,
          cam_offsets:   _snap.cam_offsets,
          cam_crop_16x9: _snap.cam_crop_16x9,
          cam_no_trim:   _snap.cam_no_trim,
        }),
      }).catch(() => {}));
    }

    saves.push(fetch(`/api/jobs/${jobId}/save-prompts`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ description, positive, negative }),
    }).catch(() => {}));
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
// Snapshot the Output/Render/Shorts form block. Taken synchronously BEFORE
// any await — a delayed save must never read fields of a newer form.
function _collectOutputConfig() {
  const _titleLine  = document.getElementById('m-settings-title')?.value.trim() || '';
  const _cardLine   = document.getElementById('m-settings-intro-card')?.value.trim() || '';
  const title       = _titleLine ? (_cardLine ? `${_titleLine}\n${_cardLine}` : _titleLine) : null;
  return {
    title,
    shorts_music_dir:      document.getElementById('m-settings-shorts-music')?.value.trim() || null,
    photos_dir:            document.getElementById('m-analyze-photos-dir')?.value.trim()    || null,
    cam_pattern:           document.getElementById('m-settings-cam-pattern')?.value.trim()  || '',
    ui_timeline_method:    document.getElementById('m-settings-timeline-method')?.value || 'music-driven',
    beats_method:               document.getElementById('m-settings-beats-method')?.value ?? 'segments',
    beats_fast:                 parseInt(document.getElementById('m-settings-beats-fast')?.value)  || null,
    beats_mid:                  parseInt(document.getElementById('m-settings-beats-mid')?.value)   || null,
    beats_slow:                 parseInt(document.getElementById('m-settings-beats-slow')?.value)  || null,
    gps_weight:                 parseFloat(document.getElementById('m-settings-gps-weight')?.value) || null,
    gps_altitude_threshold_m:   parseFloat(document.getElementById('m-settings-gps-alt-threshold')?.value) || null,
    // parseFloat('0')||null would drop an explicit 0 (= disabled) — keep it.
    adjacent_time_gap_sec:      (document.getElementById('m-settings-adjacent-gap')?.value.trim() === ''
                                 ? null
                                 : parseFloat(document.getElementById('m-settings-adjacent-gap').value)),
  };
}

// PUT the Output/Render/Shorts settings block into workDir's config.ini.
function _putProjectConfig(workDir, snap) {
  return fetch('/api/job-config', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ work_dir: workDir, ...(snap || _collectOutputConfig()) }),
  }).catch(() => null);
}

async function saveProjectModal() {
  // New-project mode: Save creates a draft job (idle, no analysis) so the
  // project shows up in the sidebar immediately; the modal then switches to
  // edit mode targeting the freshly created project.
  if (_projectModalMode === 'new') {
    const status = document.getElementById('m-analyze-status');
    const dir = document.getElementById('m-analyze-dir')?.value.trim();
    if (!dir) { if (status) status.textContent = 'Select a project directory first'; return; }
    // Snapshot EVERYTHING before the first await — a delayed response must
    // save this form's values, never those of a form opened meanwhile.
    const gen = _projectModalGen;
    const srcParams = _collectProjectParams(dir);
    const outSnap   = _collectOutputConfig();
    let data = null;
    try {
      const r = await fetch('/api/jobs?analyze_only=true&draft=true', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(srcParams),
      });
      data = r.ok ? await r.json() : null;
    } catch { data = null; }
    if (!data?.id) {
      if (gen === _projectModalGen && status) status.textContent = '✗ Save failed — check server log';
      return;
    }
    const cfgR = await _putProjectConfig(dir, outSnap);
    if (typeof refreshProjectList === 'function') refreshProjectList();
    // UI/mode updates only for the form this save belongs to.
    if (gen !== _projectModalGen) return;
    if (typeof openProject === 'function') await openProject(data.id);
    _projectModalMode = 'edit';
    _projectModalPrefillOk = true;   // form holds the user's fresh input
    const _dirEl = document.getElementById('m-analyze-dir');
    if (_dirEl) _dirEl.readOnly = true;
    // Normalize the field to the resolved work_dir (typed paths may differ,
    // e.g. a trailing slash — a mismatch would block every later save).
    const _nj = await window._modernApi.get(`/api/jobs/${data.id}`).catch(() => null);
    if (_nj?.params?.work_dir && _dirEl) _dirEl.value = _nj.params.work_dir;
    const _bb = document.getElementById('m-analyze-browse-btn');
    if (_bb) _bb.disabled = true;
    const _t = document.querySelector('#m-project-modal .m-modal-title');
    if (_t) _t.textContent = '⚙ Project';
    if (status) {
      if (cfgR && cfgR.ok) {
        status.textContent = '✓ Project created';
        setTimeout(() => { status.textContent = ''; }, 1500);
      } else {
        status.textContent = '✗ Project created, output settings not saved — press Save to retry';
      }
    }
    return;
  }
  if (typeof _jobId === 'undefined' || !_jobId) return;
  if (!_projectModalPrefillOk) {
    // Prefill failed (API error / restart) — the form holds blanks, saving
    // them would erase the project's config.
    const _st0 = document.getElementById('m-analyze-status');
    if (_st0) _st0.textContent = '✗ Settings not loaded — reopen ⚙ Project';
    return;
  }
  // Snapshot before any await — same stale-form hazard as in 'new' mode.
  const gen = _projectModalGen;
  const outSnap = _collectOutputConfig();
  const _dirField = document.getElementById('m-analyze-dir')?.value.trim();
  const status = document.getElementById('m-analyze-status');
  const job = await window._modernApi.get(`/api/jobs/${_jobId}`);
  if (!job?.params?.work_dir) return;
  // Belt & braces: in edit mode the dir field is read-only, but never write
  // this project's Output/Render block when the form points elsewhere.
  if (_dirField && _dirField !== job.params.work_dir) {
    if (gen === _projectModalGen && status) status.textContent = '✗ Directory mismatch — not saved';
    return;
  }

  // Analyze settings
  await saveAnalyzeSettings();

  // Output / Render / Shorts / Privacy
  const cfgR = await _putProjectConfig(job.params.work_dir, outSnap);
  if (gen !== _projectModalGen) return;   // a newer form owns the UI now
  if (!cfgR || !cfgR.ok) { if (status) status.textContent = '✗ Save failed'; return; }
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

function toggleProjectHelp() {
  const modal = document.getElementById('m-project-modal');
  const btn   = document.getElementById('m-help-btn');
  if (!modal) return;
  const on = modal.classList.toggle('show-help');
  localStorage.setItem('projectHelp', on ? '1' : '');
  if (btn) btn.classList.toggle('active', on);
}
window.toggleProjectHelp = toggleProjectHelp;

function _applyDetectMethod(method) {
  const p = document.getElementById('m-detect-clip-params');
  if (p) p.style.display = (method === 'traditional') ? 'none' : 'contents';
}
window._applyDetectMethod = _applyDetectMethod;

function _applyTimelineMethod(method) {
  const s = document.getElementById('m-music-driven-settings');
  if (s) s.style.display = (method === 'traditional') ? 'none' : '';
}
window._applyTimelineMethod = _applyTimelineMethod;

async function suggestClipParams() {
  const btn = document.getElementById('m-analyze-auto-params');
  if (!btn) return;
  const dir = document.getElementById('m-analyze-dir')?.value.trim();
  if (!dir) { alert('Select a project directory first.'); return; }
  btn.disabled = true;
  const orig = btn.textContent;
  btn.textContent = '…';
  try {
    const r = await fetch(`/api/suggest-clip-params?work_dir=${encodeURIComponent(dir)}`);
    const data = r.ok ? await r.json() : null;
    if (data?.clip_dur != null) {
      document.getElementById('m-analyze-clip-dur').value = data.clip_dur;
      document.getElementById('m-analyze-interval').value = data.interval;
      document.getElementById('m-analyze-min-gap').value  = data.min_gap;
    }
  } catch (e) {
    alert('Could not suggest params: ' + (e?.message || e));
  } finally {
    btn.disabled = false;
    btn.textContent = orig;
  }
}
window.generateSettingsPrompts = generateSettingsPrompts;
