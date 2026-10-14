// Copy buttons on the landing page and the "Copy as Markdown" action on docs pages.
(function () {
  function flash(btn, text) {
    const old = btn.dataset.label || btn.textContent;
    btn.dataset.label = old;
    btn.classList.add("done");
    btn.lastChild.textContent = " " + text;
    setTimeout(() => { btn.classList.remove("done"); btn.lastChild.textContent = old.startsWith(" ") ? old : " " + old.trim(); }, 1600);
  }

  async function copy(text) {
    try { await navigator.clipboard.writeText(text); return true; } catch (_) { return false; }
  }

  function setup() {
    for (const box of document.querySelectorAll("[data-copy]:not([data-ready])")) {
      box.dataset.ready = "1";
      const btn = box.querySelector(".agt-copy");
      if (btn) btn.addEventListener("click", async () => { if (await copy(box.dataset.copy)) flash(btn, "Copied"); });
    }
    for (const bar of document.querySelectorAll(".agt-ai-actions:not([data-ready])")) {
      bar.dataset.ready = "1";
      const btn = bar.querySelector(".agt-copy-md");
      btn.addEventListener("click", async () => {
        try {
          const res = await fetch(new URL(bar.dataset.md, location.href));
          if (!res.ok) throw new Error(res.status);
          if (await copy(await res.text())) flash(btn, "Copied");
        } catch (_) { flash(btn, "Unavailable"); }
      });
    }
  }

  if (window.document$ && typeof window.document$.subscribe === "function") window.document$.subscribe(setup);
  else if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", setup);
  else setup();
})();
