(() => {
  "use strict";

  const devicesEl = document.getElementById("devices");
  const logBody = document.getElementById("log-body");
  const startBtn = document.getElementById("start-btn");
  const addHelperBtn = document.getElementById("add-helper");
  const helperList = document.getElementById("helper-list");
  const numImagesInput = document.getElementById("num-images");
  const runStatusEl = document.getElementById("run-status");
  const runStatusText = document.getElementById("run-status-text");
  const summaryStrip = document.getElementById("summary-strip");
  const summaryJob = document.getElementById("summary-job");
  const summaryElapsed = document.getElementById("summary-elapsed");
  const summaryImages = document.getElementById("summary-images");
  const summaryResult = document.getElementById("summary-result");

  const MAX_LOG_LINES = 400;
  const cardEls = new Map(); // device id -> { root, sparks: {cpu,ram,free_gb} }
  let elapsedTimer = null;
  let jobStartTs = null;

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

  // ---------------- start flow ----------------
  startBtn.addEventListener("click", async () => {
    const helpers = getHelperAddresses();
    const numImages = Math.max(1, Math.min(200, parseInt(numImagesInput.value, 10) || 24));

    startBtn.disabled = true;
    startBtn.textContent = "Running...";
    clearLog();
    setRunStatus("running", "Starting...");
    summaryStrip.hidden = false;
    summaryJob.textContent = "—";
    summaryResult.textContent = "—";
    summaryImages.textContent = `0 / ${numImages}`;
    startElapsedTimer();

    try {
      const resp = await fetch("/api/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ helpers, num_images: numImages }),
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
    line.className = "log-line";
    const t = new Date((evt.ts || Date.now() / 1000) * 1000);
    const timeStr = t.toLocaleTimeString([], { hour12: false });
    line.innerHTML = `<span class="log-time">${timeStr}</span><span class="log-msg level-${evt.level || "info"}"></span>`;
    line.querySelector(".log-msg").textContent = evt.message || "";
    logBody.appendChild(line);
    while (logBody.children.length > MAX_LOG_LINES) {
      logBody.removeChild(logBody.firstChild);
    }
    logBody.scrollTop = logBody.scrollHeight;
  }

  // ---------------- device cards ----------------
  function ensureCard(device) {
    if (cardEls.has(device.id)) return cardEls.get(device.id);

    const root = document.createElement("article");
    root.className = "device-card " + (device.kind === "local" ? "local" : "helper");
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
    `;
    root.querySelector(".device-name-text").textContent = device.name;
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

    drawSpark(sparks.cpu, device.history.cpu, 100, "#5b9dff");
    drawSpark(sparks.ram, device.history.ram, 100, "#f5b94d");
    const ramCap = Math.max(device.ram_total_gb || 1, 1);
    drawSpark(sparks.free_gb, device.history.free_gb, ramCap, "#4fd1c5");
  }

  function drawSpark(canvas, values, maxValue, color) {
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const w = canvas.width;
    const h = canvas.height;
    ctx.clearRect(0, 0, w, h);
    if (!values || values.length === 0) return;

    const n = values.length;
    const max = Math.max(maxValue, ...values, 0.001);
    const stepX = n > 1 ? w / (n - 1) : w;

    ctx.beginPath();
    values.forEach((v, i) => {
      const x = i * stepX;
      const y = h - (Math.max(0, Math.min(v, max)) / max) * (h - 4) - 2;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.lineJoin = "round";
    ctx.stroke();

    ctx.lineTo(w, h);
    ctx.lineTo(0, h);
    ctx.closePath();
    ctx.fillStyle = color + "22";
    ctx.fill();
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

  function handleEvent(evt) {
    switch (evt.type) {
      case "stats":
        (evt.devices || []).forEach(renderDevice);
        break;
      case "log":
        appendLog(evt);
        break;
      case "job_start":
        summaryJob.textContent = evt.job_id;
        summaryImages.textContent = `0 / ${evt.num_images}`;
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
  }

  init();
})();
