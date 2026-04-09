function _computePerFileCuts() {
  _perFileCuts = new Set();
  if (_galleryThreshold === null) return;
  const perFile = currentJobPerFile ||
    parseFloat(document.getElementById('js-per-file')?.value || '0') || 0;
  if (perFile) {
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
  }
  _computeBalancedScenes();
  if (_balancedScenes === null) _computeGapExclusions();
}

// Passes threshold + per-file budget checks (no camera balance). Used as input for balancing.
function _passesPreBalance(f) {
  const ov = manualOverrides[f.scene];
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
  _computeGapExclusions();
}

// Gap filter: mirror of select_scenes.py MIN_GAP_SEC logic.
// Sorts auto-included scenes by timestamp, greedily excludes those too close.
// Manual force-includes always bypass the gap filter.
function _computeGapExclusions() {
  _gapExcluded = new Set();
  const minGap = parseFloat(document.getElementById('min-gap-input')?.value) || 0;
  if (minGap <= 0) return;

  // Collect auto-included scenes (not manual overrides) with timestamps
  const maxSec = currentJobMaxScene || parseFloat(document.getElementById('js-max-scene')?.value || '0') || 10;
  const candidates = framesData.filter(f => {
    if (manualOverrides[f.scene] === 'include' || manualOverrides[f.scene] === 'exclude') return false;
    if (f.duplicate) return false;
    if (_balancedScenes !== null) return _balancedScenes.has(f.scene);
    if (_galleryThreshold !== null ? f.score < _galleryThreshold : false) return false;
    return !_perFileCuts.has(f.scene);
  }).filter(f => f.file_start != null);

  // Sort by absolute timestamp
  candidates.sort((a, b) => a.file_start - b.file_start);

  let lastEnd = null;
  for (const f of candidates) {
    const ts = f.file_start;
    const take = Math.min(f.duration ?? maxSec, maxSec);
    if (lastEnd === null || (ts - lastEnd) >= minGap) {
      lastEnd = ts + take;
    } else {
      _gapExcluded.add(f.scene);
    }
  }
}

function isIncluded(f) {
  const ov = manualOverrides[f.scene];
  if (ov === 'include') return true;
  if (ov === 'exclude') return false;
  if (f.duplicate) return false;
  // In dual-cam mode use the pre-computed balanced set (includes boosted below-threshold scenes)
  if (_balancedScenes !== null) {
    if (!_balancedScenes.has(f.scene)) return false;
  } else {
    if (_galleryThreshold !== null ? f.score < _galleryThreshold : f.score < parseFloat(document.getElementById('threshold-val').value)) return false;
    if (_perFileCuts.has(f.scene)) return false;
  }
  return !_gapExcluded.has(f.scene);
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
  return true;
}

function _applyGalleryFilter() {
  document.querySelectorAll('#frames-grid .fc').forEach(card => {
    const f = framesData.find(x => x.scene === card.dataset.scene);
    card.style.display = (f && _sceneMatchesFilters(f)) ? '' : 'none';
  });
}

function onGalleryFilter() {
  _applyGalleryFilter();
}

