// Subtle always-on ambient background: a sparse drifting node field with
// faint connecting lines when nodes are near each other, evoking a
// peer-to-peer network. Deliberately understated (low particle count, low
// opacity) since this sits behind a working dashboard, not a marketing
// page — and it pauses itself whenever the tab is hidden or the user
// prefers reduced motion, so it never becomes the thing eating the CPU
// this app exists to protect.
(() => {
  "use strict";

  const canvas = document.getElementById("bg-canvas");
  if (!canvas || typeof THREE === "undefined") return;

  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    canvas.remove();
    return;
  }

  const NODE_COUNT = 46;
  const LINK_DIST = 1.7;

  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.75));

  const scene = new THREE.Scene();
  const camera = new THREE.OrthographicCamera(-8, 8, 4.5, -4.5, 0.1, 10);
  camera.position.z = 5;

  const colors = [0x6d5bf6, 0x0fb896, 0x3b82f6];
  const nodes = [];
  for (let i = 0; i < NODE_COUNT; i++) {
    nodes.push({
      pos: new THREE.Vector3((Math.random() - 0.5) * 17, (Math.random() - 0.5) * 10, 0),
      vel: new THREE.Vector3((Math.random() - 0.5) * 0.12, (Math.random() - 0.5) * 0.12, 0),
      color: new THREE.Color(colors[i % colors.length]),
    });
  }

  const dotGeo = new THREE.BufferGeometry();
  const dotPositions = new Float32Array(NODE_COUNT * 3);
  const dotColors = new Float32Array(NODE_COUNT * 3);
  nodes.forEach((n, i) => {
    n.color.toArray(dotColors, i * 3);
  });
  dotGeo.setAttribute("position", new THREE.BufferAttribute(dotPositions, 3));
  dotGeo.setAttribute("color", new THREE.BufferAttribute(dotColors, 3));
  const dotMat = new THREE.PointsMaterial({ size: 0.09, vertexColors: true, transparent: true, opacity: 0.55 });
  const points = new THREE.Points(dotGeo, dotMat);
  scene.add(points);

  const MAX_LINES = NODE_COUNT * 6;
  const lineGeo = new THREE.BufferGeometry();
  const linePositions = new Float32Array(MAX_LINES * 2 * 3);
  lineGeo.setAttribute("position", new THREE.BufferAttribute(linePositions, 3));
  const lineMat = new THREE.LineBasicMaterial({ color: 0x6d5bf6, transparent: true, opacity: 0.1 });
  const lines = new THREE.LineSegments(lineGeo, lineMat);
  scene.add(lines);

  function resize() {
    const w = window.innerWidth, h = window.innerHeight;
    renderer.setSize(w, h);
    const aspect = w / h;
    const viewH = 9;
    camera.left = (-viewH * aspect) / 2;
    camera.right = (viewH * aspect) / 2;
    camera.top = viewH / 2;
    camera.bottom = -viewH / 2;
    camera.updateProjectionMatrix();
  }
  window.addEventListener("resize", resize);
  resize();

  let running = true;
  document.addEventListener("visibilitychange", () => {
    running = !document.hidden;
    if (running) animate();
  });

  const clock = new THREE.Clock();
  let rafId = null;

  function animate() {
    if (!running) return;
    rafId = requestAnimationFrame(animate);
    const dt = Math.min(clock.getDelta(), 0.1);

    const halfW = (camera.right - camera.left) / 2 + 1;
    const halfH = (camera.top - camera.bottom) / 2 + 1;
    nodes.forEach((n, i) => {
      n.pos.addScaledVector(n.vel, dt * 6);
      if (n.pos.x > halfW) n.pos.x = -halfW;
      if (n.pos.x < -halfW) n.pos.x = halfW;
      if (n.pos.y > halfH) n.pos.y = -halfH;
      if (n.pos.y < -halfH) n.pos.y = halfH;
      dotPositions[i * 3] = n.pos.x;
      dotPositions[i * 3 + 1] = n.pos.y;
      dotPositions[i * 3 + 2] = 0;
    });
    dotGeo.attributes.position.needsUpdate = true;

    let segCount = 0;
    for (let i = 0; i < NODE_COUNT && segCount < MAX_LINES; i++) {
      for (let j = i + 1; j < NODE_COUNT && segCount < MAX_LINES; j++) {
        const d = nodes[i].pos.distanceTo(nodes[j].pos);
        if (d < LINK_DIST) {
          const base = segCount * 6;
          linePositions[base] = nodes[i].pos.x;
          linePositions[base + 1] = nodes[i].pos.y;
          linePositions[base + 2] = 0;
          linePositions[base + 3] = nodes[j].pos.x;
          linePositions[base + 4] = nodes[j].pos.y;
          linePositions[base + 5] = 0;
          segCount++;
        }
      }
    }
    lineGeo.setDrawRange(0, segCount * 2);
    lineGeo.attributes.position.needsUpdate = true;

    renderer.render(scene, camera);
  }
  animate();
})();
