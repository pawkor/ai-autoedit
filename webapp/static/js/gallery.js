function _computePerFileCuts() {
  _perFileCuts = new Set();
  if (_galleryThreshold === null) return;
  const perFile = currentJobPerFile ||
    parseFloat(document.getElementById('js-per-file')?.value || '0') || 0;
  if (!perFile) return;
  const maxSec = currentJobMaxScene ||
    parseFloat(document.getElementById('js-max-scene')?.value || '0') || Infinity;
  const groups = new Map();
  for (const f of framesData) {
    const ov = manualOverrides[f.scene];
    if (ov === 'include' || ov === 'exclude') continue;
    if (f.score == null || f.score < _galleryThreshold) continue;
    const src = f.scene.replace(/-scene-\d+$/, '');
    if (!groups.has(src)) groups.set(src, []);
    groups.get(src).push(f);
  }
  for (const scenes of groups.values()) {
    scenes.sort((a, b) => b.score - a.score);
    let total = 0;
    for (const f of scenes) {
      const take = Math.min(f.duration ?? maxSec, maxSec);
      if (take < currentJobMinTake) continue;
      if (total >= perFile || total + take - perFile > 0.5) {
        _perFileCuts.add(f.scene);
      } else {
        total += take;
      }
    }
  }
  _computeBalancedScenes();
}

// Passes threshold + per-file budget checks (no camera balance). Used as input for balancing.
function _passesPreBalance(f) {
  const ov = manualOverrides[f.scene];
  // Manual overrides are outside the balance computation — they are added/removed
  // on top of the balanced set in isIncluded(). Including them here would cause
  // rebalancing to drop/add unrelated scenes when the user toggles one scene.
  if (ov === 'include' || ov === 'exclude') return false;
  if (_galleryThreshold !== null ? f.score < _galleryThreshold : f.score < parseFloat(document.getElementById('threshold-val').value)) return false;
  return !_perFileCuts.has(f.scene);
}

// Compute the camera-balanced scene set. Mirrors select_scenes.py balancing algorithm.
// Result stored in _balancedScenes (Set<scene name>) or null for single-cam.
function _computeBalancedScenes() {
  _balancedScenes = null;
  const base = framesData.filter(f => _passesPreBalance(f));
  const hasCams = base.some(f => f.camera && f.camera !== 'default');
  if (!hasCams || base.length === 0) return;

  const SLACK = 2;
  const maxSec = currentJobMaxScene || parseFloat(document.getElementById('js-max-scene')?.value || '0') || 10;
  const perFile = currentJobPerFile || parseFloat(document.getElementById('js-per-file')?.value || '0') || 0;

  const buckets = new Map();
  for (const f of base) {
    const cam = f.camera || 'default';
    if (!buckets.has(cam)) buckets.set(cam, []);
    buckets.get(cam).push(f);
  }
  if (buckets.size <= 1) return;

  const cams = [...buckets.keys()];
  const counts = cams.map(c => buckets.get(c).length);
  if (Math.max(...counts) - Math.min(...counts) <= SLACK) return; // already balanced

  const target = Math.round(counts.reduce((a, b) => a + b, 0) / cams.length);

  // Step 1: trim over-represented cameras
  for (const cam of cams) {
    const g = buckets.get(cam);
    const maxAllowed = target + SLACK;
    if (g.length > maxAllowed)
      buckets.set(cam, [...g].sort((a, b) => b.score - a.score).slice(0, maxAllowed));
  }

  // Step 2: boost under-represented cameras with scenes just below threshold
  if (perFile > 0 && _galleryThreshold !== null) {
    const boostFloor = Math.max(0, _galleryThreshold - 0.15);
    const allUsed = new Set([...buckets.values()].flat().map(f => f.scene));
    for (const cam of cams) {
      const g = buckets.get(cam);
      const targetMin = target - SLACK;
      if (g.length >= targetMin) continue;
      const srcUsed = new Map();
      for (const f of g) {
        const src = f.scene.replace(/-scene-\d+$/, '');
        srcUsed.set(src, (srcUsed.get(src) || 0) + Math.min(f.duration ?? maxSec, maxSec));
      }
      const candidates = framesData
        .filter(f => f.camera === cam && f.score >= boostFloor &&
                     f.score < _galleryThreshold && !allUsed.has(f.scene) &&
                     manualOverrides[f.scene] !== 'exclude')
        .sort((a, b) => b.score - a.score);
      for (const f of candidates) {
        if (g.length >= targetMin) break;
        if (f.duration == null) continue;
        const src = f.scene.replace(/-scene-\d+$/, '');
        const used = srcUsed.get(src) || 0;
        if (used >= perFile) continue;
        const take = Math.min(f.duration, maxSec, perFile - used);
        if (take < currentJobMinTake) continue;
        g.push(f);
        srcUsed.set(src, used + take);
        allUsed.add(f.scene);
      }
    }
  }

  // Final trim pass
  const finalMin = Math.min(...cams.map(c => buckets.get(c).length));
  for (const cam of cams) {
    const g = buckets.get(cam);
    const maxFinal = finalMin + SLACK;
    if (g.length > maxFinal)
      buckets.set(cam, [...g].sort((a, b) => b.score - a.score).slice(0, maxFinal));
  }

  _balancedScenes = new Set([...buckets.values()].flat().map(f => f.scene));
}

function isIncluded(f) {
  const ov = manualOverrides[f.scene];
  if (ov === 'include') return true;
  if (ov === 'exclude') return false;
  // In dual-cam mode use the pre-computed balanced set (includes boosted below-threshold scenes)
  if (_balancedScenes !== null) return _balancedScenes.has(f.scene);
  if (_galleryThreshold !== null ? f.score < _galleryThreshold : f.score < parseFloat(document.getElementById('threshold-val').value)) return false;
  return !_perFileCuts.has(f.scene);
}

function saveOverrides() {
  if (!currentJobId) return;
  api.put(`/api/jobs/${currentJobId}/overrides`, manualOverrides); // fire-and-forget
}

async function loadOverrides() {
  if (!currentJobId) return;
  manualOverrides = await api.get(`/api/jobs/${currentJobId}/overrides`) || {};
}

