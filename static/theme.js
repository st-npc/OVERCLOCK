// Wires up the #theme-toggle button present on both the dashboard
// (templates/index.html) and the Receiver page (templates/receiver.html).
// Pairs with static/theme-init.js, which applies the remembered choice
// before first paint — this file only needs to handle clicks afterward.
function initThemeToggle() {
  const btn = document.getElementById("theme-toggle");
  if (!btn) return;

  const STORAGE_KEY = "overclock_theme";
  const mql = window.matchMedia("(prefers-color-scheme: dark)");

  function currentTheme() {
    const attr = document.documentElement.getAttribute("data-theme");
    if (attr === "dark" || attr === "light") return attr;
    return mql.matches ? "dark" : "light";
  }

  function apply(theme, persist) {
    document.documentElement.setAttribute("data-theme", theme);
    btn.setAttribute("aria-pressed", String(theme === "dark"));
    if (persist) {
      try {
        localStorage.setItem(STORAGE_KEY, theme);
      } catch (_err) {
        // localStorage unavailable — the toggle still works for this tab,
        // it just won't be remembered on reload.
      }
    }
  }

  apply(currentTheme(), false);

  btn.addEventListener("click", () => {
    apply(currentTheme() === "dark" ? "light" : "dark", true);
  });
}

document.addEventListener("DOMContentLoaded", initThemeToggle);
