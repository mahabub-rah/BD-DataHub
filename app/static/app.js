// BD DataHub — small progressive enhancements (the site works without JS).
(function () {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));

  // Mobile navigation.
  const navBtn = $(".nav-toggle"), nav = $(".nav");
  if (navBtn && nav) navBtn.addEventListener("click", () => {
    nav.classList.toggle("open");
    navBtn.setAttribute("aria-expanded", nav.classList.contains("open"));
  });

  // Citation tabs + copy buttons.
  $$("[data-tabs]").forEach((wrap) => {
    const buttons = $$("[role=tab]", wrap);
    buttons.forEach((b) => b.addEventListener("click", () => {
      buttons.forEach((x) => x.setAttribute("aria-selected", x === b));
      $$("[role=tabpanel]", wrap).forEach((p) => { p.hidden = p.id !== b.getAttribute("aria-controls"); });
    }));
  });
  $$("[data-copy]").forEach((btn) => btn.addEventListener("click", async () => {
    const target = $("[role=tabpanel]:not([hidden]) .cite-text", btn.closest("[data-tabs]"));
    if (!target) return;
    try {
      await navigator.clipboard.writeText(target.innerText.trim());
      const old = btn.textContent; btn.textContent = "Copied ✓";
      setTimeout(() => (btn.textContent = old), 1500);
    } catch (e) { window.getSelection().selectAllChildren(target); }
  }));

  // Auto-submit selects marked data-autosubmit (sort order).
  $$("select[data-autosubmit]").forEach((s) => s.addEventListener("change", () => s.form.submit()));
})();