function toggleFrame(scene) {
  const f = framesData.find(x=>x.scene===scene);
  if (!f) return;
  const threshold = parseFloat(document.getElementById('threshold-val').value);
  const ov = manualOverrides[scene];
  const byThreshold = f.score >= threshold;
  if (ov === undefined) {
    manualOverrides[scene] = byThreshold ? 'exclude' : 'include';
  } else {
    delete manualOverrides[scene];
  }
  _overridesChangedSinceRender = true;
  saveOverrides();
  // Update only this card in-place — no full grid re-render
  const card = document.querySelector(`[data-scene="${CSS.escape(scene)}"]`);
  if (card) {
    const newOv = manualOverrides[scene];
    const included = isIncluded(f);
    const aboveThreshold = f.score != null && _galleryThreshold !== null && f.score >= _galleryThreshold;
    const limited = !newOv && !included && aboveThreshold;
    card.className = 'fc ' + (included ? 'included' : limited ? 'limited' : 'excluded') + (newOv ? ' manual' : '');
    const limitReason = _perFileCuts.has(f.scene) ? 'per-file limit' : 'camera balance';
    card.title = newOv ? `Score: ${f.score.toFixed(3)} (manual — click to reset)`
      : limited ? `Score: ${f.score.toFixed(3)} (cut by ${limitReason} — click to force include)`
      : `Score: ${f.score.toFixed(3)} (click to toggle)`;
  }
  _syncThresholdDisplay();
  calculateGalleryStats();
}

function _cachedSrc(url) {
  if (!url) return '';
  if (_frameCache.has(url)) return _frameCache.get(url);
  // Start background fetch into blob cache
  fetch(url).then(r=>r.blob()).then(b=>{
    if (!_frameCache.has(url)) _frameCache.set(url, URL.createObjectURL(b));
  }).catch(()=>{});
  return url; // use original URL on first load
}

function _sceneMatchesFilters(f) {
  if (_filterScore) {
    const s = f.score != null ? f.score.toFixed(3) : '';
    if (!s.startsWith(_filterScore)) return false;
  }
  if (_filterTime) {
    // Extract HHMMSS from scene name e.g. VID_20250421_092049_001-scene-008 → 092049
    const m = f.scene.match(/\d{8}_(\d{6})/);
    if (!m) return false;
    const needle = _filterTime.replace(/:/g, ''); // strip colons typed by user
    if (!m[1].startsWith(needle)) return false;
  }
  return true;
}

function _applyGalleryFilter() {
  document.querySelectorAll('#frames-grid .fc').forEach(card => {
    const f = framesData.find(x => x.scene === card.dataset.scene);
    card.style.display = (f && _sceneMatchesFilters(f)) ? '' : 'none';
  });
}

function onGalleryFilter() {
  _filterScore = (document.getElementById('filter-score')?.value ?? '').trim();
  _filterTime  = (document.getElementById('filter-time')?.value  ?? '').trim();
  _applyGalleryFilter();
}

function renderGallery() {
  const grid = document.getElementById('frames-grid');
  const frag = document.createDocumentFragment();
  for (const f of [...framesData].sort((a,b)=>a.scene<b.scene?-1:a.scene>b.scene?1:0)) {
    const ov = manualOverrides[f.scene];
    const included = isIncluded(f);
    const aboveThreshold = f.score != null && _galleryThreshold !== null && f.score >= _galleryThreshold;
    const limited = !ov && !included && aboveThreshold;
    const card = document.createElement('div');
    card.className = 'fc ' + (included ? 'included' : limited ? 'limited' : 'excluded') + (ov ? ' manual' : '');
    card.style.cursor = 'pointer';
    card.dataset.scene = f.scene;
    const limitReason = _perFileCuts.has(f.scene) ? 'per-file limit' : 'camera balance';
    card.title = ov ? `Score: ${f.score.toFixed(3)} (manual — click to reset)`
      : limited ? `Score: ${f.score.toFixed(3)} (cut by ${limitReason} — click to force include)`
      : `Score: ${f.score.toFixed(3)} (click to toggle)`;
    card.onclick = ()=>toggleFrame(f.scene);
    const sceneLabel = (s => {
      const m = s.match(/(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2}).*-scene-(\d+)$/);
      return m ? `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}:${m[6]} #${m[7]}` : s.split('/').pop().slice(-22);
    })(f.scene);
    const limitBadge = limited ? `<span class="fc-limit-badge">limit</span>` : '';
    const _maxSec = currentJobMaxScene || parseFloat(document.getElementById('js-max-scene')?.value || '0') || 10;
    const effDur = Math.min(f.duration ?? _maxSec, _maxSec);
    const durBadge = `<span class="fc-dur">${effDur.toFixed(1)}s</span>`;
    card.innerHTML = `<img src="${_cachedSrc(f.frame_url)}" loading="lazy" onerror="this.style.display='none'">
      <div class="fc-info"><span class="fc-score">${f.score.toFixed(3)}</span>${durBadge}<span class="fc-name"></span>${limitBadge}</div>`;
    card.querySelector('.fc-name').textContent = sceneLabel;
    // Hover → video clip preview
    if (f.frame_url) {
      const clipPath = f.frame_url.replace('_autoframe/frames/', '_autoframe/autocut/').replace(/\.jpg$/, '.mp4');
      card.addEventListener('mouseenter', e2 => _showFilePreview(clipPath, e2, 500));
      card.addEventListener('mouseleave', _hideFilePreview);
      card.addEventListener('mousemove',  _moveFileTip);
    }
    frag.appendChild(card);
  }
  grid.innerHTML = '';
  grid.appendChild(frag);
  _galleryDirty = false;
  _applyGalleryFilter();
}

function _refreshGalleryClasses() {
  // Fast path: update only CSS classes/badges on existing cards without re-creating DOM
  const sceneMap = new Map(framesData.map(f => [f.scene, f]));
  document.querySelectorAll('#frames-grid .fc').forEach(card => {
    const f = sceneMap.get(card.dataset.scene);
    if (!f) return;
    const ov = manualOverrides[f.scene];
    const included = isIncluded(f);
    const aboveThreshold = f.score != null && _galleryThreshold !== null && f.score >= _galleryThreshold;
    const limited = !ov && !included && aboveThreshold;
    card.className = 'fc ' + (included ? 'included' : limited ? 'limited' : 'excluded') + (ov ? ' manual' : '');
    const limitReason = _perFileCuts.has(f.scene) ? 'per-file limit' : 'camera balance';
    card.title = ov ? `Score: ${f.score.toFixed(3)} (manual — click to reset)`
      : limited ? `Score: ${f.score.toFixed(3)} (cut by ${limitReason} — click to force include)`
      : `Score: ${f.score.toFixed(3)} (click to toggle)`;
    const badge = card.querySelector('.fc-limit-badge');
    if (limited && !badge) {
      card.querySelector('.fc-info')?.insertAdjacentHTML('beforeend', '<span class="fc-limit-badge">limit</span>');
    } else if (!limited && badge) {
      badge.remove();
    }
  });
  _applyGalleryFilter();
}

