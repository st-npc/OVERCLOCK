// Shared toast notifications, used by both the orchestrator dashboard
// (static/app.js) and the helper Receiver page (static/receiver.js) — same
// pattern as static/sparkline.js. Creates its own stack container lazily so
// pages just need this script tag, no extra markup.
function showToast(message, kind = "info", { duration = 4500 } = {}) {
  let stack = document.getElementById("toast-stack");
  if (!stack) {
    stack = document.createElement("div");
    stack.id = "toast-stack";
    stack.setAttribute("role", "status");
    stack.setAttribute("aria-live", "polite");
    document.body.appendChild(stack);
  }

  const icons = { success: "✓", error: "✕", warn: "⚠", info: "ℹ" };
  const toast = document.createElement("div");
  toast.className = "toast";
  toast.dataset.kind = kind;
  toast.innerHTML = `
    <span class="toast-icon" aria-hidden="true">${icons[kind] || icons.info}</span>
    <span class="toast-body"></span>
    <button class="toast-close" type="button" aria-label="Dismiss notification">&times;</button>
  `;
  toast.querySelector(".toast-body").textContent = String(message);
  stack.appendChild(toast);

  let dismissed = false;
  const dismiss = () => {
    if (dismissed) return;
    dismissed = true;
    toast.classList.add("leaving");
    setTimeout(() => toast.remove(), 260);
  };

  toast.querySelector(".toast-close").addEventListener("click", dismiss);
  if (duration > 0) setTimeout(dismiss, duration);
  return dismiss;
}
