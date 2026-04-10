// ── Scene parameter calculator ───────────────────────────────────────────────
const _sceneParamManual = { f: false, js: false };

function markManual(prefix) {
  _sceneParamManual[prefix] = true;
  const info = document.getElementById(prefix + '-src-info');
  if (info && info.dataset.count) info.textContent = info.dataset.count + ' · manual';
  if (prefix === 'js') {
    const ms = parseFloat(document.getElementById('js-max-scene')?.value || '0');
    const pf = parseFloat(document.getElementById('js-per-file')?.value  || '0');
    if (ms > 0) currentJobMaxScene = ms;
    if (pf > 0) currentJobPerFile  = pf;
    _overridesChangedSinceRender = true;
    _syncThresholdDisplay();
    _scheduleEstimate();
  }
}

async function fillSceneParams(prefix) {
  const workdir = document.getElementById('js-workdir').value.trim();
  if (!workdir) return;
  const cameras = readCamsList(prefix + '-cam-list');
  const targetMinEl = document.getElementById('gallery-target-min') ||
                      document.getElementById(prefix + '-target-min');
  const targetMin = (_parseTargetInput(targetMinEl?.value) ?? 360) / 60;

  const camsParam = cameras.length ? cameras.join(',') : '';
  const data = await api.get(
    `/api/count-sources?dir=${encodeURIComponent(workdir)}` +
    (camsParam ? `&cameras=${encodeURIComponent(camsParam)}` : '')
  );
  if (!data) return;

  const total = data.total || 0;
  const nCams = Math.max(cameras.length, 1);
  const filesPerCam = total / nCams;

  const info = document.getElementById(prefix + '-src-info');
  const countStr = `${total} files / ${nCams} cam${nCams > 1 ? 's' : ''}`;
  if (info) { info.textContent = countStr; info.dataset.count = countStr; }

  const targetSec = targetMin * 60;
  const maxPerFile = Math.max(5, Math.round((targetSec * nCams) / (filesPerCam * 0.5)));
  const maxScene   = Math.min(maxPerFile, Math.max(4, Math.round(maxPerFile * 0.2)));

  document.getElementById(prefix + '-max-scene').value = maxScene;
  document.getElementById(prefix + '-per-file').value  = maxPerFile;
  _sceneParamManual[prefix] = false;
  if (info) info.textContent = countStr;

  if (prefix === 'js') {
    currentJobMaxScene = maxScene;
    currentJobPerFile  = maxPerFile;
    _overridesChangedSinceRender = true;
    _syncThresholdDisplay();
    _scheduleEstimate();
  }

  const wd = prefix === 'js'
    ? document.getElementById('js-workdir').value.trim()
    : workdir;
  if (wd) api.put('/api/job-config', { work_dir: wd, target_minutes: targetMin });
}

// ── Camera subfolder picker ───────────────────────────────────────────────────
const _CAM_LETTERS = ['A','B','C','D','E','F','G','H'];

async function _fetchCamSubdirs(workdir) {
  if (!workdir) return [];
  const data = await api.get(`/api/subdirs?dir=${encodeURIComponent(workdir)}`);
  return data || [];
}

function _camOptions(subdirs, selected) {
  const frag = document.createDocumentFragment();
  const none = document.createElement('option'); none.value = ''; none.textContent = '— none —';
  frag.appendChild(none);
  for (const d of subdirs) {
    const o = document.createElement('option'); o.value = o.textContent = d;
    if (d === selected) o.selected = true;
    frag.appendChild(o);
  }
  return frag;
}

function _relabelCams(container) {
  container.querySelectorAll('.cam-row').forEach((row, i) => {
    row.querySelector('.cam-label').textContent = 'Cam ' + (_CAM_LETTERS[i] || String.fromCharCode(65+i));
  });
  _updateWorkdirBrowseBtn();
}

function _setWorkdir(v) {
  const el = document.getElementById('js-workdir');
  if (!el || v == null) return;
  el.value = v;
  el.size = Math.max(32, v.length + 1);
}

function _updateWorkdirBrowseBtn() {
  const btn = document.getElementById('js-workdir-browse-btn');
  if (!btn) return;
  const count = document.getElementById('js-cam-list')?.querySelectorAll('.cam-row').length || 0;
  btn.style.display = count > 0 ? 'none' : '';
}

function _openWorkdirFileBrowser() {
  const wd = document.getElementById('js-workdir')?.value.trim();
  if (wd) openFileBrowser(wd, '');
}

async function addCamRow(containerId, value, subdirs, offsetSec) {
  const container = document.getElementById(containerId);
  if (!subdirs) {
    const wd = document.getElementById('js-workdir').value.trim();
    subdirs = await _fetchCamSubdirs(wd);
  }
  const row = document.createElement('div'); row.className = 'cam-row';
  const lbl = document.createElement('span'); lbl.className = 'cam-label'; lbl.textContent = 'Cam ?';
  const sel = document.createElement('select'); sel.className = 'cam-select';
  sel.appendChild(_camOptions(subdirs, value || ''));
  const browse = document.createElement('button'); browse.className = 'btn-sm'; browse.textContent = '📁';
  browse.title = 'Browse files'; browse.style.flexShrink = '0';
  browse.onclick = () => {
    const wd = document.getElementById('js-workdir').value.trim();
    const sub = sel.value.trim();
    openFileBrowser(wd, sub);
  };
  const offLabel = document.createElement('span');
  offLabel.textContent = '±s'; offLabel.style.cssText = 'font-size:10px;color:var(--muted);flex-shrink:0';
  offLabel.title = 'Time offset in seconds (e.g. 7200 = +2h)';
  const offInput = document.createElement('input');
  offInput.type = 'number'; offInput.className = 'cam-offset';
  offInput.value = offsetSec != null ? String(Math.round(offsetSec)) : '0';
  offInput.step = '1'; offInput.title = 'Time offset in seconds (e.g. 7200 = +2h, -3600 = -1h)';
  offInput.addEventListener('change', () => typeof saveSettingsField === 'function' && saveSettingsField());
  offInput.addEventListener('blur',   () => typeof saveSettingsField === 'function' && saveSettingsField());
  const btn = document.createElement('button'); btn.className = 'btn-sm'; btn.textContent = '−';
  btn.title = 'Remove camera'; btn.style.flexShrink = '0';
  btn.onclick = () => { row.remove(); _relabelCams(container); };
  row.append(lbl, sel, browse, offLabel, offInput, btn);
  container.appendChild(row);
  _relabelCams(container);
}

