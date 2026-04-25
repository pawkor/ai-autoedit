// modern_shorts.js — Shorts generation modal

async function openShortsModal() {
  if (typeof _jobId === 'undefined' || !_jobId) return;
  const modal = document.getElementById('m-shorts-modal');
  if (!modal) return;
  document.getElementById('m-shorts-status').textContent = '';
  document.getElementById('m-shorts-btn').disabled = false;

  const job = await window._modernApi.get(`/api/jobs/${_jobId}`);
  if (job?.params) {
    const p = job.params;
    const setChk = (id, val) => {
      const el = document.getElementById(id);
      if (el) el.checked = !!val;
    };
    setChk('m-shorts-text',     p.shorts_text);
    setChk('m-shorts-multicam', p.shorts_multicam);
    setChk('m-shorts-beat',     p.shorts_beat_sync);
    setChk('m-shorts-best',     p.shorts_best);

    const cams = p.cameras || [p.cam_a, p.cam_b].filter(Boolean);
    const mcRow = document.getElementById('m-shorts-multicam-row');
    if (mcRow) mcRow.style.display = cams.length > 1 ? '' : 'none';
  }

  modal.style.display = 'flex';
}
window.openShortsModal = openShortsModal;

function closeShortsModal() {
  const modal = document.getElementById('m-shorts-modal');
  if (modal) modal.style.display = 'none';
}
window.closeShortsModal = closeShortsModal;

async function renderShorts() {
  if (typeof _jobId === 'undefined' || !_jobId) {
    alert('No project selected.'); return;
  }

  const count    = parseInt(document.getElementById('m-shorts-count')?.value) || 1;
  const text     = document.getElementById('m-shorts-text')?.checked ?? false;
  const multicam = document.getElementById('m-shorts-multicam')?.checked ?? false;
  const beat     = document.getElementById('m-shorts-beat')?.checked ?? false;
  const best     = document.getElementById('m-shorts-best')?.checked ?? false;

  const btn    = document.getElementById('m-shorts-btn');
  const status = document.getElementById('m-shorts-status');
  if (btn)    btn.disabled = true;
  if (status) status.textContent = 'Starting…';

  await window._modernApi.patch(`/api/jobs/${_jobId}/params`, {
    shorts_text:      text,
    shorts_multicam:  multicam,
    shorts_beat_sync: beat,
    shorts_best:      best,
  });

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
