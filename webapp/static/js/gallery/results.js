// ── Results tab ───────────────────────────────────────────────────────────────

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
  const mainContainer  = document.getElementById('rf-files-main');
  const shortContainer = document.getElementById('rf-files-short');
  mainContainer.innerHTML = '';
  shortContainer.innerHTML = '';
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
    if (info.music) {
      const rfMusic = document.createElement('div');
      rfMusic.style.cssText = 'font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:2px';
      rfMusic.title = info.music;
      rfMusic.textContent = '♪ ' + info.music.replace(/\.[^.]+$/, '');
      div.appendChild(rfMusic);
    }
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
    if (isShort && info.is_ncs) {
      const igBtn = document.createElement('button');
      igBtn.className = 'btn-sm';
      igBtn.textContent = '▲ IG Reel';
      igBtn.title = 'Upload to Instagram Reels';
      igBtn.style.background = 'linear-gradient(135deg,#833ab4,#fd1d1d,#fcb045)';
      igBtn.style.color = '#fff';
      igBtn.style.border = 'none';
      igBtn.onclick = e => { e.stopPropagation(); igReelModalOpen(filePath, name, info.ncs_attr || null); };
      btnRow.appendChild(igBtn);
    }
    const s3Btn = document.createElement('button');
    s3Btn.className = 'btn-sm';
    s3Btn.textContent = '▲ S3';
    s3Btn.title = 'Upload to S3';
    s3Btn.onclick = e => { e.stopPropagation(); s3ModalOpen(filePath, name); };
    api.get('/api/s3/status').then(s => { if (!s?.configured) s3Btn.style.display = 'none'; });
    btnRow.appendChild(s3Btn);
    div.appendChild(btnRow);
    if (info.yt_url) {
      const ytLink = document.createElement('a');
      ytLink.href = info.yt_url;
      ytLink.target = '_blank';
      ytLink.textContent = '▶ YouTube';
      ytLink.style.cssText = 'display:block;font-size:11px;color:var(--green);text-decoration:none;margin-top:4px';
      ytLink.onclick = e => e.stopPropagation();
      div.appendChild(ytLink);
    }
    if (info.ig_url) {
      const igLink = document.createElement('a');
      igLink.href = info.ig_url;
      igLink.target = '_blank';
      igLink.textContent = '▶ Instagram Reel';
      igLink.style.cssText = 'display:block;font-size:11px;text-decoration:none;margin-top:2px;background:linear-gradient(90deg,#833ab4,#fd1d1d,#fcb045);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text';
      igLink.onclick = e => e.stopPropagation();
      div.appendChild(igLink);
    }
    const delBtn = document.createElement('button');
    delBtn.className = 'rf-delete';
    delBtn.title = name;
    delBtn.textContent = '✕';
    delBtn.addEventListener('click', e => {
      e.stopPropagation();
      deleteResultFile(jobId, name, div);
    });
    div.appendChild(delBtn);
    div.dataset.url = info.url;
    div.dataset.previewUrl = info.preview_url || '';
    div.dataset.isShort = isShort ? '1' : '';
    div.dataset.jobId = jobId;
    div.addEventListener('click', () => playVideoOrPreview(div));
    (isShort ? shortContainer : mainContainer).appendChild(div);
  }

  // Auto-play first highlight (or first short if no highlight)
  const firstCard = mainContainer.querySelector('.rf') || shortContainer.querySelector('.rf');
  if (firstCard) playVideoOrPreview(firstCard);
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

let _previewGenerating = false;

async function playVideoOrPreview(card) {
  const isShort = card.dataset.isShort === '1';
  const url = card.dataset.url;

  // Shorts: always play original
  if (isShort) { playVideo(url, card); return; }

  // Has preview cached
  if (card.dataset.previewUrl) { playVideo(card.dataset.previewUrl, card); return; }

  // Generate preview on demand
  if (_previewGenerating) {
    const w = document.getElementById('video-wrap');
    const m = w && w.querySelector('.preview-gen-msg');
    if (m) m.style.display = 'none';
    playVideo(url, card);
    return;
  }
  _previewGenerating = true;

  document.querySelectorAll('.rf').forEach(c=>c.classList.remove('playing'));
  card.classList.add('playing');
  const wrap = document.getElementById('video-wrap');
  const video = document.getElementById('video-player');
  wrap.style.display = 'block';
  video.src = '';

  // Show generating indicator
  let genMsg = wrap.querySelector('.preview-gen-msg');
  if (!genMsg) {
    genMsg = document.createElement('div');
    genMsg.className = 'preview-gen-msg';
    genMsg.style.cssText = 'position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:#fff;font-size:14px;pointer-events:none';
    wrap.style.position = 'relative';
    wrap.appendChild(genMsg);
  }
  genMsg.textContent = 'Generating 1080p preview…';
  genMsg.style.display = 'flex';

  try {
    const jobId = card.dataset.jobId;
    const filename = card.dataset.url.split('/').pop();
    const data = await api.post(`/api/jobs/${jobId}/preview?filename=${encodeURIComponent(filename)}`, {});
    if (data?.preview_url) {
      card.dataset.previewUrl = data.preview_url;
      genMsg.style.display = 'none';
      playVideo(data.preview_url, card);
    } else {
      genMsg.style.display = 'none';
      playVideo(url, card);
    }
  } catch(e) {
    genMsg.style.display = 'none';
    playVideo(url, card);
  } finally {
    _previewGenerating = false;
  }
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
