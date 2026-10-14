// Feature clips: play while on screen, pause when off screen. If the browser blocks autoplay
// (Safari Low Power Mode, data saver) or the reader prefers reduced motion, show the controls so
// the clip can be started by hand instead of looking like a still image.
(function () {
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function setup() {
    const clips = document.querySelectorAll("video.feature-clip:not([data-ready])");
    if (!clips.length) return;
    const seen = "IntersectionObserver" in window
      ? new IntersectionObserver((entries) => {
          for (const e of entries) {
            const v = e.target;
            if (e.isIntersecting) {
              if (!reduced) v.play().catch(() => { v.controls = true; });
            } else if (!v.paused) {
              v.pause();
            }
          }
        }, { threshold: 0.35 })
      : null;
    for (const v of clips) {
      v.dataset.ready = "1";
      if (reduced) {
        v.removeAttribute("autoplay");
        v.pause();
        v.controls = true;
        continue;
      }
      if (seen) seen.observe(v);
      else v.play().catch(() => { v.controls = true; });
    }
  }

  // Material's instant navigation swaps pages without a full load; document$ fires on each one.
  if (window.document$ && typeof window.document$.subscribe === "function") {
    window.document$.subscribe(setup);
  } else if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", setup);
  } else {
    setup();
  }
})();
