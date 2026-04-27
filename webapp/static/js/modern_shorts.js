// modern_shorts.js — Shorts generation modal

async function openShortsModal() {
  if (typeof _jobId === 'undefined' || !_jobId) return;
  const modal = document.getElementById('m-shorts-modal');
  if (!modal) return;
  document.getElementById('m-shorts-status').textContent = '';
  document.getElementById('m-shorts-btn').disabled = false;

  const [job, cfg] = await Promise.all([
    window._modernApi.get(`/api/jobs/${_jobId}`),
    window._modernApi.get(`/api/job-config?dir=${encodeURIComponent(_workDir || '')}`).catch(() => null),
  ]);

  if (job?.params) {
    const p = job.params;
    const setChk = (id, val) => { const el = document.getElementById(id); if (el) el.checked = !!val; };
    setChk('m-shorts-text',     p.shorts_text);
    setChk('m-shorts-multicam', p.shorts_multicam);
    setChk('m-shorts-beat',     p.shorts_beat_sync);
    setChk('m-shorts-best',     p.shorts_best);

    const cams = p.cameras || [p.cam_a, p.cam_b].filter(Boolean);
    const mcRow = document.getElementById('m-shorts-multicam-row');
    if (mcRow) mcRow.style.display = cams.length > 1 ? '' : 'none';

    // Load subfolder checkboxes from shorts_music_dir (Settings)
    const shortsMusicBase = cfg?.shorts_music_dir || p.shorts_music_dir || '';
    const selectedDirs = p.shorts_music_dirs
      ? (Array.isArray(p.shorts_music_dirs) ? p.shorts_music_dirs : [p.shorts_music_dirs])
      : [];
    await _loadShortsDirsList(shortsMusicBase, selectedDirs);
  }

  modal.style.display = 'flex';
}
window.openShortsModal = openShortsModal;

function closeShortsModal() {
  const modal = document.getElementById('m-shorts-modal');
  if (modal) modal.style.display = 'none';
}
window.closeShortsModal = closeShortsModal;

// ── Subfolder checkbox list ───────────────────────────────────────────────────
async function _loadShortsDirsList(baseDir, selectedDirs) {
  const list  = document.getElementById('m-shorts-dirs-list');
  const empty = document.getElementById('m-shorts-dirs-empty');
  if (!list) return;
  list.innerHTML = '';

  if (!baseDir) {
    if (empty) { empty.style.display = ''; list.style.display = 'none'; }
    return;
  }
  if (empty) { empty.style.display = 'none'; list.style.display = ''; }

  // Fetch shorts_used counts for badge display
  const usedRaw = await window._modernApi.get('/api/music/used-tracks').catch(() => null) || {};

  const data = await window._modernApi.get(`/api/browse?path=${encodeURIComponent(baseDir)}`);
  const subdirs = (data?.entries || []).filter(e => e.is_dir);

  if (!subdirs.length) {
    // No subdirs — offer the base dir itself as single option
    _appendDirRow(list, baseDir, baseDir.split('/').pop() || baseDir, selectedDirs, usedRaw);
    return;
  }

  for (const e of subdirs) {
    _appendDirRow(list, e.path, e.name, selectedDirs, usedRaw);
  }
}

function _appendDirRow(list, path, label, selectedDirs, usedRaw) {
  const usedCount = Object.keys(usedRaw).filter(k => k.startsWith(path)).length;

  const row = document.createElement('label');
  row.className = 'm-shorts-dir-row';

  const cb = document.createElement('input');
  cb.type = 'checkbox';
  cb.value = path;
  cb.checked = selectedDirs.includes(path);

  const name = document.createElement('span');
  name.textContent = '📁 ' + label;
  name.style.flex = '1';

  row.appendChild(cb);
  row.appendChild(name);

  if (usedCount > 0) {
    const badge = document.createElement('span');
    badge.className = 'm-shorts-dir-used';
    badge.textContent = `${usedCount} used`;
    row.appendChild(badge);
  }

  list.appendChild(row);
}

// ── Render ────────────────────────────────────────────────────────────────────
async function renderShorts() {
  if (typeof _jobId === 'undefined' || !_jobId) {
    alert('No project selected.'); return;
  }

  const count    = parseInt(document.getElementById('m-shorts-count')?.value) || 1;
  const text     = document.getElementById('m-shorts-text')?.checked ?? false;
  const multicam = document.getElementById('m-shorts-multicam')?.checked ?? false;
  const beat     = document.getElementById('m-shorts-beat')?.checked ?? false;
  const best     = document.getElementById('m-shorts-best')?.checked ?? false;

  // Collect checked dirs
  const checkedDirs = Array.from(
    document.querySelectorAll('#m-shorts-dirs-list input[type=checkbox]:checked')
  ).map(cb => cb.value).filter(Boolean);

  const btn    = document.getElementById('m-shorts-btn');
  const status = document.getElementById('m-shorts-status');
  if (btn)    btn.disabled = true;
  if (status) status.textContent = 'Starting…';

  const params = {
    shorts_text:      text,
    shorts_multicam:  multicam,
    shorts_beat_sync: beat,
    shorts_best:      best,
    shorts_music_dirs: checkedDirs,
  };

  await window._modernApi.patch(`/api/jobs/${_jobId}/params`, params);

  if (typeof _connectJobProgress === 'function') _connectJobProgress(_jobId);

  let data = null;
  try {
    const r = await fetch(`/api/jobs/${_jobId}/render-short`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ count, best }),
    });
    data = r.ok ? await r.json() : null;
  } catch { data = null; }

  if (!data?.id) {
    if (btn)    btn.disabled = false;
    if (status) status.textContent = '✗ Failed to start';
    return;
  }

  closeShortsModal();
}
window.renderShorts = renderShorts;
