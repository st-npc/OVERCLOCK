(() => {
  "use strict";

  const devicesEl = document.getElementById("devices");
  const logBody = document.getElementById("log-body");
  const logFilters = document.getElementById("log-filters");
  const startBtn = document.getElementById("start-btn");
  const addHelperBtn = document.getElementById("add-helper");
  const helperList = document.getElementById("helper-list");
  const numImagesInput = document.getElementById("num-images");
  const taskTypeSelect = document.getElementById("task-type");
  const passphraseInput = document.getElementById("passphrase");
  const runStatusEl = document.getElementById("run-status");
  const runStatusText = document.getElementById("run-status-text");
  const summaryStrip = document.getElementById("summary-strip");
  const summaryJob = document.getElementById("summary-job");
  const summaryElapsed = document.getElementById("summary-elapsed");
  const summaryImages = document.getElementById("summary-images");
  const summaryResult = document.getElementById("summary-result");
  const summarySaved = document.getElementById("summary-saved");
  const summaryCompression = document.getElementById("summary-compression");
  const heroValueEl = document.getElementById("hero-stat-value");
  const heroSubEl = document.getElementById("hero-stat-sub");
  const splitEmpty = document.getElementById("split-empty");
  const splitBarEl = document.getElementById("split-bar");
  const splitLegendEl = document.getElementById("split-legend");
  const splitTotalEl = document.getElementById("split-total");

  const MAX_LOG_LINES = 400;
  const DEVICE_COLOR_COUNT = 6;
  const cardEls = new Map(); // device id -> { root, sparks: {cpu,ram,free_gb} }
  const deviceColorIndex = new Map();
  const deviceNames = new Map();
  let elapsedTimer = null;
  let jobStartTs = null;

  let splitCounts = {};
  let splitTotal = 0;
  let doneCounts = {};
  let doneElapsed = {};
  let deviceLoad = {};
  let failedTargets = new Set();

  function colorForDevice(id) {
    if (!deviceColorIndex.has(id)) {
      deviceColorIndex.set(id, deviceColorIndex.size % DEVICE_COLOR_COUNT);
    }
    return `var(--dev-color-${deviceColorIndex.get(id)})`;
  }

  // ---------------- helper row management ----------------
  addHelperBtn.addEventListener("click", () => addHelperRow());

  function addHelperRow(value = "") {
    const row = document.createElement("div");
    row.className = "helper-row";
    row.innerHTML = `
      <input type="text" class="helper-input" placeholder="e.g. 192.168.1.42:5001" value="${escapeAttr(value)}">
      <button class="btn-icon remove-helper" title="Remove this helper" type="button">&times;</button>
    `;
    row.querySelector(".remove-helper").addEventListener("click", () => row.remove());
    helperList.appendChild(row);
  }

  helperList.querySelectorAll(".remove-helper").forEach((btn) => {
    btn.addEventListener("click", () => btn.closest(".helper-row").remove());
  });

  function getHelperAddresses() {
    return Array.from(helperList.querySelectorAll(".helper-input"))
      .map((i) => i.value.trim())
      .filter(Boolean);
  }

  function escapeAttr(s) {
    return String(s).replace(/"/g, "&quot;");
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  // ---------------- start flow ----------------
  async function loadTaskChoices() {
    try {
      const resp = await fetch("/api/tasks");
      const data = await resp.json();
      const choices = data.tasks || [];
      taskTypeSelect.innerHTML = choices.map((t) => `<option value="${t.id}">${escapeHtml(t.name)}</option>`).join("");
      if (data.default) taskTypeSelect.value = data.default;
    } catch (_err) {
      taskTypeSelect.innerHTML = '<option value="image">Image batch</option>';
    }
  }

  startBtn.addEventListener("click", async () => {
    const helpers = getHelperAddresses();
    const numImages = Math.max(1, Math.min(200, parseInt(numImagesInput.value, 10) || 24));
    const taskType = taskTypeSelect.value || "image";
    const passphrase = passphraseInput.value || null;

    startBtn.disabled = true;
    startBtn.textContent = "Running...";
    clearLog();
    resetSplit();
    setRunStatus("running", "Starting...");
    summaryStrip.hidden = false;
    summaryJob.textContent = "—";
    summaryResult.textContent = "—";
    summarySaved.textContent = "—";
    summaryCompression.textContent = "—";
    summaryImages.textContent = `0 / ${numImages}`;
    startElapsedTimer();

    try {
      const resp = await fetch("/api/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ helpers, num_images: numImages, task_type: taskType, passphrase }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        appendLog({ level: "error", message: data.error || `Failed to start (HTTP ${resp.status})`, ts: Date.now() / 1000 });
        resetStartButton();
        setRunStatus("error", "Failed to start");
        stopElapsedTimer();
      }
    } catch (err) {
      appendLog({ level: "error", message: `Could not reach the orchestrator: ${err}`, ts: Date.now() / 1000 });
      resetStartButton();
      setRunStatus("error", "Connection error");
      stopElapsedTimer();
    }
  });

  function resetStartButton() {
    startBtn.disabled = false;
    startBtn.textContent = "Start";
  }

  function setRunStatus(kind, text) {
    runStatusEl.className = "run-status " + kind;
    runStatusText.textContent = text;
  }

  function startElapsedTimer() {
    jobStartTs = Date.now();
    stopElapsedTimer();
    elapsedTimer = setInterval(() => {
      const secs = (Date.now() - jobStartTs) / 1000;
      summaryElapsed.textContent = secs.toFixed(1) + "s";
    }, 200);
  }

  function stopElapsedTimer() {
    if (elapsedTimer) {
      clearInterval(elapsedTimer);
      elapsedTimer = null;
    }
  }

  // ---------------- log panel ----------------
  function clearLog() {
    logBody.innerHTML = "";
  }

  function appendLog(evt) {
    const line = document.createElement("div");
    const level = evt.level || "info";
    line.className = "log-line";
    line.dataset.level = level;
    const t = new Date((evt.ts || Date.now() / 1000) * 1000);
    const timeStr = t.toLocaleTimeString([], { hour12: false });
    line.innerHTML = `<span class="log-time">${timeStr}</span><span class="log-msg level-${level}"></span>`;
    line.querySelector(".log-msg").textContent = evt.message || "";
    logBody.appendChild(line);
    while (logBody.children.length > MAX_LOG_LINES) {
      logBody.removeChild(logBody.firstChild);
    }
    logBody.scrollTop = logBody.scrollHeight;
  }

  logFilters.addEventListener("click", (e) => {
    const btn = e.target.closest(".log-filter-chip");
    if (!btn) return;
    logFilters.querySelectorAll(".log-filter-chip").forEach((c) => c.classList.remove("active"));
    btn.classList.add("active");
    const level = btn.dataset.level;
    if (level === "all") {
      delete logBody.dataset.filter;
    } else {
      logBody.dataset.filter = level;
    }
  });

  // ---------------- work split panel ----------------
  function resetSplit() {
    splitCounts = {};
    splitTotal = 0;
    doneCounts = {};
    doneElapsed = {};
    deviceLoad = {};
    failedTargets = new Set();
    splitEmpty.hidden = false;
    splitBarEl.hidden = true;
    splitBarEl.innerHTML = "";
    splitLegendEl.innerHTML = "";
    splitTotalEl.textContent = "";
  }

  function handleSplit(evt) {
    splitCounts = evt.counts || {};
    splitTotal = Object.values(splitCounts).reduce((a, b) => a + b, 0);
    doneCounts = {};
    doneElapsed = {};
    deviceLoad = {};
    failedTargets = new Set();
    renderSplit();
  }

  function handleChunkDone(evt) {
    doneCounts[evt.target] = (doneCounts[evt.target] || 0) + evt.count;
    doneElapsed[evt.target] = (doneElapsed[evt.target] || 0) + (evt.elapsed || 0);
    renderSplit();
  }

  function handleChunkFailed(evt) {
    failedTargets.add(evt.target);
    renderSplit();
  }

  function renderSplit() {
    const targets = Object.keys(splitCounts);
    if (targets.length === 0 || splitTotal === 0) return;

    splitEmpty.hidden = true;
    splitBarEl.hidden = false;

    const doneTotal = Object.values(doneCounts).reduce((a, b) => a + b, 0);
    splitTotalEl.textContent = `${Math.min(doneTotal, splitTotal)} / ${splitTotal} done`;

    splitBarEl.innerHTML = targets
      .map((t) => {
        const pct = ((splitCounts[t] / splitTotal) * 100).toFixed(2);
        const color = colorForDevice(t);
        const isFailed = failedTargets.has(t);
        const isDone = !isFailed && (doneCounts[t] || 0) >= splitCounts[t];
        const cls = isFailed ? "failed" : isDone ? "done" : "pending";
        const name = deviceNames.get(t) || t;
        const tooltip = `${name}: ${splitCounts[t]} image(s)${isFailed ? " — dropped, reassigned" : ""}`;
        return `<div class="split-seg ${cls}" style="flex-basis:${pct}%;background:${color}" data-tooltip="${escapeAttr(tooltip)}"></div>`;
      })
      .join("");

    const allTargets = new Set([...targets, ...Object.keys(doneCounts)]);
    splitLegendEl.innerHTML = Array.from(allTargets)
      .map((t) => {
        const done = doneCounts[t] || 0;
        const planned = splitCounts[t] || 0;
        const display = Math.max(done, planned) === planned && done === 0 ? planned : done;
        const color = colorForDevice(t);
        const isComplete = done >= planned && planned > 0 && !failedTargets.has(t);
        const name = deviceNames.get(t) || t;
        const elapsed = doneElapsed[t] || 0;
        const throughput = isComplete && elapsed > 0 ? ` <span class="split-legend-rate">(${(done / elapsed).toFixed(1)} img/s)</span>` : "";
        const load = deviceLoad[t];
        const loadHtml = load
          ? `<div class="split-legend-load">CPU ${load.baseline_cpu.toFixed(0)}%&rarr;${load.peak_cpu.toFixed(0)}% &middot; RAM ${load.baseline_ram.toFixed(0)}%&rarr;${load.peak_ram.toFixed(0)}%</div>`
          : "";
        return `<div class="split-legend-item ${isComplete ? "is-done" : ""}">
          <span class="split-legend-swatch" style="background:${color}"></span>
          <div class="split-legend-text">
            <div class="split-legend-main">${escapeHtml(name)}: <span class="split-legend-count">${display}</span>${throughput}</div>
            ${loadHtml}
          </div>
        </div>`;
      })
      .join("");
  }

  // ---------------- device cards ----------------
  function ensureCard(device) {
    if (cardEls.has(device.id)) return cardEls.get(device.id);

    const color = colorForDevice(device.id);
    const root = document.createElement("article");
    root.className = "device-card entering " + (device.kind === "local" ? "local" : "helper");
    root.style.setProperty("--dev-accent", color);
    root.innerHTML = `
      <div class="device-card-header">
        <div class="device-name">
          <span class="device-name-text"></span>
          ${device.kind === "local" ? '<span class="tag-local">this device</span>' : ""}
        </div>
        <div class="status-pill"><span class="dot"></span><span class="status-text"></span></div>
      </div>
      <div class="metrics-row">
        ${metricBlock("cpu", "CPU")}
        ${metricBlock("ram", "RAM")}
        ${metricBlock("free_gb", "Free RAM")}
      </div>
      <div class="capacity-row">
        <div class="capacity-label"><span>Spare capacity</span><span class="capacity-value">0%</span></div>
        <div class="capacity-bar"><div class="capacity-fill"></div></div>
      </div>
      <div class="device-error" hidden></div>
      <div class="device-extra"></div>
    `;
    root.querySelector(".device-name-text").textContent = device.name;
    root.addEventListener("click", () => root.classList.toggle("expanded"));
    devicesEl.appendChild(root);

    const sparks = {
      cpu: root.querySelector('canvas[data-metric="cpu"]'),
      ram: root.querySelector('canvas[data-metric="ram"]'),
      free_gb: root.querySelector('canvas[data-metric="free_gb"]'),
    };
    const entry = { root, sparks };
    cardEls.set(device.id, entry);
    return entry;
  }

  function metricBlock(key, label) {
    return `
      <div class="metric">
        <div class="metric-label"><span>${label}</span><span class="metric-value" data-field="${key}">--</span></div>
        <canvas class="spark" data-metric="${key}" width="140" height="34"></canvas>
      </div>
    `;
  }

  function renderDevice(device) {
    deviceNames.set(device.id, device.name);
    const { root, sparks } = ensureCard(device);

    const pill = root.querySelector(".status-pill");
    pill.className = "status-pill status-" + device.status;
    pill.querySelector(".status-text").textContent = device.status;

    root.querySelector('[data-field="cpu"]').textContent = `${device.cpu_percent.toFixed(0)}%`;
    root.querySelector('[data-field="ram"]').textContent = `${device.ram_percent.toFixed(0)}%`;
    root.querySelector('[data-field="free_gb"]').textContent = `${device.ram_free_gb.toFixed(1)} GB`;

    const capPct = Math.round((device.spare_score || 0) * 100);
    root.querySelector(".capacity-value").textContent = `${capPct}%`;
    root.querySelector(".capacity-fill").style.width = `${capPct}%`;

    const errEl = root.querySelector(".device-error");
    if (device.last_error && device.status === "unreachable") {
      errEl.hidden = false;
      errEl.textContent = device.last_error;
    } else {
      errEl.hidden = true;
    }

    root.querySelector(".device-extra").innerHTML = `
      <span>RAM: ${device.ram_free_gb.toFixed(2)} GB free / ${device.ram_total_gb.toFixed(2)} GB total</span>
      <span>Status: ${device.status}${device.last_error ? " — " + escapeHtml(device.last_error) : ""}</span>
      <span>Click card to collapse</span>
    `;

    drawSpark(sparks.cpu, device.history.cpu, 100, "#3b82f6");
    drawSpark(sparks.ram, device.history.ram, 100, "#f59e0b");
    const ramCap = Math.max(device.ram_total_gb || 1, 1);
    drawSpark(sparks.free_gb, device.history.free_gb, ramCap, "#0fb896");
  }

  // ---------------- SSE ----------------
  function connectEvents() {
    const es = new EventSource("/api/events");
    es.onmessage = (msg) => {
      let evt;
      try {
        evt = JSON.parse(msg.data);
      } catch (_err) {
        return;
      }
      handleEvent(evt);
    };
    es.onerror = () => {
      // EventSource auto-reconnects; just reflect it visually if we were mid-run.
    };
  }

  // ---------------- hero stat ----------------
  let heroTweenRaf = null;
  let heroDisplayed = null;
  function animateHeroValue(target) {
    const start = heroDisplayed == null ? target : heroDisplayed;
    const t0 = performance.now();
    const DURATION = 500;
    if (heroTweenRaf) cancelAnimationFrame(heroTweenRaf);
    function step(now) {
      const p = Math.min(1, (now - t0) / DURATION);
      const eased = 1 - Math.pow(1 - p, 3);
      const value = start + (target - start) * eased;
      heroValueEl.textContent = Math.round(value);
      if (p < 1) {
        heroTweenRaf = requestAnimationFrame(step);
      } else {
        heroDisplayed = target;
      }
    }
    heroTweenRaf = requestAnimationFrame(step);
  }

  function renderHeroStat(devices) {
    if (!heroValueEl || !devices || devices.length === 0) return;
    const usable = devices.filter((d) => d.status !== "unreachable" && d.status !== "paused");
    const avgSpare = usable.length
      ? usable.reduce((sum, d) => sum + (d.spare_score || 0), 0) / usable.length
      : 0;
    animateHeroValue(Math.round(avgSpare * 100));
    const n = devices.length;
    heroSubEl.textContent = n === 1 ? "across 1 device" : `across ${n} devices`;
  }

  function handleEvent(evt) {
    switch (evt.type) {
      case "stats":
        (evt.devices || []).forEach(renderDevice);
        renderHeroStat(evt.devices || []);
        break;
      case "log":
        appendLog(evt);
        break;
      case "job_start":
        summaryJob.textContent = evt.task_name ? `${evt.job_id} (${evt.task_name})` : evt.job_id;
        summaryImages.textContent = `0 / ${evt.num_images}`;
        break;
      case "split":
        handleSplit(evt);
        break;
      case "chunk_done":
        handleChunkDone(evt);
        break;
      case "chunk_failed":
        handleChunkFailed(evt);
        break;
      case "done":
        stopElapsedTimer();
        resetStartButton();
        summaryJob.textContent = evt.job_id || summaryJob.textContent;
        if (evt.ok) {
          summaryResult.textContent = `${evt.saved} saved`;
          summaryImages.textContent = `${evt.saved} / ${evt.saved + (evt.missing || 0)}`;
          setRunStatus("ok", "Complete");
        } else {
          summaryResult.textContent = evt.error ? "error" : `${evt.missing || 0} missing`;
          setRunStatus(evt.error ? "error" : "warn", evt.error ? "Failed" : "Completed with issues");
        }

        if (evt.time_saved_seconds != null && evt.estimated_local_seconds != null) {
          const pct = evt.estimated_local_seconds > 0 ? Math.round((evt.time_saved_seconds / evt.estimated_local_seconds) * 100) : 0;
          summarySaved.textContent =
            evt.time_saved_seconds > 0.01
              ? `${evt.time_saved_seconds.toFixed(2)}s faster (~${pct}%)`
              : "no faster (fully local)";
        } else {
          summarySaved.textContent = "—";
        }

        summaryCompression.textContent =
          evt.avg_compression_ratio != null ? `${Math.round(evt.avg_compression_ratio * 100)}% of original` : "—";

        deviceLoad = evt.device_load || {};
        renderSplit();
        break;
      default:
        break;
    }
  }

  // ---------------- network interfaces ----------------
  const ifaceListEl = document.getElementById("iface-list");
  const refreshIfacesBtn = document.getElementById("refresh-ifaces");

  async function loadInterfaces() {
    try {
      const resp = await fetch("/api/interfaces");
      const data = await resp.json();
      renderInterfaces(data.interfaces || []);
    } catch (_err) {
      ifaceListEl.innerHTML = '<span class="iface-empty">Could not detect local interfaces.</span>';
    }
  }

  function renderInterfaces(ifaces) {
    if (ifaces.length === 0) {
      ifaceListEl.innerHTML = '<span class="iface-empty">No active network interfaces detected.</span>';
      return;
    }
    ifaceListEl.innerHTML = ifaces
      .map((iface) => {
        const usbTag = iface.likely_usb ? '<span class="iface-usb-tag">USB link</span>' : "";
        return `
          <div class="iface-chip ${iface.likely_usb ? "usb" : ""}">
            <span class="iface-name">${iface.name}</span>
            <span>${iface.ip}:5000</span>
            ${usbTag}
          </div>
        `;
      })
      .join("");
  }

  refreshIfacesBtn.addEventListener("click", loadInterfaces);

  // ---------------- local overload simulator ----------------
  const stressCpuInput = document.getElementById("stress-cpu");
  const stressRamInput = document.getElementById("stress-ram");
  const stressToggleBtn = document.getElementById("stress-toggle");
  const stressBadge = document.getElementById("stress-badge");

  function renderStressStatus(status) {
    if (status.active) {
      stressToggleBtn.textContent = "Stop Overload";
      stressToggleBtn.classList.add("active");
      stressBadge.hidden = false;
      stressBadge.textContent = `ACTIVE — ${status.cpu_workers} CPU worker(s), ${status.ram_mb} MB RAM`;
      stressCpuInput.disabled = true;
      stressRamInput.disabled = true;
    } else {
      stressToggleBtn.textContent = "Start Overload";
      stressToggleBtn.classList.remove("active");
      stressBadge.hidden = true;
      stressCpuInput.disabled = false;
      stressRamInput.disabled = false;
    }
  }

  async function loadStressStatus() {
    try {
      const resp = await fetch("/api/load");
      const status = await resp.json();
      if (status.max_cpu_workers) stressCpuInput.max = status.max_cpu_workers;
      if (status.max_ram_mb) stressRamInput.max = status.max_ram_mb;
      renderStressStatus(status);
    } catch (_err) {
      // leave controls as-is; next poll will reconcile
    }
  }

  stressToggleBtn.addEventListener("click", async () => {
    stressToggleBtn.disabled = true;
    try {
      const isActive = stressToggleBtn.classList.contains("active");
      const resp = await fetch(isActive ? "/api/load/stop" : "/api/load/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: isActive
          ? undefined
          : JSON.stringify({
              cpu_workers: parseInt(stressCpuInput.value, 10) || 0,
              ram_mb: parseInt(stressRamInput.value, 10) || 0,
            }),
      });
      const status = await resp.json();
      if (!resp.ok) {
        appendLog({ level: "error", message: status.error || "Could not change overload state", ts: Date.now() / 1000 });
      } else {
        renderStressStatus(status);
      }
    } catch (err) {
      appendLog({ level: "error", message: `Could not reach the orchestrator: ${err}`, ts: Date.now() / 1000 });
    } finally {
      stressToggleBtn.disabled = false;
    }
  });

  // ---------------- init ----------------
  async function init() {
    try {
      const resp = await fetch("/api/status");
      const data = await resp.json();
      (data.devices || []).forEach(renderDevice);
      if (data.job_running) {
        startBtn.disabled = true;
        startBtn.textContent = "Running...";
        setRunStatus("running", "Running...");
        summaryStrip.hidden = false;
        startElapsedTimer();
      }
    } catch (_err) {
      appendLog({ level: "warn", message: "Could not load initial status from the orchestrator.", ts: Date.now() / 1000 });
    }
    connectEvents();
    loadInterfaces();
    loadTaskChoices();
    loadStressStatus();
    setInterval(loadStressStatus, 4000);
  }

  init();
})();
