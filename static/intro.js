// "Break the screen" intro gate. Builds a small stylized PC out of three.js
// primitives (no external model assets), lets the user tap/click the
// monitor to crack it, and shatters it away once enough taps land to
// reveal the real dashboard underneath. Skippable, reduced-motion-aware,
// and fully tears itself down (stops the render loop, disposes GPU
// resources) once dismissed — this is a one-time flourish, not something
// that should keep burning CPU/GPU in the background of a tool whose
// whole point is honest resource accounting.
(() => {
  "use strict";

  const SEEN_KEY = "overclock_intro_seen";
  const TAPS_TO_BREAK = 9;

  const overlay = document.getElementById("intro-overlay");
  const canvas = document.getElementById("intro-canvas");
  const hint = document.getElementById("intro-hint");
  const progressEl = document.getElementById("intro-progress");
  const skipBtn = document.getElementById("intro-skip");

  if (!overlay || !canvas || typeof THREE === "undefined") {
    if (overlay) overlay.remove();
    return;
  }

  const prefersReducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const forceReplay = new URLSearchParams(window.location.search).get("intro") === "1";
  let alreadySeen = false;
  try {
    alreadySeen = localStorage.getItem(SEEN_KEY) === "1";
  } catch (_err) {
    alreadySeen = false;
  }

  if ((alreadySeen && !forceReplay) || prefersReducedMotion) {
    dismissInstant();
    return;
  }

  if (forceReplay) {
    // Drop the query param so a plain refresh afterward goes back to the
    // normal one-time-per-browser behavior instead of replaying forever.
    const url = new URL(window.location.href);
    url.searchParams.delete("intro");
    window.history.replaceState({}, "", url);
  }

  function markSeen() {
    try {
      localStorage.setItem(SEEN_KEY, "1");
    } catch (_err) {
      // localStorage unavailable (private mode etc.) — fine, just replays next time
    }
  }

  function dismissInstant() {
    markSeen();
    overlay.style.transition = "none";
    overlay.style.opacity = "0";
    overlay.style.display = "none";
  }

  // ---------------- scene setup ----------------
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(46, 1, 0.1, 100);
  camera.position.set(0, 0.2, 4.7);

  const accent = new THREE.Color(0x6d5bf6);
  const teal = new THREE.Color(0x0fb896);
  const amber = new THREE.Color(0xf59e0b);
  const pink = new THREE.Color(0xff4fd8);

  scene.add(new THREE.AmbientLight(0xbfc4ff, 0.7));
  const key = new THREE.DirectionalLight(0xffffff, 1.05);
  key.position.set(3, 4, 5);
  scene.add(key);
  const rim = new THREE.PointLight(accent.getHex(), 2.4, 24);
  rim.position.set(-3.2, 1.6, 3.2);
  scene.add(rim);
  const rim2 = new THREE.PointLight(teal.getHex(), 2.0, 24);
  rim2.position.set(3.2, -0.8, 2.6);
  scene.add(rim2);
  const rim3 = new THREE.PointLight(pink.getHex(), 1.6, 22);
  rim3.position.set(0, 2.4, 3.5);
  scene.add(rim3);

  // Cheap fake bloom: soft additive-blended radial sprites parked at each
  // colored light, since r128 has no postprocessing pipeline wired up here.
  function makeGlowSprite(color, size, opacity) {
    const c = document.createElement("canvas");
    c.width = c.height = 128;
    const gctx = c.getContext("2d");
    const g = gctx.createRadialGradient(64, 64, 0, 64, 64, 64);
    g.addColorStop(0, `rgba(${color.r * 255},${color.g * 255},${color.b * 255},${opacity})`);
    g.addColorStop(1, "rgba(0,0,0,0)");
    gctx.fillStyle = g;
    gctx.fillRect(0, 0, 128, 128);
    const tex = new THREE.CanvasTexture(c);
    const mat = new THREE.SpriteMaterial({ map: tex, blending: THREE.AdditiveBlending, depthWrite: false, transparent: true });
    const sprite = new THREE.Sprite(mat);
    sprite.scale.set(size, size, 1);
    return sprite;
  }
  const glow1 = makeGlowSprite(accent, 5.5, 0.8);
  glow1.position.copy(rim.position);
  scene.add(glow1);
  const glow2 = makeGlowSprite(teal, 5.5, 0.8);
  glow2.position.copy(rim2.position);
  scene.add(glow2);
  const glow3 = makeGlowSprite(pink, 4.5, 0.6);
  glow3.position.copy(rim3.position);
  scene.add(glow3);

  const pc = new THREE.Group();
  pc.scale.set(1.34, 1.34, 1.34);
  scene.add(pc);

  // Monitor body (bezel) — dark glossy shell so the RGB glow reads brightly against it.
  const bezelMat = new THREE.MeshStandardMaterial({
    color: 0x1a1230, roughness: 0.32, metalness: 0.55, emissive: accent.getHex(), emissiveIntensity: 0.18,
  });
  const bezel = new THREE.Mesh(new THREE.BoxGeometry(2.7, 1.7, 0.12), bezelMat);
  bezel.position.set(0, 0.5, 0);
  pc.add(bezel);

  // Thin glowing rim frame around the screen — the main "RGB" accent, hue-cycled.
  const rimFrameGeo = new THREE.EdgesGeometry(new THREE.BoxGeometry(2.5, 1.58, 0.02));
  const rimFrame = new THREE.LineSegments(rimFrameGeo, new THREE.LineBasicMaterial({ color: accent.getHex(), linewidth: 2 }));
  rimFrame.position.set(0, 0.5, 0.07);
  pc.add(rimFrame);

  // Screen: a looping coding video (CC0 stock footage, vendored locally —
  // see README) composited live with a separate crack overlay so tapping
  // still cracks the "glass" without disturbing the video underneath.
  // Falls back to a static branded screen if the video can't play.
  const SCREEN_W = 640, SCREEN_H = 360;
  const screenCanvas = document.createElement("canvas");
  screenCanvas.width = SCREEN_W;
  screenCanvas.height = SCREEN_H;
  const sctx = screenCanvas.getContext("2d");
  const screenTexture = new THREE.CanvasTexture(screenCanvas);

  const crackCanvas = document.createElement("canvas");
  crackCanvas.width = SCREEN_W;
  crackCanvas.height = SCREEN_H;
  const cctx = crackCanvas.getContext("2d");

  function paintFallbackBase() {
    const g = sctx.createLinearGradient(0, 0, SCREEN_W, SCREEN_H);
    g.addColorStop(0, "#2d1f6b");
    g.addColorStop(0.5, "#3a1f6e");
    g.addColorStop(1, "#0fb896");
    sctx.fillStyle = g;
    sctx.fillRect(0, 0, SCREEN_W, SCREEN_H);

    sctx.strokeStyle = "rgba(180,255,240,0.22)";
    sctx.lineWidth = 1;
    for (let x = 0; x < SCREEN_W; x += 28) {
      sctx.beginPath(); sctx.moveTo(x, 0); sctx.lineTo(x, SCREEN_H); sctx.stroke();
    }
    for (let y = 0; y < SCREEN_H; y += 28) {
      sctx.beginPath(); sctx.moveTo(0, y); sctx.lineTo(SCREEN_W, y); sctx.stroke();
    }

    sctx.font = "bold 46px 'Courier New', monospace";
    sctx.fillStyle = "rgba(255,255,255,0.92)";
    sctx.textAlign = "center";
    sctx.fillText("OVERCLOCK", SCREEN_W / 2, SCREEN_H / 2 - 6);
    sctx.font = "16px 'Courier New', monospace";
    sctx.fillStyle = "rgba(139,255,230,0.75)";
    sctx.fillText("tap to break in", SCREEN_W / 2, SCREEN_H / 2 + 26);
  }
  paintFallbackBase();

  const codingVideo = document.createElement("video");
  codingVideo.src = "/static/media/coding.mp4";
  codingVideo.loop = true;
  codingVideo.muted = true;
  codingVideo.defaultMuted = true;
  codingVideo.playsInline = true;
  codingVideo.autoplay = true;
  codingVideo.preload = "auto";
  let videoReady = false;
  codingVideo.addEventListener("canplay", () => { videoReady = true; });
  codingVideo.addEventListener("error", () => { videoReady = false; });
  const playPromise = codingVideo.play();
  if (playPromise && playPromise.catch) playPromise.catch(() => {});

  function compositeScreen() {
    if (videoReady && codingVideo.readyState >= 2) {
      sctx.drawImage(codingVideo, 0, 0, SCREEN_W, SCREEN_H);
      sctx.fillStyle = "rgba(10, 8, 30, 0.22)";
      sctx.fillRect(0, 0, SCREEN_W, SCREEN_H); // subtle tint so RGB glow still reads over the footage
    }
    sctx.drawImage(crackCanvas, 0, 0);
    screenTexture.needsUpdate = true;
  }

  const screenMat = new THREE.MeshBasicMaterial({ map: screenTexture });
  const screenMesh = new THREE.Mesh(new THREE.PlaneGeometry(2.42, 1.36), screenMat);
  screenMesh.position.set(0, 0.5, 0.065);
  pc.add(screenMesh);

  // Stand
  const standMat = new THREE.MeshStandardMaterial({ color: 0x241a3d, roughness: 0.4, metalness: 0.6 });
  const neck = new THREE.Mesh(new THREE.CylinderGeometry(0.06, 0.06, 0.55, 12), standMat);
  neck.position.set(0, -0.55, 0);
  pc.add(neck);
  const base = new THREE.Mesh(new THREE.CylinderGeometry(0.55, 0.6, 0.06, 24), standMat);
  base.position.set(0, -0.85, 0);
  pc.add(base);

  // Tower (case) beside the monitor, with a bright hue-cycling RGB LED strip
  // and a tinted glass side panel so it reads as a colorful gaming rig.
  const towerMat = new THREE.MeshStandardMaterial({
    color: 0x1c1533, roughness: 0.28, metalness: 0.6, emissive: teal.getHex(), emissiveIntensity: 0.12,
  });
  const tower = new THREE.Mesh(new THREE.BoxGeometry(0.7, 1.9, 1.1), towerMat);
  tower.position.set(2.05, -0.35, -0.3);
  pc.add(tower);
  const panelMat = new THREE.MeshStandardMaterial({
    color: teal.getHex(), roughness: 0.1, metalness: 0.2, transparent: true, opacity: 0.35,
    emissive: teal.getHex(), emissiveIntensity: 0.5,
  });
  const panel = new THREE.Mesh(new THREE.BoxGeometry(0.02, 1.6, 0.85), panelMat);
  panel.position.set(2.05 + 0.36, -0.35, -0.3);
  pc.add(panel);
  const ledMat = new THREE.MeshBasicMaterial({ color: accent.getHex() });
  const led = new THREE.Mesh(new THREE.BoxGeometry(0.06, 1.7, 0.06), ledMat);
  led.position.set(2.05 - 0.32, -0.35, 0.28);
  pc.add(led);

  pc.position.set(0, -0.1, 0);

  // ---------------- shard fragments (built once, hidden until shatter) ----------------
  const SHARD_COLS = 4, SHARD_ROWS = 3;
  const shards = [];
  const shardGroup = new THREE.Group();
  shardGroup.visible = false;
  pc.add(shardGroup);
  {
    const SCREEN_PLANE_H = 1.36;
    const w = 2.42 / SHARD_COLS, h = SCREEN_PLANE_H / SHARD_ROWS;
    for (let r = 0; r < SHARD_ROWS; r++) {
      for (let c = 0; c < SHARD_COLS; c++) {
        const geo = new THREE.PlaneGeometry(w * 0.96, h * 0.96);
        const uv = geo.attributes.uv;
        const u0 = c / SHARD_COLS, u1 = (c + 1) / SHARD_COLS;
        const v0 = 1 - (r + 1) / SHARD_ROWS, v1 = 1 - r / SHARD_ROWS;
        uv.setXY(0, u0, v1); uv.setXY(1, u1, v1); uv.setXY(2, u0, v0); uv.setXY(3, u1, v0);
        const mesh = new THREE.Mesh(geo, screenMat.clone());
        mesh.material.transparent = true;
        const x = -2.42 / 2 + w * (c + 0.5);
        const y = 0.5 - SCREEN_PLANE_H / 2 + h * (r + 0.5);
        mesh.position.set(x, y, 0.066);
        mesh.userData.home = { x, y };
        shardGroup.add(mesh);
        shards.push(mesh);
      }
    }
  }

  // ---------------- crack drawing (on the separate overlay canvas) ----------------
  function drawCrackBranch(x, y, angle, length, depth) {
    if (depth <= 0 || length < 4) return;
    const x2 = x + Math.cos(angle) * length;
    const y2 = y + Math.sin(angle) * length;
    cctx.beginPath();
    cctx.moveTo(x, y);
    cctx.lineTo(x2, y2);
    cctx.stroke();
    const branches = Math.random() < 0.55 ? 2 : 1;
    for (let i = 0; i < branches; i++) {
      const a = angle + (Math.random() - 0.5) * 1.4;
      drawCrackBranch(x2, y2, a, length * (0.55 + Math.random() * 0.25), depth - 1);
    }
  }

  function crackAt(u, v) {
    const x = u * SCREEN_W, y = (1 - v) * SCREEN_H;
    cctx.strokeStyle = "rgba(255,255,255,0.9)";
    cctx.lineWidth = 1.6;
    cctx.shadowColor = "rgba(139,255,230,0.7)";
    cctx.shadowBlur = 3;
    const spokes = 5 + Math.floor(Math.random() * 3);
    for (let i = 0; i < spokes; i++) {
      const angle = (Math.PI * 2 * i) / spokes + Math.random() * 0.4;
      drawCrackBranch(x, y, angle, 30 + Math.random() * 40, 4);
    }
    cctx.shadowBlur = 0;
    cctx.fillStyle = "rgba(255,255,255,0.95)";
    cctx.beginPath();
    cctx.arc(x, y, 3, 0, Math.PI * 2);
    cctx.fill();
  }

  // ---------------- interaction ----------------
  const raycaster = new THREE.Raycaster();
  const pointerNdc = new THREE.Vector2();
  const mouseParallax = { x: 0, y: 0 };
  let tapCount = 0;
  let broken = false;
  let rafId = null;

  function updateHud() {
    const remaining = Math.max(0, TAPS_TO_BREAK - tapCount);
    progressEl.textContent = remaining > 0 ? "●".repeat(tapCount) + "○".repeat(remaining) : "";
    hint.textContent = remaining > 0 ? (tapCount === 0 ? "tap the screen" : `${remaining} more...`) : "breaking in...";
  }
  updateHud();

  function onPointerDown(evt) {
    if (broken) return;
    const point = evt.touches ? evt.touches[0] : evt;
    const rect = canvas.getBoundingClientRect();
    pointerNdc.x = ((point.clientX - rect.left) / rect.width) * 2 - 1;
    pointerNdc.y = -((point.clientY - rect.top) / rect.height) * 2 + 1;

    raycaster.setFromCamera(pointerNdc, camera);
    const hits = raycaster.intersectObject(screenMesh);
    if (hits.length > 0 && hits[0].uv) {
      crackAt(hits[0].uv.x, hits[0].uv.y);
    } else {
      crackAt(0.5 + (Math.random() - 0.5) * 0.4, 0.5 + (Math.random() - 0.5) * 0.4);
    }

    // tactile camera jolt
    camera.position.x += (Math.random() - 0.5) * 0.06;
    camera.position.y += (Math.random() - 0.5) * 0.06;

    tapCount += 1;
    updateHud();
    if (tapCount >= TAPS_TO_BREAK) {
      shatter();
    }
  }
  overlay.addEventListener("pointerdown", onPointerDown);

  function onPointerMove(evt) {
    const point = evt.touches ? evt.touches[0] : evt;
    mouseParallax.x = (point.clientX / window.innerWidth) * 2 - 1;
    mouseParallax.y = (point.clientY / window.innerHeight) * 2 - 1;
  }
  overlay.addEventListener("pointermove", onPointerMove);

  function shatter() {
    broken = true;
    hint.textContent = "";
    progressEl.textContent = "";
    screenMesh.visible = false;
    shardGroup.visible = true;
    shards.forEach((mesh) => {
      mesh.userData.vel = new THREE.Vector3(
        (mesh.userData.home.x) * 0.6 + (Math.random() - 0.5) * 0.5,
        (Math.random() - 0.2) * 1.2 + 0.4,
        1.2 + Math.random() * 1.5
      );
      mesh.userData.angVel = new THREE.Vector3(
        (Math.random() - 0.5) * 6, (Math.random() - 0.5) * 6, (Math.random() - 0.5) * 6
      );
    });
    overlay.classList.add("shattering");
    setTimeout(finish, 1100);
  }

  function finish() {
    markSeen();
    overlay.style.opacity = "0";
    setTimeout(teardown, 650);
  }

  function teardown() {
    overlay.style.display = "none";
    overlay.removeEventListener("pointerdown", onPointerDown);
    overlay.removeEventListener("pointermove", onPointerMove);
    if (rafId) cancelAnimationFrame(rafId);
    codingVideo.pause();
    codingVideo.removeAttribute("src");
    codingVideo.load();
    shards.forEach((m) => { m.geometry.dispose(); m.material.dispose(); });
    screenMesh.geometry.dispose();
    bezel.geometry.dispose(); bezel.material.dispose();
    screenMat.dispose();
    rimFrameGeo.dispose(); rimFrame.material.dispose();
    towerMat.dispose(); tower.geometry.dispose();
    panelMat.dispose(); panel.geometry.dispose();
    ledMat.dispose(); led.geometry.dispose();
    standMat.dispose(); neck.geometry.dispose(); base.geometry.dispose();
    [glow1, glow2, glow3].forEach((s) => { s.material.map.dispose(); s.material.dispose(); });
    renderer.dispose();
  }

  skipBtn.addEventListener("click", () => {
    if (!broken) {
      broken = true;
      finish();
    }
  });

  function resize() {
    const w = window.innerWidth, h = window.innerHeight;
    renderer.setSize(w, h);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  }
  window.addEventListener("resize", resize);
  resize();

  const clock = new THREE.Clock();
  const hueColor = new THREE.Color();
  let elapsed = 0;
  function animate() {
    rafId = requestAnimationFrame(animate);
    const dt = Math.min(clock.getDelta(), 0.05);
    elapsed += dt;

    if (!broken) {
      pc.rotation.y += dt * 0.12;
      camera.position.x += (mouseParallax.x * 0.6 - camera.position.x) * 0.04;
      camera.position.y += (0.15 - mouseParallax.y * 0.35 - camera.position.y) * 0.04;
      camera.lookAt(0, 0.15, 0);

      // Slow RGB "gaming PC" hue chase across the rim frame + LED strip,
      // offset from each other so it reads as a moving light, not a blink.
      const hue1 = (elapsed * 0.06) % 1;
      hueColor.setHSL(hue1, 0.9, 0.6);
      rimFrame.material.color.copy(hueColor);
      const hue2 = (elapsed * 0.06 + 0.4) % 1;
      hueColor.setHSL(hue2, 0.9, 0.58);
      ledMat.color.copy(hueColor);

      compositeScreen();
    } else {
      shards.forEach((mesh) => {
        const v = mesh.userData.vel;
        if (!v) return;
        v.y -= dt * 2.6; // gravity
        mesh.position.x += v.x * dt;
        mesh.position.y += v.y * dt;
        mesh.position.z += v.z * dt;
        mesh.rotation.x += mesh.userData.angVel.x * dt;
        mesh.rotation.y += mesh.userData.angVel.y * dt;
        mesh.material.opacity = Math.max(0, mesh.material.opacity - dt * 0.6);
      });
    }

    renderer.render(scene, camera);
  }
  animate();
})();