async function setCamsList(containerId, cameras, camOffsets) {
  const container = document.getElementById(containerId);
  container.innerHTML = '';
  const wd = document.getElementById('js-workdir').value.trim();
  const subdirs = await _fetchCamSubdirs(wd);
  const list = (cameras || []).filter(Boolean);
  for (const cam of list) await addCamRow(containerId, cam, subdirs, (camOffsets || {})[cam]);
  _updateWorkdirBrowseBtn();
}

function readCamOffsets(containerId) {
  const offsets = {};
  document.getElementById(containerId).querySelectorAll('.cam-row').forEach(row => {
    const cam = row.querySelector('.cam-select')?.value?.trim();
    const sec = parseInt(row.querySelector('.cam-offset')?.value || '0', 10);
    if (cam && sec !== 0) offsets[cam] = sec;
  });
  return Object.keys(offsets).length ? offsets : null;
}

function readCamsList(containerId) {
  return [...document.getElementById(containerId).querySelectorAll('.cam-select')]
    .map(s => s.value.trim()).filter(Boolean);
}

// Music dir (job Music tab)
function openMusicDirBrowser() {
  openDirBrowser(document.getElementById('music-dir-input').value, p => {
    document.getElementById('music-dir-input').value = p;
    const wd = document.getElementById('js-workdir').value.trim();
    if (wd) api.put('/api/job-config', { work_dir: wd, music_dir: p });
    if (currentJobId) api.patch(`/api/jobs/${currentJobId}/params`, { music_dir: p });
    loadMusicTracks();
  });
}

async function loadDirConfig(dir) {
  if (!dir || !dir.trim()) return;
  const data = await api.get(`/api/job-config?dir=${encodeURIComponent(dir.trim())}`);
  if (!data) return;

  if (data._resolved) {
    const jobList = await api.get('/api/jobs') || [];
    const existing = jobList.find(j => j.work_dir === data._resolved);
    if (existing) { openJob(existing.id); return; }
    if (data._has_processed) {
      const imp = await api.post('/api/jobs/import', { work_dir: data._resolved });
      if (imp?.id) { refreshJobList(); openJob(imp.id); return; }
    }
    const dr = await api.post('/api/jobs?draft=true', { work_dir: data._resolved });
    if (dr?.id) { refreshJobList(); openJob(dr.id); return; }
  }
}

async function dirUp() {
  const data = await api.get(`/api/browse?path=${encodeURIComponent(browsePath)}`);
  if (data?.parent) await browseTo(data.parent);
}

async function browseTo(path) {
  const url = path ? `/api/browse?path=${encodeURIComponent(path)}` : '/api/browse';
  const data = await api.get(url);
  if (!data) return;
  browsePath = data.path;
  document.getElementById('dir-path-txt').textContent = data.path;
  const list = document.getElementById('dir-list');
  list.innerHTML = '';
  if (data.parent) {
    const d=document.createElement('div'); d.className='de';
    d.innerHTML=`<span class="icon">↑</span><span class="name">..</span>`;
    d.onclick=()=>browseTo(data.parent); list.appendChild(d);
  }
  for (const e of data.entries) {
    if (!e.is_dir) continue;
    const d=document.createElement('div'); d.className='de';
    d.innerHTML=`<span class="icon">▶</span><span class="name"></span>
      ${e.has_mp4?'<span class="badge">MP4</span>':''}
      ${e.has_autoframe?'<span class="badge" style="color:var(--accent)">cached</span>':''}`;
    d.querySelector('.name').textContent = e.name;
    d.onclick=()=>{browsePath=e.path; document.getElementById('dir-path-txt').textContent=e.path;};
    d.ondblclick=()=>browseTo(e.path);
    list.appendChild(d);
  }
}

// ── Settings modal ────────────────────────────────────────────────────────────
async function openSettings() {
  const s = await api.get('/api/settings');
  if (s) {
    document.getElementById('s-max-jobs').value     = s.max_concurrent_jobs;
    document.getElementById('s-max-detect').value   = s.max_detect_workers;
    document.getElementById('s-clip-batch').value   = s.clip_batch_size;
    document.getElementById('s-clip-workers').value = s.clip_workers;
  }
  document.getElementById('settings-modal').classList.add('open');
  ytCheckStatus();
}

// ── Advanced detect modal ─────────────────────────────────────────────────────
function openAdvancedDetect() {
  document.getElementById('advanced-detect-modal').style.display = 'flex';
}
function closeAdvancedDetect() {
  document.getElementById('advanced-detect-modal').style.display = 'none';
}
function _syncAdvancedHint() {}  // placeholder for future live hint
