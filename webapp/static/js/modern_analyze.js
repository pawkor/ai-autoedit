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
  if (typeof refreshProjectList === 'function') refreshProjectList();
  if (typeof openProject === 'function') await openProject(data.id);
  if (typeof _connectJobProgress === 'function') _connectJobProgress(data.id);
}
window.runAnalyze = runAnalyze;

// ── Settings modal ────────────────────────────────────────────────────────────
async function openSettingsModal() {
  const modal = document.getElementById('m-settings-modal');
  if (!modal) return;
  document.getElementById('m-settings-status').textContent = '';
  await _loadSettingsPanel();
  modal.style.display = 'flex';
}
window.openSettingsModal = openSettingsModal;

function closeSettingsModal() {
  document.getElementById('m-settings-modal').style.display = 'none';
}
window.closeSettingsModal = closeSettingsModal;

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

  const payload = {
    work_dir: job.params.work_dir,
    music_dir:          musicDir,
    clip_scan_clip_dur: clipDur,
    positive,
    negative,
  };

  const status = document.getElementById('m-settings-status');
  const r = await fetch('/api/job-config', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!r.ok) {
    if (status) status.textContent = '✗ Save failed';
    return;
  }
  closeSettingsModal();
}
window.saveSettings = saveSettings;
