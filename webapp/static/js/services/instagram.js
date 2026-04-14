// ── Instagram Reels ───────────────────────────────────────────────────────────

let _igFilePath = null, _igFileName = null;

async function igReelModalOpen(filePath, fileName, ncsAttr = null) {
  const status = await api.get('/api/ig/status');
  if (!status?.configured) {
    alert('Instagram not configured.\nSet IG_ACCESS_TOKEN and IG_USER_ID in .env and restart the server.');
    return;
  }
  _igFilePath = filePath;
  _igFileName = fileName;
  document.getElementById('ig-file-name').textContent = fileName;
  document.getElementById('ig-status').textContent = '';
  document.getElementById('btn-ig-upload').disabled = false;
  document.getElementById('btn-ig-upload').textContent = '▲ Upload';

  const tokenWarn = document.getElementById('ig-token-warn');
  if (status.days_until_expiry != null && status.days_until_expiry <= 5) {
    tokenWarn.textContent = `⚠ IG token expires in ${Math.ceil(status.days_until_expiry)} day(s) — auto-refresh attempted at startup`;
    tokenWarn.style.display = '';
  } else {
    tokenWarn.style.display = 'none';
  }

  const warn = document.getElementById('ig-cooldown-warn');
  if (!status.ready) {
    const rem = Math.ceil(status.cooldown_remaining_h * 60);
    warn.textContent = `⚠ Cooldown active — ${rem} min until next upload (min ${status.min_hours}h between posts)`;
    warn.style.display = '';
    document.getElementById('btn-ig-upload').disabled = true;
  } else {
    warn.style.display = 'none';
  }

  const cap = document.getElementById('ig-caption');
  const hashtags = '#reels #motorcycle #motovlog #ktm #adventurebike #roadtrip';
  const repoUrl = 'https://github.com/pawkor/ai-autoedit';
  if (ncsAttr) {
    cap.value = `Music: ${ncsAttr} (NCS Release)\n\n${hashtags}\n${repoUrl}`;
  } else {
    cap.value = `${hashtags}\n${repoUrl}`;
  }

  document.getElementById('ig-reel-modal').classList.add('open');
}

function igReelModalClose() {
  document.getElementById('ig-reel-modal').classList.remove('open');
}

async function igReelUpload() {
  const btn    = document.getElementById('btn-ig-upload');
  const status = document.getElementById('ig-status');
  const caption = document.getElementById('ig-caption').value.trim();
  btn.disabled = true;
  btn.textContent = 'Uploading…';
  status.textContent = 'Submitting…';
  status.style.color = 'var(--muted)';

  const res = await api.post('/api/ig/upload', {file_path: _igFilePath, caption});
  if (!res?.upload_id) {
    status.textContent = '⚠ ' + (res?.detail || 'Failed to start upload');
    status.style.color = 'var(--red)';
    btn.disabled = false;
    btn.textContent = '▲ Upload';
    return;
  }

  const poll = setInterval(async () => {
    const s = await api.get(`/api/ig/upload/${res.upload_id}`);
    if (!s) return;
    status.textContent = s.message || s.status;
    if (s.status === 'done') {
      clearInterval(poll);
      status.innerHTML = '';
      const a = document.createElement('a');
      a.href = s.url; a.target = '_blank'; a.style.color = 'var(--green)';
      a.textContent = '✓ ' + s.url;
      status.appendChild(a);
      status.style.color = 'var(--green)';
      btn.textContent = '✓ Done';
      btn.onclick = igReelModalClose;
    } else if (s.status === 'error') {
      clearInterval(poll);
      status.textContent = '⚠ ' + s.message;
      status.style.color = 'var(--red)';
      btn.disabled = false;
      btn.textContent = '▲ Retry';
    }
  }, 5000);
}