function _estimateDuration(scenes) {
  // Mirrors select_scenes.py: max_scene cap per scene + per_file cap per source file
  const maxSec = currentJobMaxScene ||
    parseFloat(document.getElementById('js-max-scene')?.value || '0') || 10;
  const perFile = currentJobPerFile ||
    parseFloat(document.getElementById('js-per-file')?.value || '0') || null;
  const bySource = new Map();
  for (const f of scenes) {
    if (f.duration == null) continue;
    const src = f.scene.replace(/-scene-\d+$/, '');
    if (!bySource.has(src)) bySource.set(src, []);
    bySource.get(src).push(f);
  }
  let total = 0;
  for (const group of bySource.values()) {
    let fileTot = 0;
    for (const f of group.sort((a,b) => (b.score??0)-(a.score??0))) {
      let take = Math.min(f.duration, maxSec);
      if (perFile != null) take = Math.min(take, perFile - fileTot);
      if (take < currentJobMinTake) continue;
      total   += take;
      fileTot += take;
      if (perFile != null && fileTot >= perFile) break;
    }
  }
  return total;
}

// Convert main-cam duration + main-cam scene count to total (all cams) duration.
// _avgBackCamTakeSec is the avg capped back-cam clip take, loaded from /frames.
// cam_ratio - 1 = back-cam scenes per main-cam scene (pairing rate).
function _estimateTotalDuration(mainDur, mainCount) {
  const camRatio = analyzeResult?.cam_ratio ?? 1.0;
  if (camRatio <= 1.0 || _avgBackCamTakeSec == null) return mainDur * camRatio;
  return mainDur + mainCount * (camRatio - 1) * _avgBackCamTakeSec;
}

function _balancedEstimate() {
  // balanced = main-cam scenes only (back cam is not in framesData / scene_scores.csv).
  const balanced = framesData.filter(f => isIncluded(f));
  const scaledCount = balanced.length;
  const camRatio = analyzeResult?.cam_ratio ?? 1.0;

  // Scale from the server's last dry-run estimate using ONLY the manual-override delta.
  // We avoid using balanced.length as a ratio denominator because it can be 2× serverMain
  // when perFile is unset (perFileCuts skips, all above-threshold scenes land in balanced).
  // Instead: serverMain + count(force-include) - count(force-exclude) is accurate.
  const serverDur  = analyzeResult?.estimated_duration_sec;
  const serverMain = analyzeResult?.estimated_main_scenes ?? analyzeResult?.estimated_scenes;
  if (serverDur > 0 && serverMain > 0) {
    let delta = 0;
    for (const ov of Object.values(manualOverrides)) {
      if (ov === 'include') delta++;
      if (ov === 'exclude') delta--;
    }
    const duration = Math.max(0, Math.round(serverDur * (serverMain + delta) / serverMain));
    return { scenes: balanced, duration, scaledCount, camRatio };
  }

  // Fallback used only before any binary-search/estimate has run.
  const mainDur = _estimateDuration(balanced);
  const duration = _estimateTotalDuration(mainDur, balanced.length);
  return { scenes: balanced, duration, scaledCount, camRatio };
}

function calculateGalleryStats() {
  if (_targetSearchActive) return; // progress bar is shown; don't overwrite with stale estimate
  const v = _galleryThreshold;
  // useActual: threshold matches last render (actual_threshold)
  const renderThr = analyzeResult?.actual_threshold;
  const useActual = v !== null && analyzeResult?.actual_selected_scenes != null &&
                    analyzeResult?.actual_duration_sec != null && !_overridesChangedSinceRender &&
                    renderThr != null && Math.abs(v - renderThr) < 0.0015;
  // useEstimated: threshold matches last server estimate (auto_threshold, set by binary search)
  const estThr = analyzeResult?.auto_threshold;
  const useEstimated = !useActual && !_overridesChangedSinceRender &&
                       analyzeResult?.estimated_scenes > 0 &&
                       analyzeResult?.estimated_duration_sec > 0 &&
                       estThr != null && Math.abs(v - estThr) < 0.0015;
  if (useActual) {
    document.getElementById('gallery-stats-text').textContent =
      `${analyzeResult.actual_selected_scenes} / ${framesData.length} scenes · ${fmtDur(analyzeResult.actual_duration_sec)}`;
    return;
  }
  if (useEstimated) {
    document.getElementById('gallery-stats-text').textContent =
      `${analyzeResult.estimated_scenes} / ${framesData.length} scenes · ${fmtDur(analyzeResult.estimated_duration_sec, '~')} est.`;
    return;
  }
  const est = _balancedEstimate();
  const hasDur = est.scenes.some(f => f.duration != null);
  const countTxt = `${est.scaledCount} / ${framesData.length} scenes`;
  let durTxt = '';
  if (hasDur) {
    durTxt = ` · ${fmtDur(est.duration, '~')} est.`;
  }
  document.getElementById('gallery-stats-text').textContent = countTxt + durTxt;
}

function saveSettingsField() {
  if (!currentJobId) return;
  const gv = id => document.getElementById(id)?.value.trim();
  const gc = id => document.getElementById(id)?.checked;
  const workDir = gv('js-workdir');
  if (!workDir) return;
  const data = { work_dir: workDir };
  const sdt = gv('js-sd-threshold'); if (sdt) data.sd_threshold = parseFloat(sdt);
  const sdm = gv('js-sd-min-scene'); if (sdm) data.sd_min_scene = sdm;
  const ms  = gv('js-max-scene');    if (ms)  data.max_scene     = parseFloat(ms);
  const pf  = gv('js-per-file');     if (pf)  data.per_file      = parseFloat(pf);
  const ti  = gv('js-title');        if (ti)  data.title          = ti;
  data.no_intro    = gc('js-no-intro') || false;
  data.no_music    = gc('js-no-music') || false;
  data.shorts_text = gc('js-shorts-text') || false;
  const offsets = readCamOffsets('js-cam-list');
  if (offsets) data.cam_offsets = offsets;
  api.put('/api/job-config', data);
}

function savePromptsField() {
  if (!currentJobId) return;
  const workDir = document.getElementById('js-workdir')?.value.trim();
  if (!workDir) return;
  const positive = document.getElementById('js-positive')?.value.trim() || '';
  const negative = document.getElementById('js-negative')?.value.trim() || '';
  api.post('/api/save-prompts', { work_dir: workDir, positive, negative });
}

function _saveThreshold() {
  if (!currentJobId || _galleryThreshold === null) return;
  clearTimeout(_thresholdSaveTimer);
  _thresholdSaveTimer = setTimeout(() => {
    api.patch(`/api/jobs/${currentJobId}/params`, { threshold: _galleryThreshold });
  }, 600);
}

