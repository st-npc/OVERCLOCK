// Shared canvas sparkline renderer, used by both the orchestrator dashboard
// (static/app.js) and the helper Receiver page (static/receiver.js).
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
  ctx.lineWidth = 1.75;
  ctx.lineJoin = "round";
  ctx.stroke();

  ctx.lineTo(w, h);
  ctx.lineTo(0, h);
  ctx.closePath();
  ctx.fillStyle = color + "22";
  ctx.fill();
}
