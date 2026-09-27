/* Alternative Studio shell.
 * It deliberately reuses the existing controls and API. The mode changes
 * layout and interaction density only, so switching back cannot invalidate a
 * project or timeline.
 */
(function () {
  function applyUiMode(mode) {
    const alternative = mode === 'alternative';
    document.body.classList.toggle('ui-alternative', alternative);
    document.documentElement.dataset.uiMode = alternative ? 'alternative' : 'modern';
  }

  window.applyUiMode = applyUiMode;

  async function loadUiMode() {
    try {
      const r = await fetch('/api/settings', { cache: 'no-store' });
      const s = await r.json();
      applyUiMode(s.ui_mode || 'modern');
    } catch (_) {
      applyUiMode('modern');
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', loadUiMode, { once: true });
  } else {
    loadUiMode();
  }
})();
