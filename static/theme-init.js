// Applies a remembered theme choice before first paint, so switching pages
// (or reloading) never flashes the wrong theme. Deliberately tiny, external
// (the dashboard's CSP has no 'unsafe-inline' for scripts), and loaded
// blocking in <head> — the whole point is running before the stylesheet
// paints anything.
(() => {
  try {
    const stored = localStorage.getItem("overclock_theme");
    if (stored === "dark" || stored === "light") {
      document.documentElement.setAttribute("data-theme", stored);
    }
  } catch (_err) {
    // localStorage unavailable (private mode etc.) — falls back to the OS
    // prefers-color-scheme setting, which the CSS already handles.
  }
})();