let _estimateTimer = null;
function _scheduleEstimate() {
  if (!currentJobId || !analyzeResult) return;
  clearTimeout(_estimateTimer);
  _estimateTimer = setTimeout(async () => {
    const thr = _galleryThreshold;
    const ms  = currentJobMaxScene || parseFloat(document.getElementById('js-max-scene')?.value) || 10;
    const pf  = currentJobPerFile  || parseFloat(document.getElementById('js-per-file')?.value)  || 45;
    const res = await api.post(`/api/jobs/${currentJobId}/estimate`,
                               { threshold: thr, max_scene: ms, per_file: pf });
    if (res?.scenes != null && analyzeResult) {
      analyzeResult.estimated_scenes        = res.scenes;
      analyzeResult.estimated_duration_sec  = res.duration_sec;
      analyzeResult.estimated_main_scenes   = res.main_scenes;
      analyzeResult.cam_ratio               = res.cam_ratio;
      analyzeResult.auto_threshold          = res.threshold;
      _syncThresholdDisplay();
      calculateGalleryStats();

    }
  }, 900);
}

function _syncThresholdDisplay() {
  if (_galleryThreshold === null) return;
  _computePerFileCuts();
  const v = _galleryThreshold;
  if (!framesData.length) return;

  // If threshold matches the last actual run and overrides haven't changed since, show actual output.
  // Otherwise fall back to the live estimate (simulates what a re-run would produce).
  const renderThr = analyzeResult?.actual_threshold;
  const useActual = analyzeResult?.actual_selected_scenes != null &&
                    analyzeResult?.actual_duration_sec   != null &&
                    !_overridesChangedSinceRender &&
                    renderThr != null && Math.abs(v - renderThr) < 0.0015;

  const estThr = analyzeResult?.auto_threshold;
  const useEstimated = !useActual && !_overridesChangedSinceRender &&
                       analyzeResult?.estimated_scenes > 0 &&
                       analyzeResult?.estimated_duration_sec > 0 &&
                       estThr != null && Math.abs(v - estThr) < 0.0015;
  let scenes, durStr;
  if (useActual) {
    scenes  = analyzeResult.actual_selected_scenes;
    durStr  = fmtDur(analyzeResult.actual_duration_sec);
  } else if (useEstimated) {
    scenes  = analyzeResult.estimated_scenes;
    durStr  = fmtDur(analyzeResult.estimated_duration_sec, '~');
  } else {
    const est = _balancedEstimate();
    scenes  = est.scaledCount;
    const hasDur = est.scenes.some(f => f.duration != null);
    const estDur = Math.round(est.duration);
    durStr  = (hasDur && scenes) ? fmtDur(estDur, '~') : '—';
    if (analyzeResult) { analyzeResult._live_est_scenes = scenes; analyzeResult._live_est_dur = estDur; }
  }

  document.getElementById('sum-duration').textContent = durStr;
  document.getElementById('sum-scene-selected').textContent = scenes || '—';
  const mEst = document.getElementById('music-est-duration');
  if (mEst) mEst.textContent = durStr;
  const mScenes = document.getElementById('music-est-scenes');
  if (mScenes) mScenes.textContent = scenes || '—';
}

// Parse "M:SS", "M:S", or plain "M" / "M.f" into total seconds
function _parseTargetInput(raw) {
  const s = (raw || '').trim();
  const m = s.match(/^(\d+):(\d{1,2})$/);
  if (m) return parseInt(m[1]) * 60 + parseInt(m[2]);
  const n = parseFloat(s);
  return isNaN(n) ? null : n * 60;
}

function _applyTargetMin(el) {
  const secs = _parseTargetInput(el.value);
  if (!secs || secs <= 0) {
    const w = document.getElementById('target-dur-warn');
    if (w) { w.textContent = ''; w.style.display = 'none'; }
    return;
  }
  const mins  = secs / 60;
  const mm    = Math.floor(mins);
  const ss    = Math.round((mins - mm) * 60);
  el.value = `${mm}:${String(ss).padStart(2,'0')}`;
  autoTargetThreshold(mins);
}

function _durationAtThreshold(thr) {
  // Mirror _balancedEstimate() exactly at an arbitrary threshold.
  // Save and restore all threshold-dependent state so the binary search is side-effect-free.
  const savedThr      = _galleryThreshold;
  const savedBalanced = _balancedScenes;
  const savedCuts     = _perFileCuts;

  _galleryThreshold = thr;
  _computePerFileCuts(); // updates _perFileCuts + _balancedScenes for this thr

  const balanced = framesData.filter(f => isIncluded(f));
  // Must mirror _balancedEstimate() exactly.
  const mainDur = _estimateDuration(balanced);
  const dur = _estimateTotalDuration(mainDur, balanced.length);

  _galleryThreshold = savedThr;
  _balancedScenes   = savedBalanced;
  _perFileCuts      = savedCuts;

  return dur;
}

