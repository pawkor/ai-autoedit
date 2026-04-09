// ── yt-dlp music download ─────────────────────────────────────────────────────

let _ytdlEs = null;
function ytdlDownload() {
  const url = document.getElementById('music-yt-url').value.trim();
  if (!url) return;
  const msgEl  = document.getElementById('music-yt-msg');
  const pctEl  = document.getElementById('music-yt-pct');
  msgEl.textContent  = 'Starting…';
  pctEl.textContent  = '';
  _ytdlEs?.close();
  _ytdlEs = new EventSource(`/api/music/yt-download?url=${encodeURIComponent(url)}`);
  _ytdlEs.onmessage = e => {
    const d = JSON.parse(e.data);
    if (d.msg) msgEl.textContent = d.msg.replace(/^\[.*?\]\s*/, '');
    if (d.pct != null) pctEl.textContent = Math.round(d.pct) + '%';
    if (d.done) {
      _ytdlEs.close(); _ytdlEs = null;
      pctEl.textContent = '';
      document.getElementById('music-yt-url').value = '';
      _ytdlSave(d.path, d.name, msgEl, pctEl);
    }
    if (d.error) {
      _ytdlEs.close(); _ytdlEs = null;
      msgEl.textContent = '✗ ' + d.error.split('\n').pop();
      pctEl.textContent = '';
    }
  };
  _ytdlEs.onerror = () => {
    _ytdlEs.close();
    document.getElementById('music-yt-msg').textContent = 'Connection error';
  };
}

async function _ytdlSave(tmpPath, name, msgEl, pctEl) {
  const musicDir = document.getElementById('music-dir-input').value.trim();

  if (_s3Configured) {
    // ── S3 path ──────────────────────────────────────────────────────────────
    const prefix = (document.getElementById('music-s3-prefix').value.trim() || 'music/').replace(/\/*$/, '/');
    const key    = prefix + name + '.mp3';
    msgEl.textContent = 'Uploading to S3…';
    let _s3UpEs = new EventSource(`/api/s3/upload?local_path=${encodeURIComponent(tmpPath)}&key=${encodeURIComponent(key)}`);
    await new Promise(resolve => {
      _s3UpEs.onmessage = ev => {
        const u = JSON.parse(ev.data);
        if (u.pct != null) pctEl.textContent = u.pct + '%';
        if (u.speed)       msgEl.textContent = 'S3 ' + u.speed;
        if (u.done) {
          _s3UpEs.close();
          pctEl.textContent = '';
          resolve(true);
        }
        if (u.error) {
          _s3UpEs.close();
          msgEl.textContent = '✗ S3: ' + u.error;
          pctEl.textContent = '';
          pinnedTrack = tmpPath;
          document.querySelector('#btn-music-to-summary')?.classList.remove('hidden');
          updatePhaseUI();
          resolve(false);
        }
      };
      _s3UpEs.onerror = () => { _s3UpEs.close(); msgEl.textContent = 'S3 upload error'; resolve(false); };
    });
    if (musicDir) {
      const res = await fetch('/api/music/save-downloaded', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({tmp_path: tmpPath, music_dir: musicDir}),
      }).then(r => r.ok ? r.json() : null).catch(() => null);
      if (res?.ok) {
        pinnedTrack = res.path;
        msgEl.textContent = '✓ ' + name + ' (S3 + local)';
        loadMusicTracks();
      } else {
        pinnedTrack = tmpPath;
        msgEl.textContent = '✓ ' + name + ' (S3)';
      }
    } else {
      pinnedTrack = tmpPath;
      msgEl.textContent = '✓ ' + name + ' (S3)';
    }
    s3MusicBrowse();
  } else {
    // ── Local path ───────────────────────────────────────────────────────────
    if (!musicDir) {
      pinnedTrack = tmpPath;
      msgEl.textContent = '✓ ' + name + ' (set Music dir to save permanently)';
      document.querySelector('#btn-music-to-summary')?.classList.remove('hidden');
      updatePhaseUI();
      return;
    }
    const res = await fetch('/api/music/save-downloaded', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({tmp_path: tmpPath, music_dir: musicDir}),
    }).then(r => r.ok ? r.json() : null).catch(() => null);
    if (res?.ok) {
      pinnedTrack = res.path;
      msgEl.textContent = '✓ ' + name;
      loadMusicTracks();
    } else {
      pinnedTrack = tmpPath;
      msgEl.textContent = '✓ ' + name + ' (save failed)';
    }
  }
  document.querySelector('#btn-music-to-summary')?.classList.remove('hidden');
  updatePhaseUI();
}
