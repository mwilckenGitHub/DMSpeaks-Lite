'use strict';

// Keep a description attached even when its visual bubble is dismissed.
// Moving from a button into its tooltip should not make the text disappear.
(() => {
  let current;
  let hideTimer;
  function closeTip() {
    clearTimeout(hideTimer);
    if (current) current.hidden = true;
    current = null;
  }
  function leave(control, tip) {
    clearTimeout(hideTimer);
    hideTimer = setTimeout(() => {
      if (current === tip && !control.matches(':hover, :focus') && !tip.matches(':hover')) closeTip();
    }, 120);
  }
  for (const control of document.querySelectorAll('[data-tip]')) {
    const tip = document.createElement('div');
    tip.id = control.id + '-tip';
    tip.className = 'control-tip';
    tip.setAttribute('role', 'tooltip');
    tip.textContent = liteLocale.message(control.dataset.tip);
    tip.hidden = true;
    document.body.append(tip);
    const descriptions = [control.getAttribute('aria-describedby'), tip.id].filter(Boolean);
    control.setAttribute('aria-describedby', descriptions.join(' '));
    function show() {
      closeTip();
      current = tip;
      tip.hidden = false;
      const anchor = control.getBoundingClientRect();
      const box = tip.getBoundingClientRect();
      const margin = 8;
      const start = document.documentElement.dir === 'rtl' ? anchor.right - box.width : anchor.left;
      tip.style.left = Math.max(margin, Math.min(start, document.documentElement.clientWidth - box.width - margin)) + 'px';
      const below = anchor.bottom + 2;
      tip.style.top = (below + box.height <= innerHeight - margin ? below : Math.max(margin, anchor.top - box.height - 2)) + 'px';
    }
    control.addEventListener('focus', show);
    control.addEventListener('blur', () => leave(control, tip));
    control.addEventListener('pointerenter', event => { if (event.pointerType !== 'touch') show(); });
    control.addEventListener('pointerleave', () => leave(control, tip));
    control.addEventListener('click', closeTip);
    tip.addEventListener('pointerenter', () => clearTimeout(hideTimer));
    tip.addEventListener('pointerleave', () => leave(control, tip));
  }
  document.addEventListener('keydown', event => { if (event.key === 'Escape') closeTip(); });
  window.addEventListener('scroll', closeTip, true);
  window.addEventListener('resize', closeTip);
})();
