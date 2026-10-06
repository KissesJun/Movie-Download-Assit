/* Runs before styles load; skin changes do not reload the app or its data. */
(function () {
  "use strict";
  const names = {fluent: "Fluent", win98: "Win98"};
  const key = "workbenchTheme";
  const valid = value => Object.prototype.hasOwnProperty.call(names, value);
  const root = document.documentElement;
  let saved;
  try { saved = localStorage.getItem(key); } catch {}
  const requested = new URLSearchParams(location.search).get("theme");
  let current = valid(requested) ? requested : valid(saved) ? saved : "fluent";
  root.dataset.theme = current;
  if (valid(requested)) {
    try { localStorage.setItem(key, current); } catch {}
  }

  function updateControls() {
    const label = document.getElementById("skinLabel");
    const toggle = document.getElementById("skinToggle");
    if (label) label.textContent = names[current];
    if (toggle) toggle.setAttribute("aria-label", "皮肤，当前为 " + names[current]);
    document.querySelectorAll("[data-skin]").forEach(button => {
      const selected = button.dataset.skin === current;
      button.setAttribute("aria-pressed", String(selected));
      const check = button.querySelector(".skin-check");
      if (check) check.hidden = !selected;
    });
  }

  let revision = 0;
  function selectTheme(theme) {
    if (!valid(theme)) return;
    const windowPosition = {left: window.scrollX, top: window.scrollY, behavior: "instant"};
    const scrollPositions = [...document.querySelectorAll(".table-scroll, .sidebar-list, .engine-scroll")]
      .map(element => ({element, left: element.scrollLeft, top: element.scrollTop}));
    const selection = ++revision;
    current = theme;
    root.dataset.theme = current;
    try { localStorage.setItem(key, current); } catch {}
    const url = new URL(location.href);
    url.searchParams.set("theme", current);
    history.replaceState(history.state, "", url);
    updateControls();
    document.getElementById("skinPicker").open = false;
    document.getElementById("skinToggle").focus({preventScroll: true});
    requestAnimationFrame(() => {
      if (selection !== revision) return;
      for (const position of scrollPositions) {
        position.element.scrollLeft = position.left;
        position.element.scrollTop = position.top;
      }
      window.scrollTo(windowPosition);
    });
  }

  function initializePicker() {
    const picker = document.getElementById("skinPicker");
    if (!picker) return;
    updateControls();
    picker.querySelectorAll("[data-skin]").forEach(button => {
      button.addEventListener("click", () => selectTheme(button.dataset.skin));
    });
    document.addEventListener("click", event => {
      if (!picker.contains(event.target)) picker.open = false;
    });
    document.addEventListener("keydown", event => {
      if (event.key === "Escape" && picker.open) {
        picker.open = false;
        document.getElementById("skinToggle").focus({preventScroll: true});
        event.preventDefault();
      }
    });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initializePicker, {once: true});
  } else {
    initializePicker();
  }
})();
