document.addEventListener('DOMContentLoaded', function () {
  const sidebar   = document.getElementById('sidebar');
  const overlay   = document.getElementById('sidebar-overlay');
  const btnOpen   = document.getElementById('btn-hamburger-open');
  const btnClose  = document.getElementById('btn-hamburger');

  function openSidebar() {
    sidebar.classList.add('open');
    overlay.classList.add('open');
  }

  function closeSidebar() {
    sidebar.classList.remove('open');
    overlay.classList.remove('open');
  }

  // On mobile: show sidebar by default (project list instead of welcome screen)
  openSidebar();

  btnOpen.addEventListener('click', openSidebar);
  btnClose.addEventListener('click', closeSidebar);
  overlay.addEventListener('click', closeSidebar);

  // Close when user picks a job or starts a new one
  document.getElementById('job-list').addEventListener('click', closeSidebar);
  document.getElementById('btn-new').addEventListener('click', closeSidebar);
});