async function autoTargetThreshold(targetMin) {
  if (!framesData.length || !targetMin || targetMin <= 0 || !currentJobId) return;
  const targetSec = targetMin * 60;
  const warnEl = document.getElementById('target-dur-warn');

  // Abort any in-flight search
  if (_targetAbortController) { _targetAbortController.abort(); _targetAbortController = null; }
  const ctrl = new AbortController();
  _targetAbortController = ctrl;

  // Clear any overrides we auto-added in a previous fill run
  for (const scene of _autoFillOverrides) delete manualOverrides[scene];
  _autoFillOverrides.clear();

  // Persist target to config and job params
  const wd = document.getElementById('js-workdir')?.value.trim();
  if (wd) api.put('/api/job-config', { work_dir: wd, target_minutes: targetMin });
  if (currentJobId) api.patch(`/api/jobs/${currentJobId}/params`, { target_minutes: targetMin });

  // Show progress indicator in stats text
  _targetSearchActive = true;
  _setGallerySearchStatus(0, 12);
  if (warnEl)  { warnEl.textContent = ''; warnEl.style.display = 'none'; }

  const ms = currentJobMaxScene || parseFloat(document.getElementById('js-max-scene')?.value) || 10;
  const pf = currentJobPerFile  || parseFloat(document.getElementById('js-per-file')?.value)  || 45;


  let res = null;
  let _searchId = null;
  try {
    const r = await fetch(`/api/jobs/${currentJobId}/find-threshold`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target_sec: targetSec, max_scene: ms, per_file: pf }),
      signal: ctrl.signal,
    });
    if (!r.ok) { res = null; return; }
    const startData = await r.json();
    _searchId = startData.search_id;
    if (!_searchId) { res = null; return; }

    // Poll until done
    while (true) {
      if (ctrl.signal.aborted) break;
      await new Promise(ok => setTimeout(ok, 400));
      if (ctrl.signal.aborted) break;
      const pr = await fetch(`/api/threshold-search/${_searchId}`, { signal: ctrl.signal });
      if (!pr.ok) break;
      const msg = await pr.json();
      if (!msg.done) {
        _setGallerySearchStatus(msg.iteration || 0, msg.total || 12);
      } else {
        res = (msg.error || msg.cancelled) ? null : msg;
        break;
      }
    }
  } catch (e) {
    if (e.name === 'AbortError') return; // superseded by newer request — exit silently
  } finally {
    _targetSearchActive = false;
    if (_searchId) fetch(`/api/threshold-search/${_searchId}`, { method: 'DELETE' }).catch(() => {});
    if (_targetAbortController === ctrl) _targetAbortController = null;
    // Stats text will be restored by calculateGalleryStats() called from _applyThreshold
    // (or remains as last search state until then — reset here as fallback)
    _setGallerySearchStatus(null, 0);
  }

  let warnMsg = '';

  if (res?.threshold != null) {
    const thr = Math.round(res.threshold * 1000) / 1000;
    document.getElementById('threshold-val').value = thr.toFixed(3);

    // Ensure analyzeResult exists — may be null if loadAnalyzeResult raced with binary search
    if (!analyzeResult) analyzeResult = {};
    analyzeResult.estimated_scenes       = res.scenes;
    analyzeResult.estimated_duration_sec = res.duration_sec;
    analyzeResult.estimated_main_scenes  = res.main_scenes;
    analyzeResult.cam_ratio              = res.cam_ratio;
    analyzeResult.auto_threshold         = thr;

    _applyThreshold(thr);
    // Immediate save to config.ini (no debounce) so render uses the found threshold
    if (currentJobId) {
      api.patch(`/api/jobs/${currentJobId}/params`, { threshold: thr });
      const wd2 = document.getElementById('js-workdir')?.value.trim();
      if (wd2) api.put('/api/job-config', { work_dir: wd2, threshold: thr });
    }

    if (res.duration_sec < targetSec - 10) {
      const gotMin = Math.floor(res.duration_sec / 60), gotS = Math.round(res.duration_sec % 60);
      warnMsg = `⚠ max ~${gotMin}:${String(gotS).padStart(2,'0')}`;
    }
  } else if (res !== null) {
    warnMsg = '⚠ search failed';
  }

  if (warnEl) {
    warnEl.textContent = warnMsg;
    warnEl.style.display = warnMsg ? '' : 'none';
    warnEl.style.color = warnMsg ? '#d4a017' : '';
  }
}

function resetThreshold() {
  manualOverrides = {};
  _autoFillOverrides.clear();
  saveOverrides();
  const targetMin = _parseTargetInput(document.getElementById('gallery-target-min')?.value) / 60;
  if (targetMin > 0) {
    autoTargetThreshold(targetMin);
  } else if (analyzeResult?.auto_threshold) {
    _galleryThreshold = parseFloat(analyzeResult.auto_threshold);
    document.getElementById('threshold-val').value = _galleryThreshold.toFixed(3);
    _applyThreshold(_galleryThreshold);
  }
}

let _thresholdRafId = null;
function _applyThreshold(v) {
  _galleryThreshold = v;
  _syncThresholdDisplay();
  _saveThreshold();
  _scheduleEstimate();
  calculateGalleryStats(); // update count/est immediately (pure JS, no DOM mutation)
  if (_thresholdRafId) cancelAnimationFrame(_thresholdRafId);
  _thresholdRafId = requestAnimationFrame(() => {
    _thresholdRafId = null;
    _refreshGalleryClasses(); // slow DOM class updates deferred to next frame
  });
}

function stepThreshold(delta) {
  const inp = document.getElementById('threshold-val');
  let v = parseFloat(inp.value) + delta;
  v = Math.max(0, Math.round(v * 1000) / 1000);
  inp.value = v.toFixed(3);
  _applyThreshold(v);
}

function onThresholdEdit() {
  const inp = document.getElementById('threshold-val');
  let v = parseFloat(inp.value);
  if (isNaN(v)) return;
  v = Math.max(0, Math.min(1, v));
  inp.value = v.toFixed(3);
  _applyThreshold(v);
}


// ── Music tab ─────────────────────────────────────────────────────────────────
async function loadMusicTracks() {
  const dir = document.getElementById('music-dir-input').value.trim();
  if (!dir) return;
  // Persist music dir immediately (before API call, so it's saved even if API fails)
  const wd = document.getElementById('js-workdir').value.trim();
  if (wd) api.put('/api/job-config', { work_dir: wd, music_dir: dir });
  if (currentJobId) api.patch(`/api/jobs/${currentJobId}/params`, { music_dir: dir });
  const data = await api.get(`/api/music-files?dir=${encodeURIComponent(dir)}`);
  if (!data) return;
  musicTracks = data;
  // Populate genre dropdown
  const genres = [...new Set(data.map(t => t.genre).filter(Boolean))].sort();
  const sel = document.getElementById('music-genre');
  const cur = sel.value;
  sel.innerHTML = '<option value="">all genres</option>';
  for (const g of genres) {
    const o = document.createElement('option');
    o.value = o.textContent = g;
    sel.appendChild(o);
  }
  if (genres.includes(cur)) sel.value = cur;
  renderMusicList();
}

function _trackVisible(t, filter, genre) {
  if (filter && !t.title.toLowerCase().includes(filter)) return false;
  if (genre && (t.genre || '').toLowerCase() !== genre) return false;
  return true;
}

function sortMusic(key) {
  if (_musicSort.key === key) _musicSort.asc = !_musicSort.asc;
  else { _musicSort.key = key; _musicSort.asc = true; }
  document.querySelectorAll('#music-list-header span[onclick]').forEach(s => {
    s.classList.toggle('sort-active', s.getAttribute('onclick') === `sortMusic('${key}')`);
    const isActive = s.classList.contains('sort-active');
    s.textContent = s.textContent.replace(/ [▲▼]$/, '');
    if (isActive) s.textContent += _musicSort.asc ? ' ▲' : ' ▼';
  });
  renderMusicList();
}

