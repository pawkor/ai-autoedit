// ── Sidebar drawer ─────────────────────────────────────────────────────────────
function toggleSidebar() {
  document.getElementById('m-sidebar').classList.toggle('open');
  document.getElementById('m-sidebar-overlay').classList.toggle('open');
}
window.toggleSidebar = toggleSidebar;

// Close sidebar when project is selected on mobile
document.addEventListener('click', function(e) {
  const item = e.target.closest('.m-proj-item');
  if (item && window.innerWidth <= 768) {
    const sidebar = document.getElementById('m-sidebar');
    if (sidebar.classList.contains('open')) toggleSidebar();
  }
});

// ── Mobile tab switching ───────────────────────────────────────────────────────
function mobileTab(tab) {
  const pool  = document.getElementById('m-pool');
  const tl    = document.getElementById('m-timeline-wrap');
  const panel = document.getElementById('m-panel');
  const tabs  = document.querySelectorAll('.m-bnav-tab');

  if (tab === 'controls') {
    // Toggle controls bottom sheet
    const isOpen = panel.classList.contains('mobile-open');
    panel.classList.toggle('mobile-open', !isOpen);
    tabs.forEach(b => b.classList.toggle('active',
      !isOpen && b.dataset.tab === 'controls'));
    return;
  }

  // Close controls sheet when switching to another tab
  panel.classList.remove('mobile-open');

  if (tab === 'pool') {
    if (pool) pool.classList.remove('mobile-hidden');
    if (tl)   tl.classList.add('mobile-hidden');
  } else if (tab === 'timeline') {
    if (pool) pool.classList.add('mobile-hidden');
    if (tl)   tl.classList.remove('mobile-hidden');
    // A redraw requested while the timeline was display:none was deferred
    // (zero-width guard in drawTimeline) — run it now that it can measure.
    if (window._tlNeedsRedraw && typeof drawTimeline === 'function') {
      window._tlNeedsRedraw = false;
      drawTimeline();
    }
  }

  tabs.forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
}
window.mobileTab = mobileTab;
