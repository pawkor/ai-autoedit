// modern_appsettings.js — App Settings modal (HW info, volumes, concurrency, queue)

let _appHwInfo = null;

async function openAppSettingsModal() {
  const modal = document.getElementById('m-appsettings-modal');
  if (!modal) return;
  document.getElementById('m-app-settings-status').textContent = '';

  const [settings, hw, cfg] = await Promise.all([
    window._modernApi.get('/api/settings').catch(() => null),
    window._modernApi.get('/api/hw-info').catch(() => null),
    window._modernApi.get('/api/config').catch(() => null),
  ]);

  _appHwInfo = hw;

  const cpuEl  = document.getElementById('m-app-hw-cpu');
  const ramEl  = document.getElementById('m-app-hw-ram');
  const vramEl = document.getElementById('m-app-hw-vram');
  if (hw) {
    if (cpuEl)  cpuEl.textContent  = `CPU ${hw.cpu_count} cores`;
    if (ramEl)  ramEl.textContent  = `RAM ${hw.ram_gb} GB`;
    if (vramEl) vramEl.textContent = hw.vram_mb ? `VRAM ${hw.vram_mb} MB` : 'No GPU';
  }

  const concEl = document.getElementById('m-app-concurrent');
  if (concEl && settings) concEl.value = settings.max_concurrent_jobs ?? 1;

  const dataRootEl = document.getElementById('m-app-data-root');
  if (dataRootEl && cfg) dataRootEl.value = cfg.data_root ?? '';

  _initVolSlider('m-app-music-vol', 'm-app-music-vol-val', settings?.music_vol_pct ?? 100);

  const clipCtxEl = document.getElementById('m-app-clip-context');
  if (clipCtxEl && settings) {
    clipCtxEl.value = settings.global_clip_context ||
      'helmet-cam and handlebar/chest action cameras, KTM adventure motorcycle, road always visible in frame, both rider-POV and face-cam perspectives';
  }

  await _appQueueRefresh();
  await _appYtCheckStatus();
  modal.style.display = 'flex';
  _queueTimer = setInterval(_appQueueRefresh, 3000);
}
window.openAppSettingsModal = openAppSettingsModal;

async function closeAppSettingsModal() {
  if (_queueTimer) { clearInterval(_queueTimer); _queueTimer = null; }
  await saveAppSettings();
  const modal = document.getElementById('m-appsettings-modal');
  if (modal) modal.style.display = 'none';
}
window.closeAppSettingsModal = closeAppSettingsModal;

function _initVolSlider(sliderId, valId, pct) {
  const slider = document.getElementById(sliderId);
  const label  = document.getElementById(valId);
  if (!slider) return;
  slider.value = pct;
  if (label) label.textContent = pct;
  slider.oninput = () => { if (label) label.textContent = slider.value; };
}

// Suggest values based on detected HW
function autoDetectSettings() {
  if (!_appHwInfo) return;
  const concEl = document.getElementById('m-app-concurrent');
  if (concEl) concEl.value = _appHwInfo.vram_mb >= 16384 ? 2 : 1;
}
window.autoDetectSettings = autoDetectSettings;

async function saveAppSettings() {
  const concurrent = parseInt(document.getElementById('m-app-concurrent')?.value) || 1;
  const musicVol   = parseInt(document.getElementById('m-app-music-vol')?.value)   ?? 100;
  const dataRoot   = document.getElementById('m-app-data-root')?.value.trim() ?? '';
  const status     = document.getElementById('m-app-settings-status');

  const clipCtx = document.getElementById('m-app-clip-context')?.value ?? '';
  const r = await fetch('/api/settings', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ max_concurrent_jobs: concurrent, music_vol_pct: musicVol, global_clip_context: clipCtx }),
  });
  if (!r.ok) { if (status) status.textContent = '✗ Save failed'; return; }

  if (dataRoot) {
    await fetch('/api/config/data-root', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: dataRoot }),
    });
    if (status) status.textContent = 'Scanning projects…';
    const scan = await fetch('/api/jobs/scan-root', { method: 'POST' }).then(r => r.ok ? r.json() : null).catch(() => null);
    if (scan?.imported > 0 && typeof refreshProjectList === 'function') refreshProjectList();
    if (status) status.textContent = scan?.imported > 0 ? `✓ Saved — found ${scan.imported} project(s)` : '✓ Saved';
  } else {
    if (status) status.textContent = '✓ Saved';
  }
  setTimeout(() => { if (status) status.textContent = ''; }, 2000);
}

async function _appPickDataRoot() {
  if (typeof window.pickFolder === 'function') {
    const path = await window.pickFolder();
    if (path) {
      const el = document.getElementById('m-app-data-root');
      if (el) el.value = path;
    }
    return;
  }
  alert('Folder picker not available — type the path manually.');
}
window._appPickDataRoot = _appPickDataRoot;
window.saveAppSettings = saveAppSettings;

// ── Queue ─────────────────────────────────────────────────────────────────────
function _queuePhaseLabel(phase) {
  return (typeof _tm === 'function' ? _tm('misc.phase.' + phase) : null) || phase || '';
}
let _queueTimer = null;