function _sortedTracks(tracks) {
  const targetFromInput = _parseTargetInput(document.getElementById('gallery-target-min')?.value);
  // Sort music by actual estimated highlight duration, not target.
  // Target is a goal for scene selection; music must match what will actually be rendered.
  const targetDur = analyzeResult?._live_est_dur
    ?? (analyzeResult?.estimated_duration_sec > 0 ? analyzeResult.estimated_duration_sec : null)
    ?? (targetFromInput > 0 ? targetFromInput : null)
    ?? 0;
  if (!_musicSort.key) {
    // Default: sort by closeness to target duration, alternating longer/shorter
    // Order: exact → +1s → -1s → +2s → -2s → ...
    if (!targetDur) return tracks;
    return [...tracks].sort((a, b) => {
      const da = (a.duration || 0) - targetDur;
      const db = (b.duration || 0) - targetDur;
      const absDa = Math.abs(da), absDb = Math.abs(db);
      if (absDa !== absDb) return absDa - absDb;
      // same absolute diff: longer before shorter
      return db - da;
    });
  }
  return [...tracks].sort((a, b) => {
    let va, vb;
    if (_musicSort.key === 'title')  { va = (a.artist || '') + a.title; vb = (b.artist || '') + b.title; }
    if (_musicSort.key === 'genre')  { va = a.genre || ''; vb = b.genre || ''; }
    if (_musicSort.key === 'dur')    { va = a.duration || 0; vb = b.duration || 0; }
    if (_musicSort.key === 'bpm')    { va = a.bpm || 0; vb = b.bpm || 0; }
    if (_musicSort.key === 'energy') { va = a.energy_norm || 0; vb = b.energy_norm || 0; }
    if (va < vb) return _musicSort.asc ? -1 : 1;
    if (va > vb) return _musicSort.asc ?  1 : -1;
    return 0;
  });
}

function renderMusicList() {
  const list = document.getElementById('music-list');
  const filter = document.getElementById('music-filter').value.toLowerCase();
  const genre  = document.getElementById('music-genre').value.toLowerCase();
  const frag = document.createDocumentFragment();
  let shown = 0;
  for (const t of _sortedTracks(musicTracks)) {
    if (!_trackVisible(t, filter, genre)) continue;
    shown++;
    const row = document.createElement('div');
    row.className = 'mt';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = musicSelected.has(t.file);
    cb.addEventListener('change', e => {
      if (e.target.checked) musicSelected.add(t.file);
      else musicSelected.delete(t.file);
      // single checked = pinned; 0 or multiple = auto-select
      if (musicSelected.size === 1) {
        pinnedTrack = [...musicSelected][0];
        const name = pinnedTrack.split('/').pop().replace(/\.[^.]+$/, '');
        const sumTrack = document.getElementById('sum-track');
        if (sumTrack) sumTrack.textContent = `✓ ${name}`;
      } else {
        pinnedTrack = null;
        const sumTrack = document.getElementById('sum-track');
        if (sumTrack) sumTrack.textContent = t_('misc.no_pin') || 'No track pinned — will auto-select.';
      }
      updateMusicCount(shown);
      updatePhaseUI();
      if (currentJobId) api.patch(`/api/jobs/${currentJobId}/params`, { music_files: [...musicSelected] });
    });
    const titleWrap = document.createElement('span');
    titleWrap.className = 'mt-title';
    if (t.artist) {
      const artist = document.createElement('span');
      artist.className = 'mt-artist';
      artist.textContent = t.artist;
      titleWrap.appendChild(artist);
    }
    const titleText = document.createElement('span');
    titleText.textContent = t.title;
    titleWrap.appendChild(titleText);
    const dur = t.duration ? fmtDur(t.duration) : '?';
    const meta = document.createElement('span');
    meta.className = 'mt-genre';
    meta.textContent = t.genre || '—';
    const durSpan = document.createElement('span');
    durSpan.className = 'mt-dur';
    durSpan.textContent = dur;
    const bpm = document.createElement('span');
    bpm.className = 'mt-bpm';
    bpm.textContent = t.bpm ? `${Math.round(t.bpm)} BPM` : '';
    const energyWrap = document.createElement('div');
    energyWrap.className = 'mt-energy';
    const energyFill = document.createElement('div');
    energyFill.className = 'mt-energy-fill';
    energyFill.style.width = `${Math.round((t.energy_norm ?? 0) * 100)}%`;
    energyWrap.appendChild(energyFill);
    const playBtn = document.createElement('button');
    playBtn.className = 'mt-play';
    playBtn.textContent = '▶';
    const seek = document.createElement('input');
    seek.type = 'range';
    seek.className = 'mt-seek';
    seek.min = 0; seek.value = 0; seek.max = 300;
    playBtn.onclick = e => { e.stopPropagation(); _playTrack(t.file, playBtn, seek); };
    seek.onclick = e => e.stopPropagation();
    titleWrap.appendChild(seek);
    row.appendChild(cb);
    row.appendChild(playBtn);
    row.appendChild(titleWrap);
    row.appendChild(meta);
    row.appendChild(durSpan);
    row.appendChild(bpm);
    row.appendChild(energyWrap);
    row.onclick = e => { if (e.target !== cb && e.target !== playBtn && e.target !== seek) { cb.checked = !cb.checked; cb.dispatchEvent(new Event('change')); } };
    frag.appendChild(row);
  }
  list.innerHTML = '';
  list.appendChild(frag);
  updateMusicCount(shown);
}

function updateMusicCount(shown) {
  const s = musicSelected.size, t = musicTracks.length;
  const shownTxt = (shown != null && shown !== t) ? ` · ${shown} shown` : '';
  const allTxt = s === 0 && t > 0 ? ' (no filter = all)' : '';
  document.getElementById('music-count').textContent = `${s} / ${t} selected${shownTxt}${allTxt}`;
  const btnTgt = document.getElementById('btn-set-target-dur');
  if (btnTgt) btnTgt.style.display = s === 1 ? '' : 'none';
}

async function rebuildMusicIndex() {
  const dir = document.getElementById('music-dir-input').value.trim();
  if (!dir) return;
  const btn  = document.getElementById('btn-music-rebuild');
  const wrap = document.getElementById('music-rebuild-progress');
  const bar  = document.getElementById('music-rebuild-bar');
  btn.textContent = '↺ …';
  btn.disabled = true;
  bar.style.width = '0%';
  wrap.style.display = '';

  const force       = document.getElementById('music-force').checked;
  const forceGenres = document.getElementById('music-force-genres').checked;

  let ok = false;
  try {
    const startResp = await fetch('/api/music-rebuild', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dir, force, force_genres: forceGenres }),
    });
    const { task_id } = await startResp.json();

    // Poll progress every 500ms
    while (true) {
      await new Promise(r => setTimeout(r, 500));
      const status = await fetch(`/api/music-rebuild-status/${task_id}`).then(r => r.json());
      if (status.total > 0) {
        const p = Math.min(99, Math.round(status.progress / status.total * 100));
        bar.style.width = p + '%';
      } else if (!status.done) {
        // Indeterminate: slowly crawl toward 30% while waiting for first file
        const cur = parseFloat(bar.style.width) || 0;
        bar.style.width = Math.min(cur + (30 - cur) * 0.15, 30) + '%';
      }
      if (status.done) { ok = status.ok; break; }
    }
  } catch(e) {
    alert('Rebuild error: ' + e);
  }

  btn.textContent = TRANS[currentLang]?.labels?.['btn.rebuild_index'] || '↺ Update index';
  btn.disabled = false;
  if (ok) {
    bar.style.width = '100%';
    setTimeout(() => { wrap.style.display = 'none'; bar.style.width = '0%'; }, 1500);
    await loadMusicTracks();
  } else {
    bar.style.background = '#f64';
    setTimeout(() => { wrap.style.display = 'none'; bar.style.background = ''; bar.style.width = '0%'; }, 2000);
  }
}

