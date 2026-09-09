(() => {
  "use strict";

  const statusLine = document.getElementById("receiver-status-line");
  const statusText = document.getElementById("receiver-status-text");
  const toggleBtn = document.getElementById("receiver-toggle");
  const addressEl = document.getElementById("receiver-address");
  const devicesEl = document.getElementById("devices");
  const activityBody = document.getElementById("activity-body");

  const HISTORY_LEN = 60;
  const history = { cpu: [], ram: [], free_gb: [] };
  let ramTotalGb = 1;
  let accepting = true;
  let busy = false;
  let toggling = false;

  addressEl.textContent = `${window.location.host}`;

  // ---------------- self device card (built once) ----------------
  devicesEl.innerHTML = `
    <article class="device-card local" id="self-card">
      <div class="device-card-header">
        <div class="device-name">
          <span class="device-name-text">This device</span>
          <span class="tag-local">this device</span>
        </div>
        <div class="status-pill"><span class="dot"></span><span class="status-text">--</span></div>
      </div>
      <div class="metrics-row">
        <div class="metric">
          <div class="metric-label"><span>CPU</span><span class="metric-value" data-field="cpu">--</span></div>
          <canvas class="spark" data-metric="cpu" width="140" height="34"></canvas>
        </div>
        <div class="metric">
          <div class="metric-label"><span>RAM</span><span class="metric-value" data-field="ram">--</span></div>
          <canvas class="spark" data-metric="ram" width="140" height="34"></canvas>
        </div>
        <div class="metric">
          <div class="metric-label"><span>Free RAM</span><span class="metric-value" data-field="free_gb">--</span></div>
          <canvas class="spark" data-metric="free_gb" width="140" height="34"></canvas>
        </div>
      </div>
    </article>
  `;
  const cardEl = document.getElementById("self-card");
  const sparks = {
    cpu: cardEl.querySelector('canvas[data-metric="cpu"]'),
    ram: cardEl.querySelector('canvas[data-metric="ram"]'),
    free_gb: cardEl.querySelector('canvas[data-metric="free_gb"]'),
  };

  function pushHistory(key, value) {
    const arr = history[key];
    arr.push(value);
    if (arr.length > HISTORY_LEN) arr.shift();
  }

  function renderStatus() {
    const pill = cardEl.querySelector(".status-pill");
    const label = busy ? "busy" : accepting ? "idle" : "paused";
    pill.className = "status-pill status-" + label;
    pill.querySelector(".status-text").textContent = label;

    if (busy) {
      statusLine.className = "receiver-status-line live";
      statusText.textContent = "Receiving — processing work now";
    } else if (accepting) {
      statusLine.className = "receiver-status-line live";
      statusText.textContent = "Receiving — idle, ready for work";
    } else {
      statusLine.className = "receiver-status-line";
      statusText.textContent = "Paused — not accepting new work";
    }

    toggleBtn.textContent = accepting ? "Pause Receiving" : "Start Receiving";
    toggleBtn.className = "btn-receiver" + (accepting ? "" : " paused");
    toggleBtn.disabled = toggling;
  }

  async function pollStats() {
    try {
      const resp = await fetch("/stats");
      const data = await resp.json();
      ramTotalGb = Math.max(data.ram_total_gb || 1, 1);
      accepting = !!data.accepting;
      busy = data.status === "busy";

      pushHistory("cpu", data.cpu_percent);
      pushHistory("ram", data.ram_percent);
      pushHistory("free_gb", data.ram_free_gb);

      cardEl.querySelector('[data-field="cpu"]').textContent = `${data.cpu_percent.toFixed(0)}%`;
      cardEl.querySelector('[data-field="ram"]').textContent = `${data.ram_percent.toFixed(0)}%`;
      cardEl.querySelector('[data-field="free_gb"]').textContent = `${data.ram_free_gb.toFixed(1)} GB`;

      drawSpark(sparks.cpu, history.cpu, 100, "#3b82f6");
      drawSpark(sparks.ram, history.ram, 100, "#f59e0b");
      drawSpark(sparks.free_gb, history.free_gb, ramTotalGb, "#0fb896");

      renderStatus();
    } catch (_err) {
      statusLine.className = "receiver-status-line";
      statusText.textContent = "Could not reach this device's own service (unexpected).";
    }
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function timeStr(ts) {
    return new Date(ts * 1000).toLocaleTimeString([], { hour12: false });
  }

  async function pollActivity() {
    try {
      const resp = await fetch("/api/activity");
      const data = await resp.json();
      const items = data.activity || [];
      if (items.length === 0) {
        activityBody.innerHTML = '<p class="activity-empty">No work received yet.</p>';
        return;
      }
      // task_type/job_id/chunk_id are free-form strings supplied by
      // whoever sent this device work — escape before rendering.
      activityBody.innerHTML = items
        .map((item) => {
          const task = escapeHtml(item.task_type || "?");
          const jobId = escapeHtml(item.job_id || "?");
          const chunkId = escapeHtml(item.chunk_id || "?");
          return `
        <div class="activity-item">
          <span class="time">${timeStr(item.ts)}</span>
          <span class="desc">${task} — job ${jobId} / chunk ${chunkId} in ${item.elapsed_seconds}s</span>
          <span class="count">${item.count} img</span>
        </div>
      `;
        })
        .join("");
    } catch (_err) {
      // Keep showing the last known activity rather than clearing it on a blip.
    }
  }

  toggleBtn.addEventListener("click", async () => {
    toggling = true;
    renderStatus();
    try {
      const resp = await fetch("/api/toggle", { method: "POST" });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        if (typeof showToast === "function") {
          showToast(data.error || "Could not change receiving state.", resp.status === 429 ? "warn" : "error");
        }
      } else {
        accepting = !!data.accepting;
        if (typeof showToast === "function") {
          showToast(accepting ? "Now receiving work." : "Paused — not accepting new work.", "info");
        }
      }
    } catch (_err) {
      if (typeof showToast === "function") showToast("Could not reach this device's own service.", "error");
    } finally {
      toggling = false;
      renderStatus();
    }
  });

  pollStats();
  pollActivity();
  setInterval(pollStats, 1000);
  setInterval(pollActivity, 1500);
})();