function onMinGapChange() {
  const val = parseFloat(document.getElementById('min-gap-input')?.value) || 0;
  if (!currentJobId) return;
  api.patch(`/api/jobs/${currentJobId}/params`, { min_gap_sec: val });
  _overridesChangedSinceRender = true;
  _computeGapExclusions();
  _refreshGalleryClasses();
  calculateGalleryStats();
  _scheduleEstimate();
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
    const sceneNum = (f.scene.match(/-scene-(\d+)$/) || [])[1] || '';
    const sceneLabel = (() => {
      if (f.file_start) {
        const d = new Date(f.file_start * 1000);
        const pad = n => String(n).padStart(2, '0');
        return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())} #${sceneNum}`;
      }
      const m = f.scene.match(/(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2}).*-scene-(\d+)$/);
      return m ? `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}:${m[6]} #${m[7]}` : f.scene.split('/').pop().slice(-22);
    })();
    const isDup = f.duplicate && !ov;
    if (isDup) {
      card.className = 'fc excluded';
      card.title = `Score: ${f.score.toFixed(3)} (near-duplicate removed — click to force include)`;
    }
    const limitBadge = limited ? `<span class="fc-limit-badge">limit</span>` : '';
    const dupBadge   = isDup   ? `<span class="fc-limit-badge" style="background:var(--muted)">dup</span>` : '';
    const _maxSec = currentJobMaxScene || parseFloat(document.getElementById('js-max-scene')?.value || '0') || 10;
    const effDur = f.duration != null ? Math.min(f.duration, _maxSec) : null;
    const durBadge = `<span class="fc-dur">${effDur != null ? effDur.toFixed(1) + 's' : '?'}</span>`;
    card.innerHTML = `<img src="${_cachedSrc(f.frame_url)}" loading="lazy" onerror="this.style.display='none'">
      <div class="fc-info"><span class="fc-score">${f.score.toFixed(3)}</span>${durBadge}<span class="fc-name"></span>${limitBadge}${dupBadge}</div>`;
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
  // Compute average duration for scenes with known duration (fallback for unknown)
  let _knownTot = 0, _knownCnt = 0;
  for (const f of scenes) {
    if (f.duration == null) continue;
    _knownTot += Math.min(f.duration, maxSec); _knownCnt++;
  }
  const avgDur = _knownCnt > 0 ? _knownTot / _knownCnt : maxSec * 0.5;
  const bySource = new Map();
  for (const f of scenes) {
    const src = f.scene.replace(/-scene-\d+$/, '');
    if (!bySource.has(src)) bySource.set(src, []);
    bySource.get(src).push(f);
  }
  let total = 0;
  for (const group of bySource.values()) {
    let fileTot = 0;
    for (const f of group.sort((a,b) => (b.score??0)-(a.score??0))) {
      let take = Math.min(f.duration ?? avgDur, maxSec);
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

  // Scale from the server's last dry-run estimate proportionally by current scene count.
  // scaledCount already reflects both threshold changes and manual overrides.
  const serverDur  = analyzeResult?.estimated_duration_sec;
  const serverMain = analyzeResult?.estimated_main_scenes ?? analyzeResult?.estimated_scenes;
  if (serverDur > 0 && serverMain > 0) {
    const duration = Math.max(0, Math.round(serverDur * scaledCount / serverMain));
    return { scenes: balanced, duration, scaledCount, camRatio };
  }

  // Fallback used only before any binary-search/estimate has run.
  const mainDur = _estimateDuration(balanced);
  const duration = _estimateTotalDuration(mainDur, balanced.length);
  return { scenes: balanced, duration, scaledCount, camRatio };
}

// Returns intro+outro duration in seconds (0 when no_intro is checked).
// analyzeResult.intro_dur_sec is set from /api/analyze-result (default 3s → 2*3=6s).
function _introDurSec() {
  if (document.getElementById('js-no-intro')?.checked) return 0;
  return 2 * (analyzeResult?.intro_dur_sec ?? 3.0);
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
    analyzeResult._live_est_dur = analyzeResult.actual_duration_sec;
    return;
  }
  if (useEstimated) {
    const _ids = _introDurSec();
    document.getElementById('gallery-stats-text').textContent =
      `${analyzeResult.estimated_scenes} / ${framesData.length} scenes · ${fmtDur(analyzeResult.estimated_duration_sec + _ids, '~')} est.`;
    if (analyzeResult) analyzeResult._live_est_dur = analyzeResult.estimated_duration_sec + _ids;
    return;
  }
  const est = _balancedEstimate();
  const hasDur = framesData.some(f => f.duration != null);
  const countTxt = `${est.scaledCount} / ${framesData.length} scenes`;
  let durTxt = '';
  if (hasDur) {
    const _ids = _introDurSec();
    const estTotal = est.duration + _ids;
    durTxt = ` · ${fmtDur(estTotal, '~')} est.`;
    if (analyzeResult) analyzeResult._live_est_dur = estTotal;
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
  data.no_intro        = gc('js-no-intro') || false;
  data.no_music        = gc('js-no-music') || false;
  data.shorts_text     = gc('js-shorts-text') || false;
  data.shorts_multicam    = gc('js-shorts-multicam') || false;
  data.shorts_ncs         = gc('js-shorts-ncs') || false;
  data.shorts_crop_offsets = document.getElementById('js-shorts-crop-offsets')?.value?.trim() || '';
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
    const mg  = parseFloat(document.getElementById('min-gap-input')?.value) || 0;
    const estimateParams = { threshold: thr, max_scene: ms, per_file: pf };
    if (mg > 0) estimateParams.min_gap_sec = mg;
    const res = await api.post(`/api/jobs/${currentJobId}/estimate`, estimateParams);
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
    analyzeResult._live_est_dur = analyzeResult.actual_duration_sec;
  } else if (useEstimated) {
    scenes  = analyzeResult.estimated_scenes;
    const _ids = _introDurSec();
    const _estTotal = analyzeResult.estimated_duration_sec + _ids;
    durStr  = fmtDur(_estTotal, '~');
    analyzeResult._live_est_dur = _estTotal;
  } else {
    const est = _balancedEstimate();
    scenes  = est.scaledCount;
    const hasDur = framesData.some(f => f.duration != null);
    const _ids = _introDurSec();
    const estDur = Math.round(est.duration + _ids);
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
  const savedThr      = _galleryThreshold;
  const savedBalanced = _balancedScenes;
  const savedCuts     = _perFileCuts;

  _galleryThreshold = thr;
  _computePerFileCuts(); // updates _perFileCuts + _balancedScenes for this thr

  const balanced = framesData.filter(f => isIncluded(f));
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
  const mg = parseFloat(document.getElementById('min-gap-input')?.value) || 0;

  // Subtract intro+outro duration so the search targets clips-only, giving final video ≈ targetSec.
  const introDur = _introDurSec();
  const adjustedTargetSec = Math.max(targetSec - introDur, 1);

  let res = null;
  let _searchId = null;
  try {
    const searchBody = { target_sec: adjustedTargetSec, max_scene: ms, per_file: pf };
    if (mg > 0) searchBody.min_gap_sec = mg;
    const r = await fetch(`/api/jobs/${currentJobId}/find-threshold`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(searchBody),
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
    _setGallerySearchStatus(null, 0);
  }

  let warnMsg = '';

  if (res?.threshold != null) {
    const thr = Math.round(res.threshold * 1000) / 1000;
    document.getElementById('threshold-val').value = thr.toFixed(3);

    if (!analyzeResult) analyzeResult = {};
    analyzeResult.estimated_scenes       = res.scenes;
    analyzeResult.estimated_duration_sec = res.duration_sec;
    analyzeResult.estimated_main_scenes  = res.main_scenes;
    analyzeResult.cam_ratio              = res.cam_ratio;
    analyzeResult.auto_threshold         = thr;

    _applyThreshold(thr);
    if (currentJobId) {
      api.patch(`/api/jobs/${currentJobId}/params`, { threshold: thr });
      const wd2 = document.getElementById('js-workdir')?.value.trim();
      if (wd2) api.put('/api/job-config', { work_dir: wd2, threshold: thr });
    }

    if (res.duration_sec + introDur < targetSec - 10) {
      const gotTotal = res.duration_sec + introDur;
      const gotMin = Math.floor(gotTotal / 60), gotS = Math.round(gotTotal % 60);
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
  calculateGalleryStats();
  if (_thresholdRafId) cancelAnimationFrame(_thresholdRafId);
  _thresholdRafId = requestAnimationFrame(() => {
    _thresholdRafId = null;
    _refreshGalleryClasses();
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