function setAllMusic(checked) {
  const filter = document.getElementById('music-filter').value.toLowerCase();
  const genre  = document.getElementById('music-genre').value.toLowerCase();
  for (const t of musicTracks) {
    if (!_trackVisible(t, filter, genre)) continue;
    if (checked) musicSelected.add(t.file);
    else musicSelected.delete(t.file);
  }
  renderMusicList();
}

function setTargetFromSelectedTrack() {
  if (musicSelected.size === 0) return;
  const file = [...musicSelected][0];
  const track = musicTracks.find(t => t.file === file);
  if (!track?.duration) return;
  const mins = track.duration / 60;
  const mm = Math.floor(mins), ss = Math.round((mins - mm) * 60);
  const inp = document.getElementById('gallery-target-min');
  if (inp) {
    inp.value = `${mm}:${String(ss).padStart(2,'0')}`;
  }
  autoTargetThreshold(mins);
  const wd = document.getElementById('js-workdir')?.value.trim();
  if (wd) api.put('/api/job-config', { work_dir: wd, target_minutes: mins });
  if (currentJobId) api.patch(`/api/jobs/${currentJobId}/params`, { target_minutes: mins });
  switchTab('gallery');
}

async function populateJobSettings(params) {
  const sv = (id, v) => { if (v != null && v !== '') document.getElementById(id).value = v; };
  const sc = (id, v) => { if (v != null) document.getElementById(id).checked = !!v; };

  _setWorkdir(params.work_dir);
  s3SectionInit();
  const cfg = await api.get(`/api/job-config?dir=${encodeURIComponent(params.work_dir)}`);
  const _camOffsets = params.cam_offsets || cfg?.cam_offsets;
  await setCamsList('js-cam-list', params.cameras || [params.cam_a, params.cam_b].filter(Boolean), _camOffsets);
  sv('js-title',    params.title);
  sc('js-no-intro',    params.no_intro);
  sc('js-no-music',    params.no_music);
  sc('js-shorts-text', params.shorts_text);
  // Music tab state (set from params first; cfg.music_dir will override below if set)
  if (params.music_dir) document.getElementById('music-dir-input').value = params.music_dir;
  if (params.music_files?.length) musicSelected = new Set(params.music_files);
  else musicSelected = new Set();

  // Load config.ini defaults and prompts
  let th = params.threshold, ms = params.max_scene, pf = params.per_file;
  if (cfg) {
    if (th == null) th = cfg.threshold;
    if (ms == null) ms = cfg.max_scene;
    if (pf == null) pf = cfg.per_file;
    // cfg.music_dir only as fallback when params doesn't have it
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
  // Target minutes — stored in gallery field as M:SS
  const tm = params.target_minutes ?? cfg?.target_minutes;
  if (tm != null) {
    const el = document.getElementById('gallery-target-min');
    if (el) { const mm = Math.floor(tm); const ss = Math.round((tm - mm) * 60); el.value = `${mm}:${String(ss).padStart(2,'0')}`; }
  }
  // Auto-count source files (non-blocking); restore saved values after
  _sceneParamManual['js'] = false;
  fillSceneParams('js').then(() => {
    if (ms != null) { document.getElementById('js-max-scene').value = ms; _sceneParamManual['js'] = false; }
    if (pf != null) { document.getElementById('js-per-file').value  = pf; _sceneParamManual['js'] = false; }
  });
  // Auto-load music index if dir is known
  if (document.getElementById('music-dir-input').value.trim()) loadMusicTracks();
}

function readJobSettings() {
  const gv = id => document.getElementById(id).value.trim();
  const gc = id => document.getElementById(id).checked;
  const p = { work_dir: gv('js-workdir') };
  if (_galleryThreshold !== null) p.threshold = _galleryThreshold;
  const ms = gv('js-max-scene'); if (ms) p.max_scene = parseFloat(ms);
  const pf = gv('js-per-file');  if (pf) p.per_file  = parseFloat(pf);
  const sdt = gv('js-sd-threshold'); p.sd_threshold = sdt ? parseFloat(sdt) : 20;
  const sdm = gv('js-sd-min-scene'); if (sdm) p.sd_min_scene  = sdm;
  const cameras = readCamsList('js-cam-list'); if (cameras.length) p.cameras = cameras;
  const offsets = readCamOffsets('js-cam-list'); if (offsets) p.cam_offsets = offsets;
  const ti = gv('js-title');     if (ti) p.title = ti;
  p.no_intro    = gc('js-no-intro');
  p.no_music    = gc('js-no-music');
  p.shorts_text = gc('js-shorts-text');
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

// ── Results ───────────────────────────────────────────────────────────────────
async function deleteResultFile(jobId, filename, card) {
  if (!await showConfirm('Delete file', `Delete ${filename} from disk?`, null, 'Delete')) return;
  try {
    const resp = await api.del(`/api/jobs/${jobId}/result-file?filename=${encodeURIComponent(filename)}`);
    if (resp?.ok) {
      const video = document.getElementById('video-player');
      if (card.classList.contains('playing')) { video.pause(); video.src = ''; document.getElementById('video-wrap').style.display = 'none'; }
      card.remove();
    } else {
      alert('Delete failed: ' + (resp?.detail || JSON.stringify(resp)));
    }
  } catch(e) {
    alert('Delete error: ' + e);
  }
}

async function loadResults(jobId) {
  const data = await api.get(`/api/jobs/${jobId}/result`);
  if (!data) return;
  const container = document.getElementById('rf-files');
  container.innerHTML = '';
  for (const [name, info] of Object.entries(data)) {
    const filePath = info.url;  // url is now a direct path e.g. /data/2025/.../video.mp4
    const div = document.createElement('div');
    div.className = 'rf';
    const rfName = document.createElement('div'); rfName.className = 'rf-name'; rfName.textContent = name;
    const rfMeta = document.createElement('div'); rfMeta.className = 'rf-meta';
    const rfSize = document.createElement('span'); rfSize.className = 'rf-size'; rfSize.textContent = `${info.size_mb} MB`;
    const rfDur  = document.createElement('span'); rfDur.className  = 'rf-dur';
    rfDur.textContent = info.duration_sec ? fmtDur(info.duration_sec) : '';
    rfMeta.appendChild(rfSize); rfMeta.appendChild(rfDur);
    div.appendChild(rfName); div.appendChild(rfMeta);
    const btnRow = document.createElement('div');
    btnRow.style.cssText = 'display:flex;gap:5px;align-items:center;margin-top:5px;flex-wrap:wrap';
    const dlBtn = document.createElement('a');
    dlBtn.className = 'btn-sm';
    dlBtn.textContent = '▼';
    dlBtn.title = 'Download';
    dlBtn.href = '/api/file?path=' + encodeURIComponent(filePath) + '&dl=1';
    dlBtn.download = name;
    dlBtn.style.textDecoration = 'none';
    dlBtn.onclick = e => e.stopPropagation();
    btnRow.appendChild(dlBtn);
    const isShort = /short/i.test(name);
    const ytBtn = document.createElement('button');
    ytBtn.className = 'btn-sm';
    ytBtn.textContent = isShort ? '▲ YT Shorts' : '▲ YT';
    ytBtn.onclick = e => { e.stopPropagation(); isShort ? ytShortsModalOpen(filePath, name) : ytModalOpen(filePath, name, info.yt_url || ''); };
    btnRow.appendChild(ytBtn);
    const s3Btn = document.createElement('button');
    s3Btn.className = 'btn-sm';
    s3Btn.textContent = '▲ S3';
    s3Btn.title = 'Upload to S3';
    s3Btn.onclick = e => { e.stopPropagation(); s3ModalOpen(filePath, name); };
    api.get('/api/s3/status').then(s => { if (!s?.configured) s3Btn.style.display = 'none'; });
    btnRow.appendChild(s3Btn);
    if (info.yt_url) {
      const ytLink = document.createElement('a');
      ytLink.href = info.yt_url;
      ytLink.target = '_blank';
      ytLink.textContent = '▶ YouTube';
      ytLink.style.cssText = 'font-size:11px;color:var(--green);text-decoration:none;';
      ytLink.onclick = e => e.stopPropagation();
      btnRow.appendChild(ytLink);
    }
    div.appendChild(btnRow);
    const delBtn = document.createElement('button');
    delBtn.className = 'rf-delete';
    delBtn.title = name;
    delBtn.textContent = '✕';
    delBtn.dataset.jobId = jobId;
    delBtn.dataset.filename = name;
    div.appendChild(delBtn);
    div.onclick = () => playVideo(info.url, div);
    container.appendChild(div);
  }
  container.querySelector('.rf')?.classList.add('playing');

  container.onclick = e => {
    const btn = e.target.closest('.rf-delete');
    if (!btn) return;
    e.stopPropagation();
    const card = btn.closest('.rf');
    deleteResultFile(btn.dataset.jobId, btn.dataset.filename, card);
  };
}

function playVideo(url, card) {
  document.querySelectorAll('.rf').forEach(c=>c.classList.remove('playing'));
  if (card) card.classList.add('playing');
  const wrap = document.getElementById('video-wrap');
  const video = document.getElementById('video-player');
  wrap.style.display = 'block';
  video.src = url;
  video.load();
}

// ── Dir browser ───────────────────────────────────────────────────────────────
let _dirSelectCb = null;
function _showDataRootModal() {
  const hint = document.getElementById('dir-hint');
  hint.textContent = 'Select the root directory where your video recordings are stored.';
  hint.style.display = '';
  openDirBrowser(null, async p => {
    hint.style.display = 'none';
    const r = await api.post('/api/config/data-root', { path: p });
    if (r?.ok) location.reload();
    else alert('Failed to save data root: ' + (r?.detail || 'unknown error'));
  });
}

async function openDirBrowser(startPath, callback) {
  _dirSelectCb = callback || null;
  document.getElementById('dir-modal').classList.add('open');
  await browseTo((startPath || '').trim() || null);
}
function closeDirBrowser() {
  document.getElementById('dir-modal').classList.remove('open');
  const hint = document.getElementById('dir-hint');
  hint.style.display = 'none';
  hint.textContent = '';
  _dirSelectCb = null;
}
function selectCurrentDir() {
  if (_dirSelectCb) _dirSelectCb(browsePath);
  closeDirBrowser();
}
async function createFolder() {
  const name = prompt('Folder name:');
  if (!name || !name.trim()) return;
  const data = await api.post('/api/mkdir', { path: browsePath, name: name.trim() });
  if (data?.path) {
    await browseTo(data.path);
  } else {
    alert('Could not create folder');
  }
}

function startUpload(inputEl, prefix) {
  const files = [...inputEl.files];
  inputEl.value = '';
  if (!files.length) return;
  const workDir = document.getElementById(prefix + '-workdir').value.trim();
  if (!workDir) { alert('Set working directory first'); return; }

  const wrap  = document.getElementById(prefix + '-upload-wrap');
  const nameEl  = document.getElementById(prefix + '-upload-name');
  const speedEl = document.getElementById(prefix + '-upload-speed');
  const pctEl   = document.getElementById(prefix + '-upload-pct');
  const bar     = document.getElementById(prefix + '-upload-bar');

  wrap.style.display = '';
  let idx = 0;

  function uploadNext() {
    if (idx >= files.length) {
      nameEl.textContent = `Done — ${files.length} file${files.length > 1 ? 's' : ''} uploaded`;
      speedEl.textContent = '';
      pctEl.textContent = '';
      bar.style.width = '100%';
      setTimeout(() => { wrap.style.display = 'none'; bar.style.width = '0%'; }, 3000);
      return;
    }
    const file = files[idx++];
    nameEl.textContent = `${idx}/${files.length}: ${file.name}`;
    speedEl.textContent = '';
    pctEl.textContent = '0%';
    bar.style.width = '0%';

    const formData = new FormData();
    formData.append('file', file);
    formData.append('work_dir', workDir);

    const xhr = new XMLHttpRequest();
    let lastLoaded = 0, lastTime = Date.now();

    xhr.upload.onprogress = e => {
      if (!e.lengthComputable) return;
      const pct = Math.round(e.loaded / e.total * 100);
      pctEl.textContent = pct + '%';
      bar.style.width = pct + '%';
      const now = Date.now(), dt = (now - lastTime) / 1000;
      if (dt >= 0.5) {
        const mbps = (e.loaded - lastLoaded) / dt / 1048576;
        speedEl.textContent = mbps.toFixed(1) + ' MB/s';
        lastLoaded = e.loaded; lastTime = now;
      }
    };
    xhr.onload = () => {
      if (xhr.status === 200) { uploadNext(); }
      else { alert('Upload failed: ' + xhr.responseText); wrap.style.display = 'none'; }
    };
    xhr.onerror = () => { alert('Upload error'); wrap.style.display = 'none'; };
    xhr.open('POST', '/api/upload');
    xhr.send(formData);
  }
  uploadNext();
}
// ── S3 source (Settings) ──────────────────────────────────────────────────────
let _s3FetchEs = null;

