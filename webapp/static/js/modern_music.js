// modern_music.js — music list, pin, rebuild trigger

async function loadMusicList(jobId) {
  const [musicData, jobData] = await Promise.all([
    window._modernApi.get(`/api/music?job_id=${jobId}`),
    window._modernApi.get(`/api/jobs/${jobId}`),
  ]);
  const tracks = musicData?.tracks || [];

  // Restore pinned track from saved job params
  if (jobData?.params?.selected_track) {
    _pinnedTrack = jobData.params.selected_track;
  }

  renderMusicList(tracks);

  if (_pinnedTrack) {
    const t = tracks.find(t => t.file === _pinnedTrack);
    const label = document.getElementById('m-music-label');
    if (label) label.textContent = t?.title || _pinnedTrack.split('/').pop();
    const rebuild = document.getElementById('m-btn-rebuild');
    if (rebuild) rebuild.disabled = false;
    rebuildTimeline();
  }
}
window.loadMusicList = loadMusicList;

function renderMusicList(tracks) {
  const list = document.getElementById('m-music-list');
  if (!list) return;
  if (tracks.length === 0) {
    list.innerHTML = '<div class="m-empty">No tracks found</div>';
    return;
  }
  list.innerHTML = '';
  tracks.forEach(t => {
    const isPinned = t.file === _pinnedTrack;
    const dur = fmtSec(t.duration || 0);
    const bpm = t.bpm ? `· ${Math.round(t.bpm)} BPM` : '';
    const item = document.createElement('div');
    item.className = `m-track-row-item${isPinned ? ' pinned' : ''}`;
    item.onclick = () => pinTrack(t.file, tracks);
    const title = document.createElement('div');
    title.className = 'm-track-row-title';
    title.textContent = t.title || t.file.split('/').pop();
    const meta = document.createElement('div');
    meta.className = 'm-track-row-meta';
    meta.textContent = `${dur} ${bpm}`;
    item.appendChild(title);
    item.appendChild(meta);
    if (isPinned) {
      const dot = document.createElement('div');
      dot.className = 'm-track-pinned-dot';
      dot.textContent = '● pinned';
      item.appendChild(dot);
    }
    list.appendChild(item);
  });
}

async function pinTrack(file, tracks) {
  _pinnedTrack = (_pinnedTrack === file) ? null : file;
  renderMusicList(tracks);

  const label = document.getElementById('m-music-label');
  if (label) {
    const t = tracks.find(t => t.file === _pinnedTrack);
    label.textContent = _pinnedTrack ? (t?.title || _pinnedTrack.split('/').pop()) : 'no track selected';
  }

  const rebuild = document.getElementById('m-btn-rebuild');
  if (rebuild) rebuild.disabled = !_pinnedTrack;

  if (_pinnedTrack) await rebuildTimeline();
}
window.pinTrack = pinTrack;
