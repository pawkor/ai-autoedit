document.addEventListener('DOMContentLoaded', function () {
  // ── Left sidebar (project list) ──────────────────────────────────────────
  const sidebar        = document.getElementById('sidebar');
  const sidebarOverlay = document.getElementById('sidebar-overlay');
  const btnOpen        = document.getElementById('btn-hamburger-open');
  const btnClose       = document.getElementById('btn-hamburger');

  function openSidebar() {
    sidebar.classList.add('open');
    sidebarOverlay.classList.add('open');
  }
  function closeSidebar() {
    sidebar.classList.remove('open');
    sidebarOverlay.classList.remove('open');
  }

  // On mobile: show sidebar by default (project list instead of welcome screen)
  openSidebar();

  btnOpen.addEventListener('click', openSidebar);
  btnClose.addEventListener('click', closeSidebar);
  sidebarOverlay.addEventListener('click', closeSidebar);

  document.getElementById('job-list').addEventListener('click', closeSidebar);
  document.getElementById('btn-new').addEventListener('click', closeSidebar);

  // ── Right inspector ───────────────────────────────────────────────────────
  const inspector        = document.getElementById('inspector');
  const inspectorOverlay = document.getElementById('inspector-overlay');
  const btnInspOpen      = document.getElementById('btn-inspector-open');

  function openInspector() {
    inspector.classList.add('open');
    inspectorOverlay.classList.add('open');
  }
  function closeInspector() {
    inspector.classList.remove('open');
    inspectorOverlay.classList.remove('open');
  }

  if (btnInspOpen) btnInspOpen.addEventListener('click', openInspector);
  if (inspectorOverlay) inspectorOverlay.addEventListener('click', closeInspector);
});
