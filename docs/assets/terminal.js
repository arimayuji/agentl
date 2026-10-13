// Animated terminal on the landing page: install -> validate -> run -> dashboard.
(function () {
  const script = [
    { cmd: "pip install agentl" },
    { out: "Successfully installed agentl-0.1.0", cls: "ok" },
    { cmd: "agentl validate pipeline.yaml" },
    { out: "ok: invoices (5 steps)", cls: "ok" },
    { cmd: "agentl run pipeline.yaml" },
    { out: "run.started {\"steps\": 5}" },
    { out: "step.succeeded [invoices] {\"records\": 4020}" },
    { out: "step.succeeded [clients] {\"records\": 312}" },
    { out: "step.succeeded [matched] {\"records\": 4020}" },
    { out: "item.rejected [classify] {\"count\": 12, \"rate\": 0.003}", cls: "warn" },
    { out: "step.succeeded [classify] {\"records\": 4008}" },
    { out: "step.succeeded [save] {\"records\": 1}" },
    { out: "run.succeeded {\"seconds\": 41.2, \"rejected\": 12}", cls: "ok" },
    { cmd: "agentl dashboard" },
    { out: "agentl dashboard: http://127.0.0.1:7070", cls: "link" },
  ];
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  async function play(el) {
    if (el.dataset.playing) return;
    el.dataset.playing = "1";
    const body = el.querySelector(".agt-body");
    while (true) {
      body.textContent = "";
      for (const line of script) {
        const row = document.createElement("div");
        body.appendChild(row);
        if (line.cmd) {
          row.className = "agt-cmd";
          if (reduced) { row.textContent = line.cmd; continue; }
          for (const ch of line.cmd) { row.textContent += ch; await sleep(35); }
          await sleep(350);
        } else {
          row.className = "agt-out " + (line.cls || "");
          row.textContent = line.out;
          await sleep(reduced ? 0 : 220);
        }
      }
      if (reduced) return;
      await sleep(4000);
    }
  }

  function init() {
    document.querySelectorAll(".agt-terminal").forEach((el) => {
      const io = new IntersectionObserver((entries) => {
        if (entries.some((e) => e.isIntersecting)) { io.disconnect(); play(el); }
      });
      io.observe(el);
    });
  }
  // Material's instant navigation swaps pages without a reload.
  if (window.document$) window.document$.subscribe(init); else document.addEventListener("DOMContentLoaded", init);
})();
