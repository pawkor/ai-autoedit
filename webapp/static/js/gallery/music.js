// ── Music tab ─────────────────────────────────────────────────────────────────

async function loadMusicTracks() {
  const dir = document.getElementById('music-dir-input').value.trim();
  if (!dir) return;
  // Persist music dir immediately (before API call, so it's saved even if API fails)
  const wd = document.getElementById('js-workdir').value.trim();
  if (wd) api.put('/api/job-config', { work_dir: wd, music_dir: dir });
  if (currentJobId) api.patch(`/api/jobs/${currentJobId}/params`, { music_dir: dir });
  const [data, acrSt] = await Promise.all([
    api.get(`/api/music-files?dir=${encodeURIComponent(dir)}`),
    api.get('/api/acr-status'),
  ]);
  if (!data) return;
  _acrConfigured = acrSt?.configured ?? false;
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
  if (filter && !`${t.title} ${t.artist || ''}`.toLowerCase().includes(filter)) return false;
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
  // Prefer explicit user target over clips-only estimate (clips don't include intro/outro)
  const targetDur = (targetFromInput > 0 ? targetFromInput : null)
    ?? analyzeResult?._live_est_dur
    ?? (analyzeResult?.estimated_duration_sec > 0 ? analyzeResult.estimated_duration_sec : null)
    ?? 0;
  if (!_musicSort.key) {
    if (!targetDur) return tracks;
    return [...tracks].sort((a, b) => {
      const da = (a.duration || 0) - targetDur;
      const db = (b.duration || 0) - targetDur;
      const absDa = Math.abs(da), absDb = Math.abs(db);
      if (absDa !== absDb) return absDa - absDb;
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
    if (t.yt_url) {
      const badge = document.createElement('a');
      badge.href = t.yt_url;
      badge.target = '_blank';
      badge.rel = 'noopener';
      badge.onclick = e => e.stopPropagation();
      const isCC = (t.yt_license || '').toLowerCase().includes('creative commons');
      badge.className = 'mt-yt-badge ' + (isCC ? 'mt-yt-cc' : 'mt-yt-copy');
      badge.title = isCC ? `✓ ${t.yt_license}` : `⚠ No free license — may trigger Content ID\n${t.yt_license || 'License unknown'}`;
      badge.textContent = isCC ? 'CC' : '©';
      titleWrap.appendChild(badge);
    }
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
    const delBtn = document.createElement('button');
    delBtn.className = 'mt-del';
    delBtn.textContent = '✕';
    delBtn.title = 'Delete from disk';
    delBtn.onclick = async e => {
      e.stopPropagation();
      const name = t.title + (t.artist ? ` — ${t.artist}` : '');
      if (!await showConfirm('Delete track', `Delete "${name}" from disk?\nAlso removes from index and usage history.`, null, 'Delete')) return;
      const r = await api.del(`/api/music-file?path=${encodeURIComponent(t.file)}`);
      if (r?.ok) {
        musicTracks = musicTracks.filter(x => x.file !== t.file);
        musicSelected.delete(t.file);
        renderMusicList();
      } else {
        alert('Delete failed');
      }
    };
    // ACR check button + cached result badge
    const acrBtn = document.createElement('button');
    acrBtn.className = 'mt-acr';
    acrBtn.textContent = '⚙';
    acrBtn.title = 'Check Content ID (ACRCloud)';
    const acrBadge = document.createElement('span');
    acrBadge.className = 'mt-acr-badge';
    if (t.acr_matched === true) {
      acrBadge.className += t.acr_blocked ? ' acr-blocked' : ' acr-ok';
      acrBadge.textContent = t.acr_blocked ? '⚠ Claimed' : '✓ Free';
      acrBadge.title = t.acr_info || '';
    } else if (t.acr_matched === false) {
      acrBadge.className += ' acr-ok';
      acrBadge.textContent = '✓ No match';
    }
    acrBtn.onclick = async e => {
      e.stopPropagation();
      if (!_acrConfigured) { alert('ACRCloud not configured — add ACRCLOUD_HOST/ACCESS_KEY/ACCESS_SECRET to .env'); return; }
      acrBtn.textContent = '…';
      acrBtn.disabled = true;
      try {
        const r = await api.post('/api/music/acr-check', { path: t.file });
        if (r.matched) {
          const info = `${r.artists} — ${r.title} (${r.label}) score:${r.score}`;
          t.acr_matched = true;
          t.acr_blocked = r.blocked;
          t.acr_info    = info + (r.rights?.length ? '\nRights: ' + JSON.stringify(r.rights) : '');
          acrBadge.className = 'mt-acr-badge ' + (r.blocked ? 'acr-blocked' : 'acr-ok');
          acrBadge.textContent = r.blocked ? '⚠ Claimed' : '✓ Free';
          acrBadge.title = t.acr_info;
        } else {
          t.acr_matched = false;
          t.acr_blocked = false;
          acrBadge.className = 'mt-acr-badge acr-ok';
          acrBadge.textContent = '✓ No match';
          acrBadge.title = r.msg || '';
        }
      } catch(err) {
        acrBadge.textContent = '!err';
        acrBadge.title = String(err);
      } finally {
        acrBtn.textContent = '⚙';
        acrBtn.disabled = false;
      }
    };
    row.appendChild(cb);
    row.appendChild(playBtn);
    row.appendChild(delBtn);
    row.appendChild(acrBtn);
    row.appendChild(acrBadge);
    row.appendChild(titleWrap);
    row.appendChild(meta);
    row.appendChild(durSpan);
    row.appendChild(bpm);
    row.appendChild(energyWrap);
    row.onclick = e => {
      if ([cb, playBtn, seek, delBtn, acrBtn, acrBadge].includes(e.target)) return;
      cb.checked = !cb.checked; cb.dispatchEvent(new Event('change'));
    };
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

    while (true) {
      await new Promise(r => setTimeout(r, 500));
      const status = await fetch(`/api/music-rebuild-status/${task_id}`).then(r => r.json());
      if (status.total > 0) {
        const p = Math.min(99, Math.round(status.progress / status.total * 100));
        bar.style.width = p + '%';
      } else if (!status.done) {
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
