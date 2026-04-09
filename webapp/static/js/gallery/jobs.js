// ── Job settings (Settings tab) ───────────────────────────────────────────────

async function populateJobSettings(params) {
  const sv = (id, v) => { if (v != null && v !== '') document.getElementById(id).value = v; };
  const sc = (id, v) => { if (v != null) document.getElementById(id).checked = !!v; };

  _setWorkdir(params.work_dir);
  s3SectionInit();
  const cfg = await api.get(`/api/job-config?dir=${encodeURIComponent(params.work_dir)}`);
  const _camOffsets = params.cam_offsets || cfg?.cam_offsets;
  const _cams = params.cameras || [params.cam_a, params.cam_b].filter(Boolean);
  await setCamsList('js-cam-list', _cams, _camOffsets);
  const _multicamWrap = document.getElementById('shorts-multicam-wrap');
  if (_multicamWrap) _multicamWrap.style.display = _cams.length >= 2 ? '' : 'none';
  sv('js-title',    params.title);
  sc('js-no-intro',        params.no_intro);
  sc('js-no-music',        params.no_music);
  sc('js-shorts-text',     params.shorts_text);
  sc('js-shorts-multicam', params.shorts_multicam);
  sc('js-shorts-ncs',      params.shorts_ncs);
  const cropEl = document.getElementById('js-shorts-crop-offsets');
  if (cropEl) cropEl.value = params.shorts_crop_offsets || '';
  if (params.music_dir) document.getElementById('music-dir-input').value = params.music_dir;
  musicSelected = new Set();

  let th = params.threshold, ms = params.max_scene, pf = params.per_file;
  if (cfg) {
    if (th == null) th = cfg.threshold;
    if (ms == null) ms = cfg.max_scene;
    if (pf == null) pf = cfg.per_file;
    if (!params.music_dir && cfg.music_dir) document.getElementById('music-dir-input').value = cfg.music_dir;
    if (cfg.positive) document.getElementById('js-positive').value = cfg.positive;
    if (cfg.negative) document.getElementById('js-negative').value = cfg.negative;
  }
  document.getElementById('js-description').value = params.description ||
    'Motorcycle ride on winding mountain roads, scenic landscapes, sweeping curves and technical sections. Sunny weather, good visibility, beautiful surroundings.';
  sv('js-max-scene',    ms);
  sv('js-per-file',     pf);
  sv('js-sd-threshold', params.sd_threshold ?? cfg?.sd_threshold);
  sv('js-sd-min-scene', params.sd_min_scene  ?? cfg?.sd_min_scene);
  if (ms != null) currentJobMaxScene = parseFloat(ms);
  if (pf != null) currentJobPerFile  = parseFloat(pf);
  const mt = params.min_take ?? cfg?.min_take;
  if (mt != null) currentJobMinTake = parseFloat(mt); else currentJobMinTake = 0.5;
  const mg = params.min_gap_sec ?? cfg?.min_gap_sec;
  const mgEl = document.getElementById('min-gap-input');
  if (mgEl && mg != null) mgEl.value = parseFloat(mg) || '';
  const tm = params.target_minutes ?? cfg?.target_minutes;
  if (tm != null) {
    const el = document.getElementById('gallery-target-min');
    if (el) { const mm = Math.floor(tm); const ss = Math.round((tm - mm) * 60); el.value = `${mm}:${String(ss).padStart(2,'0')}`; }
  }
  _sceneParamManual['js'] = false;
  fillSceneParams('js').then(() => {
    if (ms != null) { document.getElementById('js-max-scene').value = ms; _sceneParamManual['js'] = false; }
    if (pf != null) { document.getElementById('js-per-file').value  = pf; _sceneParamManual['js'] = false; }
  });
  if (document.getElementById('music-dir-input').value.trim()) loadMusicTracks();
}

function readJobSettings() {
  const gv = id => document.getElementById(id).value.trim();
  const gc = id => document.getElementById(id).checked;
  const p = { work_dir: gv('js-workdir') };
  if (_galleryThreshold !== null) p.threshold = _galleryThreshold;
  const ms = gv('js-max-scene'); if (ms) p.max_scene = parseFloat(ms);
  const pf = gv('js-per-file');  if (pf) p.per_file  = parseFloat(pf);
  const mg = parseFloat(document.getElementById('min-gap-input')?.value) || 0;
  if (mg > 0) p.min_gap_sec = mg;
  const sdt = gv('js-sd-threshold'); p.sd_threshold = sdt ? parseFloat(sdt) : 20;
  const sdm = gv('js-sd-min-scene'); if (sdm) p.sd_min_scene  = sdm;
  const cameras = readCamsList('js-cam-list'); if (cameras.length) p.cameras = cameras;
  const offsets = readCamOffsets('js-cam-list'); if (offsets) p.cam_offsets = offsets;
  const ti = gv('js-title');     if (ti) p.title = ti;
  p.no_intro        = gc('js-no-intro');
  p.no_music        = gc('js-no-music');
  p.shorts_text     = gc('js-shorts-text');
  p.shorts_multicam    = gc('js-shorts-multicam');
  p.shorts_ncs         = gc('js-shorts-ncs');
  p.shorts_crop_offsets = document.getElementById('js-shorts-crop-offsets')?.value?.trim() || '';
  const md = document.getElementById('music-dir-input').value.trim();
  if (md) p.music_dir = md;
  if (musicSelected.size) p.music_files = [...musicSelected];
  const desc = gv('js-description'); if (desc) p.description = desc;
  const pos = gv('js-positive'); if (pos) p.positive = pos;
  const neg = gv('js-negative'); if (neg) p.negative = neg;
  return p;
}

async function rerunFromSettings() {
  const params = readJobSettings();
  if (!params.work_dir || !currentJobId) return;
  const btn = document.querySelector('[onclick="rerunFromSettings()"]');
  if (btn) { btn.disabled = true; btn.textContent = '…'; }
  try {
    const resp = await api.post(`/api/jobs/${currentJobId}/rerun`, params);
    if (resp?.id) { refreshJobList(); openJob(resp.id); }
    else alert('Analyze failed: ' + JSON.stringify(resp));
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Analyze'; }
  }
}

async function rerunCurrentJob() {
  if (!currentJobId) return;
  const job = await api.get(`/api/jobs/${currentJobId}`);
  if (!job) return;
  const settingsWorkdir = document.getElementById('js-workdir').value.trim();
  const params = settingsWorkdir === job.params.work_dir ? readJobSettings() : {...job.params};
  if (_galleryThreshold !== null) params.threshold = _galleryThreshold;
  const resp = await api.post(`/api/jobs/${currentJobId}/rerun`, params);
  if (resp?.id) { refreshJobList(); openJob(resp.id); }
}