async function _appQueueRefresh() {
  const list = document.getElementById('m-app-queue-list');
  if (!list) return;

  const jobs = await window._modernApi.get('/api/jobs').catch(() => null);
  if (!jobs?.length) {
    list.innerHTML = '<span style="color:var(--muted)">No jobs</span>';
    return;
  }

  const active = jobs.filter(j => j.status === 'running' || j.status === 'queued');
  if (!active.length) {
    list.innerHTML = '<span style="color:var(--muted)">No active jobs</span>';
    return;
  }

  list.innerHTML = '';
  for (const j of active) {
    const wrap = document.createElement('div');
    wrap.style.cssText = 'padding:4px 0;border-bottom:1px solid var(--border)';

    const row = document.createElement('div');
    row.style.cssText = 'display:flex;align-items:center;gap:8px';

    const dot = document.createElement('span');
    dot.style.cssText = `width:8px;height:8px;border-radius:50%;flex-shrink:0;background:${j.status === 'running' ? 'var(--green-hi)' : 'var(--sub)'}`;

    const name = document.createElement('span');
    name.style.cssText = 'flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px';
    const dir = j.work_dir || j.id;
    const segs = dir.split('/');
    name.textContent = segs.length >= 2 ? segs.slice(-2).join('/') : segs.pop();
    name.title = dir;

    const phase = document.createElement('span');
    const phaseText = j.status === 'queued'
      ? _tm('misc.queued')
      : _queuePhaseLabel(j.phase);
    phase.style.cssText = 'font-size:10px;color:var(--sub);white-space:nowrap;flex-shrink:0';
    phase.textContent = phaseText;

    const stopBtn = document.createElement('button');
    stopBtn.className = 'm-btn m-btn-ghost m-btn-sm';
    stopBtn.textContent = '✕';
    stopBtn.title = 'Stop';
    stopBtn.onclick = async () => {
      stopBtn.disabled = true;
      await fetch(`/api/jobs/${j.id}`, { method: 'DELETE' }).catch(() => {});
      await _appQueueRefresh();
    };

    row.append(dot, name, phase, stopBtn);
    wrap.appendChild(row);

    // Progress bar + ETA (only when running and progress known)
    const pct = j.progress ?? 0;
    if (j.status === 'running' && pct > 0) {
      const pbRow = document.createElement('div');
      pbRow.style.cssText = 'display:flex;align-items:center;gap:6px;margin-top:3px;padding-left:16px';

      const track = document.createElement('div');
      track.style.cssText = 'flex:1;height:3px;background:var(--border);border-radius:2px;overflow:hidden';
      const fill = document.createElement('div');
      fill.style.cssText = `height:3px;background:var(--blue);border-radius:2px;width:${pct}%;transition:width .4s`;
      track.appendChild(fill);

      const info = document.createElement('span');
      info.style.cssText = 'font-size:9px;color:var(--muted);white-space:nowrap';
      let infoText = pct + '%';
      if (j.progress_label) infoText += '  ' + j.progress_label.substring(0, 40);
      if (j.started_at && pct > 2) {
        const elapsed = Date.now() / 1000 - j.started_at;
        const eta = elapsed * (100 - pct) / pct;
        infoText += '  ETA ' + (eta < 60 ? `~${Math.round(eta)}s` : `~${Math.floor(eta/60)}m`);
      }
      info.textContent = infoText;

      pbRow.append(track, info);
      wrap.appendChild(pbRow);
    }

    list.appendChild(wrap);
  }
}
window._appQueueRefresh = _appQueueRefresh;

// ── YouTube connect ───────────────────────────────────────────────────────────
async function _appYtCheckStatus() {
  const data = await window._modernApi.get('/api/youtube/status').catch(() => null);
  const statusEl   = document.getElementById('m-app-yt-status');
  const connectBtn = document.getElementById('m-app-yt-connect');
  const discBtn    = document.getElementById('m-app-yt-disconnect');
  const hintEl     = document.getElementById('m-app-yt-secrets-hint');
  if (!statusEl) return;
  if (!data) { statusEl.textContent = 'unavailable'; return; }
  if (!data.has_secrets) {
    statusEl.textContent = '✗ no credentials';
    statusEl.style.color = 'var(--red)';
    if (hintEl)     hintEl.style.display = '';
    if (connectBtn) connectBtn.style.display = 'none';
    if (discBtn)    discBtn.style.display    = 'none';
  } else if (data.authenticated) {
    statusEl.textContent = '● connected';
    statusEl.style.color = 'var(--green-hi, #4ade80)';
    if (hintEl)     hintEl.style.display = 'none';
    if (connectBtn) connectBtn.style.display = 'none';
    if (discBtn)    discBtn.style.display    = '';
  } else {
    statusEl.textContent = '○ not connected';
    statusEl.style.color = '';
    if (hintEl)     hintEl.style.display = 'none';
    if (connectBtn) connectBtn.style.display = '';
    if (discBtn)    discBtn.style.display    = 'none';
  }
}

async function appYtConnect() {
  const win = window.open('', 'yt-auth', 'width=620,height=720');
  const data = await window._modernApi.get(
    `/api/youtube/auth?origin=${encodeURIComponent(window.location.origin)}`
  );
  if (!data?.url) {
    win.close();
    alert('Failed to get auth URL — is youtube_client_secrets.json in place?');
    return;
  }
  win.location.href = data.url;
  let winClosedAt = null;
  const timer = setInterval(async () => {
    if (win.closed && !winClosedAt) winClosedAt = Date.now();
    const s = await window._modernApi.get('/api/youtube/status');
    if (s?.authenticated) { clearInterval(timer); _appYtCheckStatus(); }
    else if (winClosedAt && Date.now() - winClosedAt > 5000) clearInterval(timer);
  }, 800);
}
window.appYtConnect = appYtConnect;

async function appYtDisconnect() {
  if (!confirm('Disconnect YouTube account?')) return;
  await fetch('/api/youtube/disconnect', { method: 'DELETE' });
  _appYtCheckStatus();
}
window.appYtDisconnect = appYtDisconnect;
