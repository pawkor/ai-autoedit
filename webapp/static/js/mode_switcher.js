(function() {
  const mode = localStorage.getItem('uiMode') || 'legacy';
  const path = window.location.pathname;
  const isModernPage = path.endsWith('modern.html');

  if (mode === 'modern' && !isModernPage) {
    window.location.href = 'modern.html' + window.location.search;
  } else if (mode === 'legacy' && isModernPage) {
    window.location.href = 'index.html' + window.location.search;
  }
})();
