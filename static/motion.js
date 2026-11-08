(() => {
  'use strict';
  const panel = document.querySelector('.flow-panel');
  const toggle = document.querySelector('#flow-motion-toggle');
  if (!panel || !toggle) return;

  const label = toggle.querySelector('.flow-toggle-state');
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  let paused = false;

  function updateMotion() {
    const reduced = reducedMotion.matches;
    const running = !paused && !reduced;
    panel.dataset.motionPaused = String(!running);
    toggle.setAttribute('aria-pressed', String(running));
    toggle.disabled = reduced;
    label.textContent = reduced ? 'reduced' : running ? 'on' : 'paused';
    toggle.title = reduced ? 'Animation is off because of your system motion preference.' : running ? 'Pause animation' : 'Resume animation';
  }
  function updateVisibility() {
    panel.dataset.pageHidden = String(document.hidden);
  }

  toggle.addEventListener('click', () => { paused = !paused; updateMotion(); });
  document.addEventListener('visibilitychange', updateVisibility);
  if (reducedMotion.addEventListener) reducedMotion.addEventListener('change', updateMotion);
  else if (reducedMotion.addListener) reducedMotion.addListener(updateMotion);
  updateVisibility();
  updateMotion();
})();
