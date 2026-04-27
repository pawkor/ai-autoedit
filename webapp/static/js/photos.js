// modern_photos.js — Photo Browser modal

let _photoSelection = new Set();
let _photoList = [];
let _previewIdx = -1;

async function openPhotoBrowser() {
  if (typeof _jobId === 'undefined' || !_jobId) { alert('No project selected.'); return; }

  const modal = document.getElementById('m-photos-modal');
  const grid  = document.getElementById('m-photos-grid');
  if (!modal || !grid) return;

  // Persist photos_dir before fetching
  const dir = document.getElementById('m-analyze-photos-dir')?.value.trim() || '';
  if (dir) {
    const job = await window._modernApi.get(`/api/jobs/${_jobId}`).catch(() => null);
    if (job?.params?.work_dir) {
      await fetch('/api/job-config', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ work_dir: job.params.work_dir, photos_dir: dir }),
      }).catch(() => {});
    }
  }

  grid.innerHTML = '<span style="color:var(--muted);padding:16px">Loading…</span>';
  modal.style.display = 'flex';

  const data = await window._modernApi.get(`/api/jobs/${_jobId}/photos`).catch(() => null);
  _photoList = data?.photos || [];

  if (!_photoList.length) {
    grid.innerHTML = '<span style="color:var(--muted);padding:16px">No photos found. Set Photos directory in Source settings.</span>';
    return;
  }

  _photoSelection = new Set(_photoList.filter(p => p.selected).map(p => p.path));
  _renderPhotoGrid();
  _updatePhotoCount();
}
window.openPhotoBrowser = openPhotoBrowser;

function _renderPhotoGrid() {
  const grid = document.getElementById('m-photos-grid');
  if (!grid) return;
  grid.innerHTML = '';
  for (const photo of _photoList) {
    const sel = _photoSelection.has(photo.path);
    const cell = document.createElement('div');
    cell.style.cssText = `display:flex;flex-direction:column;border-radius:4px;overflow:hidden;cursor:pointer;
      border:2px solid ${sel ? 'var(--green-hi)' : 'transparent'};transition:border-color .1s;background:var(--surface2)`;
    cell.dataset.path = photo.path;

    const imgWrap = document.createElement('div');
    imgWrap.style.cssText = 'position:relative;aspect-ratio:1/1;overflow:hidden;flex-shrink:0;';

    const img = document.createElement('img');
    img.src = photo.thumb_url;
    img.style.cssText = 'width:100%;height:100%;object-fit:cover;display:block;';
    img.loading = 'lazy';
    img.title = photo.filename;
    img.addEventListener('click', (e) => { e.stopPropagation(); openPhotoPreview(photo.path); });

    const cb = document.createElement('div');
    cb.style.cssText = `position:absolute;top:4px;right:4px;width:20px;height:20px;border-radius:3px;
      border:2px solid #fff;background:${sel ? 'var(--green-hi)' : 'rgba(0,0,0,.5)'};
      display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;
      color:#000;user-select:none;`;
    cb.textContent = sel ? '✓' : '';
    imgWrap.append(img, cb);

    const ts = document.createElement('div');
    ts.style.cssText = 'background:#09090b;color:#94a3b8;font-size:10px;padding:3px 4px;text-align:center;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;';
    const d = photo.timestamp ? new Date(photo.timestamp * 1000) : null;
    ts.textContent = d ? d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'}) : photo.filename;

    cell.append(imgWrap, ts);
    cell.addEventListener('click', () => _photoToggle(photo.path));
    grid.appendChild(cell);
  }
}

function _photoToggle(path) {
  if (_photoSelection.has(path)) {
    _photoSelection.delete(path);
  } else {
    _photoSelection.add(path);
  }
  const grid = document.getElementById('m-photos-grid');
  if (grid) {
    const cell = grid.querySelector(`[data-path="${CSS.escape(path)}"]`);
    if (cell) {
      const sel = _photoSelection.has(path);
      cell.style.borderColor = sel ? 'var(--green-hi)' : 'transparent';
      const cb = cell.querySelector('div > div');
      if (cb) { cb.style.background = sel ? 'var(--green-hi)' : 'rgba(0,0,0,.5)'; cb.textContent = sel ? '✓' : ''; }
    }
  }
  _updatePhotoCount();
}

function _updatePhotoCount() {
  const el = document.getElementById('m-photos-count');
  if (!el) return;
  el.textContent = _photoSelection.size
    ? `${_photoSelection.size} photo${_photoSelection.size > 1 ? 's' : ''} selected`
    : 'No photos selected';
}

function openPhotoPreview(path) {
  const overlay = document.getElementById('m-photo-preview');
  const img     = document.getElementById('m-photo-preview-img');
  if (!overlay || !img) return;
  _previewIdx = _photoList.findIndex(p => p.path === path);
  _renderPreviewAt(_previewIdx);
  overlay.style.display = 'flex';
}
window.openPhotoPreview = openPhotoPreview;

function _renderPreviewAt(idx) {
  if (idx < 0 || idx >= _photoList.length) return;
  const photo = _photoList[idx];
  const img = document.getElementById('m-photo-preview-img');
  if (img) img.src = `/api/file?path=${encodeURIComponent(photo.path)}`;
  // update selection indicator on overlay
  _updatePreviewSelBadge();
}

function _updatePreviewSelBadge() {
  const badge = document.getElementById('m-photo-preview-sel');
  if (!badge || _previewIdx < 0) return;
  const path = _photoList[_previewIdx]?.path;
  const sel = path && _photoSelection.has(path);
  badge.textContent = sel ? '✓' : '+';
  badge.style.background = sel ? 'var(--green-hi)' : 'rgba(0,0,0,.55)';
  badge.style.color = sel ? '#000' : '#fff';
}

function _previewToggleCurrent() {
  if (_previewIdx < 0 || _previewIdx >= _photoList.length) return;
  _photoToggle(_photoList[_previewIdx].path);
  _updatePreviewSelBadge();
}

function closePhotoPreview() {
  const overlay = document.getElementById('m-photo-preview');
  if (overlay) overlay.style.display = 'none';
  _previewIdx = -1;
}
window.closePhotoPreview = closePhotoPreview;

document.addEventListener('keydown', e => {
  const prev = document.getElementById('m-photo-preview');
  if (!prev || prev.style.display === 'none') return;
  if (e.key === 'Escape')       { closePhotoPreview(); e.stopPropagation(); }
  else if (e.key === 'ArrowRight') { _previewIdx = (_previewIdx + 1) % _photoList.length; _renderPreviewAt(_previewIdx); }
  else if (e.key === 'ArrowLeft')  { _previewIdx = (_previewIdx - 1 + _photoList.length) % _photoList.length; _renderPreviewAt(_previewIdx); }
  else if (e.key === ' ')          { e.preventDefault(); _previewToggleCurrent(); }
});

async function savePhotoSelection() {
  if (typeof _jobId === 'undefined' || !_jobId) return;
  await fetch(`/api/jobs/${_jobId}/params`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ selected_photos: [..._photoSelection] }),
  }).catch(() => {});
  closePhotoBrowser();
}
window.savePhotoSelection = savePhotoSelection;

function closePhotoBrowser() {
  const modal = document.getElementById('m-photos-modal');
  if (modal) modal.style.display = 'none';
  closePhotoPreview();
}
window.closePhotoBrowser = closePhotoBrowser;
