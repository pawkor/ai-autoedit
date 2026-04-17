// Preview tab — dry-run render order with click-to-ban

let _previewSequence = [];
let _prevPreviewScenes = []; // scene names from last run, by slot index

async function runPreviewSequence() {
  if (!currentJobId) return;
  const btn    = document.getElementById('btn-preview-run');
  const status = document.getElementById('preview-status');
  const grid   = document.getElementById('preview-grid');

  const L = () => TRANS[currentLang]?.labels || TRANS.en.labels;
  btn.disabled = true;
  btn.textContent = L()['misc.preview_running'] || '⏳ Running…';
  status.textContent = L()['misc.preview_calculating'] || 'Calculating scene order (no encoding)…';
  grid.innerHTML = '';

  try {
    const data = await api.post(`/api/jobs/${currentJobId}/preview-sequence`, {});
    if (data?.detail) throw new Error(data.detail);

    const prevScenes = _previewSequence.map(s => s.scene);
    _previewSequence = data.sequence || [];
    _prevPreviewScenes = prevScenes;
    status.textContent = `${_previewSequence.length} slots · click scene to ban · re-run to refresh`;
    _renderPreviewGrid();
  } catch (e) {
    status.textContent = '⚠ ' + (e.message || 'Error');
  } finally {
    btn.disabled = false;
    btn.textContent = (TRANS[currentLang]?.labels || TRANS.en.labels)['btn.preview_run'] || '▶ Preview render order';
  }
}

function _renderPreviewGrid() {
  const grid = document.getElementById('preview-grid');
  const banCount = document.getElementById('preview-ban-count');
  grid.innerHTML = '';
  const frag = document.createDocumentFragment();
  let bannedCount = 0;

  _previewSequence.forEach((slot, idx) => {
    const banned = manualOverrides[slot.scene] === 'ban';
    if (banned) bannedCount++;
    const isNew = _prevPreviewScenes.length > 0
      && _prevPreviewScenes[idx] !== undefined
      && _prevPreviewScenes[idx] !== slot.scene;

    const card = document.createElement('div');
    card.style.cssText = `
      position:relative; cursor:pointer; border-radius:6px; overflow:hidden;
      border:2px solid ${banned ? 'var(--red,#ef4444)' : isNew ? '#22c55e' : 'transparent'};
      background:var(--bg2); opacity:${banned ? '0.4' : '1'};
      transition:border-color .15s,opacity .15s;
    `;
    card.title = banned
      ? `#${idx+1} ${slot.scene} — BANNED (click to unban)`
      : `#${idx+1} ${slot.scene}\nScore: ${slot.clip_score.toFixed(3)}  Energy: ${slot.energy.toFixed(2)}\nDuration: ${slot.duration.toFixed(1)}s  Start: ${_fmtTime(slot.music_start)}\nClick to ban`;

    // Thumbnail
    const img = document.createElement('img');
    img.src = slot.frame_url || '';
    img.style.cssText = 'width:100%;aspect-ratio:16/9;object-fit:cover;display:block';
    img.onerror = () => { img.style.display = 'none'; };

    // Slot number badge
    const numBadge = document.createElement('div');
    numBadge.style.cssText = `
      position:absolute;top:4px;left:4px;background:rgba(0,0,0,.7);
      color:#fff;font-size:10px;padding:1px 5px;border-radius:3px;font-family:var(--mono);
    `;
    numBadge.textContent = `#${idx+1}`;

    // New replacement dot
    if (isNew) {
      const newDot = document.createElement('div');
      newDot.style.cssText = `
        position:absolute;bottom:26px;right:4px;
        width:8px;height:8px;border-radius:50%;background:#22c55e;
        box-shadow:0 0 4px #22c55e;
      `;
      newDot.title = `Replaced: was ${_prevPreviewScenes[idx]?.split('/').pop()?.slice(-20)}`;
      card.appendChild(newDot);
    }

    // Energy badge
    const eBadge = document.createElement('div');
    eBadge.style.cssText = `
      position:absolute;top:4px;right:4px;
      background:${slot.energy > 0.65 ? '#ef4444' : slot.energy < 0.35 ? '#3b82f6' : '#f59e0b'};
      color:#fff;font-size:9px;padding:1px 4px;border-radius:3px;
    `;
    eBadge.textContent = slot.energy > 0.65 ? 'fast' : slot.energy < 0.35 ? 'slow' : 'mid';

    // Info bar
    const info = document.createElement('div');
    info.style.cssText = 'padding:3px 6px;font-size:10px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;background:var(--bg2)';
    // Short scene name: last 2 parts of stem
    const parts = slot.scene.replace(/-(scene|clip)-\d+$/, '').split(/[_-]/);
    info.textContent = `${_fmtTime(slot.music_start)} · ${slot.duration.toFixed(1)}s`;

    // Ban label overlay
    const banLabel = document.createElement('div');
    banLabel.style.cssText = `
      display:${banned ? 'flex' : 'none'};position:absolute;inset:0;
      align-items:center;justify-content:center;
      background:rgba(239,68,68,.15);color:var(--red,#ef4444);
      font-size:13px;font-weight:700;letter-spacing:.05em;
    `;
    banLabel.textContent = 'BANNED';

    card.appendChild(img);
    card.appendChild(numBadge);
    card.appendChild(eBadge);
    card.appendChild(info);
    card.appendChild(banLabel);

    card.addEventListener('click', () => _previewToggleBan(slot.scene, idx));
    if (slot.clip_path) {
      card.addEventListener('mouseenter', () => _showInlinePreview(card, slot.clip_path, 400));
      card.addEventListener('mouseleave', () => _hideInlinePreview(card));
    }

    frag.appendChild(card);
  });

  grid.appendChild(frag);

  if (bannedCount > 0) {
    banCount.textContent = `${bannedCount} banned — re-run to update order`;
    banCount.style.display = '';
  } else {
    banCount.style.display = 'none';
  }
}

function _previewToggleBan(scene, idx) {
  if (manualOverrides[scene] === 'ban') {
    delete manualOverrides[scene];
  } else {
    manualOverrides[scene] = 'ban';
  }
  saveOverrides();
  // Refresh gallery classes if gallery is loaded
  if (typeof _refreshGalleryClasses === 'function') _refreshGalleryClasses();
  if (typeof calculateGalleryStats === 'function') calculateGalleryStats();
  _renderPreviewGrid();
}

function _fmtTime(sec) {
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60).toString().padStart(2, '0');
  return `${m}:${s}`;
}
