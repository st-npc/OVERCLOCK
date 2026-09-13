"""Orchestrator + dashboard service. This is the app the user opens in a
browser and clicks "Start" on. HTTP wiring only — job logic lives in
orchestrator.py, device state lives in device_monitor.py.
"""
import argparse
import datetime
import json
import os
import queue
import threading
import time

from flask import Flask, Response, jsonify, render_template, request

import discovery
import net_client
import scheduler
from device_monitor import DeviceMonitor
from flask_common import install_error_handlers, install_security_headers
from interfaces import list_network_interfaces
from orchestrator import JobAlreadyRunningError, Orchestrator
from security import RateLimiter, constant_time_eq, rate_limited
from stress import LoadGenerator
from tasks import DEFAULT_TASK, TASKS, task_choices

app = Flask(__name__)
# Control payloads here are small JSON bodies (a helper list, a batch size);
# nothing here legitimately needs to be large. Caps an unauthenticated
# large-body DoS at the Flask/Werkzeug layer before a route even runs.
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024
# Static assets are versioned by Flask's own url_for (an mtime query string),
# so they're safe to cache aggressively — cuts repeat-visit load time.
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = datetime.timedelta(days=365)

# Optional operator lock for the dashboard's state-changing endpoints
# (starting a job, toggling the local overload simulator). Unset by default
# so the zero-config "open the dashboard, click Start" demo flow this
# project is built around keeps working exactly as before — set
# OVERCLOCK_ADMIN_TOKEN (or --admin-token) to require it, the same opt-in
# shape as the existing per-job "Security: shared-passphrase auth" (see
# README.md) but for who may drive *this* device's dashboard at all.
ADMIN_TOKEN = os.environ.get("OVERCLOCK_ADMIN_TOKEN") or None
ADMIN_TOKEN_HEADER = "X-Overclock-Admin-Token"

MAX_HELPERS = 32  # bounds the thread pool orchestrator spins up per job

monitor = DeviceMonitor()
monitor.start()

load_generator = LoadGenerator()

_start_limiter = RateLimiter(max_requests=10, window_seconds=10)
_load_limiter = RateLimiter(max_requests=20, window_seconds=10)
_read_limiter = RateLimiter(max_requests=120, window_seconds=10)
# Each call blocks ~1.5s broadcasting on the LAN — a generous quota still
# comfortably prevents someone from turning this into a broadcast-storm knob.
_discover_limiter = RateLimiter(max_requests=6, window_seconds=10)

DASHBOARD_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "media-src 'self'; "
    "connect-src 'self'; "
    "font-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


install_security_headers(app, csp=DASHBOARD_CSP)


def _admin_auth_ok() -> bool:
    if not ADMIN_TOKEN:
        return True
    provided = request.headers.get(ADMIN_TOKEN_HEADER, "")
    return constant_time_eq(provided, ADMIN_TOKEN)


def require_admin(fn):
    """Gate a mutating endpoint behind ADMIN_TOKEN, if one is configured."""
    import functools

    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        if not _admin_auth_ok():
            return jsonify({"error": "missing or invalid admin token"}), 401
        return fn(*args, **kwargs)

    return wrapped


class EventBus:
    """Fan-out pub/sub so multiple dashboard tabs can each get every event,
    not just whichever client happens to read the queue first."""

    def __init__(self):
        self._subscribers = []
        self._lock = threading.Lock()

    def subscribe(self) -> "queue.Queue":
        q = queue.Queue()
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def put(self, event):
        with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            q.put(event)


events = EventBus()
orchestrator = Orchestrator(monitor, events)


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/status")
@rate_limited(_read_limiter)
def api_status():
    return jsonify(
        {
            "devices": monitor.snapshot_all(),
            "job_running": orchestrator.is_running(),
            "job_id": orchestrator.current_job_id,
            "admin_auth_required": bool(ADMIN_TOKEN),
        }
    )


@app.get("/api/interfaces")
@rate_limited(_read_limiter)
def api_interfaces():
    return jsonify({"interfaces": list_network_interfaces()})


@app.post("/api/discover")
@rate_limited(_discover_limiter)
def api_discover():
    """Broadcast a LAN discovery request and return whatever helpers
    answered in time. Read-only from this device's point of view (it
    doesn't register the found helpers or start anything) — the dashboard
    decides what to do with the results."""
    found = discovery.discover_helpers()
    return jsonify({"helpers": found})


@app.get("/api/tasks")
@rate_limited(_read_limiter)
def api_tasks():
    return jsonify({"tasks": task_choices(), "default": DEFAULT_TASK})


@app.get("/api/scheduler")
@rate_limited(_read_limiter)
def api_scheduler():
    """Observability into the adaptive scheduler's learned state: EWMA
    throughput/overhead/reliability per device, plus which strategies are
    selectable. Read-only — exists so the dashboard (or bench/simulate.py,
    or a curious operator) can see *why* a split came out the way it did,
    instead of the scheduler being a black box."""
    return jsonify(
        {
            "strategies": list(scheduler.STRATEGIES),
            "default_strategy": scheduler.DEFAULT_STRATEGY,
            "learned": orchestrator.scheduler.snapshot(),
        }
    )


@app.get("/api/load")
@rate_limited(_read_limiter)
def api_load_status():
    return jsonify(load_generator.status())


@app.post("/api/load/start")
@rate_limited(_load_limiter)
@require_admin
def api_load_start():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "request body must be a JSON object"}), 400
    cpu_workers = payload.get("cpu_workers", 0)
    ram_mb = payload.get("ram_mb", 0)
    if not isinstance(cpu_workers, int) or isinstance(cpu_workers, bool) or not isinstance(ram_mb, int) or isinstance(ram_mb, bool):
        return jsonify({"error": "'cpu_workers' and 'ram_mb' must be integers"}), 400
    if cpu_workers <= 0 and ram_mb <= 0:
        return jsonify({"error": "set at least one of 'cpu_workers' or 'ram_mb' above zero"}), 400
    status = load_generator.start(cpu_workers, ram_mb)
    return jsonify(status)


@app.post("/api/load/stop")
@rate_limited(_load_limiter)
@require_admin
def api_load_stop():
    return jsonify(load_generator.stop())


@app.post("/api/start")
@rate_limited(_start_limiter)
@require_admin
def api_start():
    if orchestrator.is_running():
        return jsonify({"error": "a job is already running"}), 409

    raw_body = request.get_data(cache=True)
    payload = request.get_json(silent=True)
    if payload is None:
        if raw_body:
            return jsonify({"error": "request body must be valid JSON"}), 400
        payload = {}
    if not isinstance(payload, dict):
        return jsonify({"error": "request body must be a JSON object"}), 400

    raw_helpers = payload.get("helpers", [])
    if not isinstance(raw_helpers, list):
        return jsonify({"error": "'helpers' must be a list of addresses"}), 400
    if len(raw_helpers) > MAX_HELPERS:
        return jsonify({"error": f"too many helpers (max {MAX_HELPERS})"}), 400

    helpers = []
    seen = set()
    for h in raw_helpers:
        if not isinstance(h, str):
            return jsonify({"error": "each helper address must be a string"}), 400
        try:
            addr = net_client.validate_helper_address(h)
        except net_client.InvalidHelperAddress as exc:
            return jsonify({"error": f"invalid helper address {h!r}: {exc}"}), 400
        if addr not in seen:
            seen.add(addr)
            helpers.append(addr)

    num_images = payload.get("num_images", 24)
    if not isinstance(num_images, int) or isinstance(num_images, bool) or not (1 <= num_images <= 200):
        return jsonify({"error": "'num_images' must be an integer between 1 and 200"}), 400

    task_type = payload.get("task_type", DEFAULT_TASK)
    if not isinstance(task_type, str) or task_type not in TASKS:
        return jsonify({"error": f"'task_type' must be one of {sorted(TASKS.keys())}"}), 400

    passphrase = payload.get("passphrase")
    if passphrase is not None:
        if not isinstance(passphrase, str):
            return jsonify({"error": "'passphrase' must be a string"}), 400
        if len(passphrase) > 256:
            return jsonify({"error": "'passphrase' is too long"}), 400
    monitor.set_passphrase(passphrase)

    strategy = payload.get("strategy", scheduler.DEFAULT_STRATEGY)
    if not isinstance(strategy, str) or strategy not in scheduler.STRATEGIES:
        return jsonify({"error": f"'strategy' must be one of {list(scheduler.STRATEGIES)}"}), 400

    for addr in helpers:
        monitor.add_helper(addr)

    try:
        orchestrator.start_job_async(helpers, num_images, task_type, strategy)
    except JobAlreadyRunningError:
        return jsonify({"error": "a job is already running"}), 409

    return jsonify(
        {"ok": True, "helpers": helpers, "num_images": num_images, "task_type": task_type, "strategy": strategy}
    )


STATS_PUSH_INTERVAL_IDLE = 1.0
STATS_PUSH_INTERVAL_ACTIVE = 0.2  # while a job is running, so the dashboard
                                   # actually shows the CPU/RAM move in real
                                   # time instead of only once a second

MAX_SSE_CLIENTS = 64  # bounds an fd/thread exhaustion DoS from opening many
                       # concurrent /api/events connections


@app.get("/api/events")
@rate_limited(_read_limiter)
def api_events():
    if len(events._subscribers) >= MAX_SSE_CLIENTS:
        return jsonify({"error": "too many active connections — try again shortly"}), 503

    def format_sse(event: dict) -> str:
        return f"data: {json.dumps(event)}\n\n"

    def stream():
        client_queue = events.subscribe()
        try:
            # Send an immediate snapshot so a newly-opened tab isn't blank
            # until the next tick.
            yield format_sse({"type": "stats", "devices": monitor.snapshot_all(), "ts": time.time()})
            last_stats = time.time()
            while True:
                push_interval = STATS_PUSH_INTERVAL_ACTIVE if orchestrator.is_running() else STATS_PUSH_INTERVAL_IDLE
                try:
                    event = client_queue.get(timeout=push_interval)
                    yield format_sse(event)
                except queue.Empty:
                    pass
                now = time.time()
                if now - last_stats >= push_interval:
                    yield format_sse({"type": "stats", "devices": monitor.snapshot_all(), "ts": now})
                    last_stats = now
        except GeneratorExit:
            pass
        finally:
            events.unsubscribe(client_queue)

    resp = Response(stream(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


install_error_handlers(app)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Overclock orchestrator + dashboard")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument(
        "--admin-token",
        default=None,
        help="require this token (as X-Overclock-Admin-Token) to start jobs or control the "
        "overload simulator. Prefer OVERCLOCK_ADMIN_TOKEN instead — a CLI arg is visible to "
        "other local users via `ps`. Unset means the dashboard stays open, as before.",
    )
    args = parser.parse_args()
    if args.admin_token:
        ADMIN_TOKEN = args.admin_token

    print(f"Overclock dashboard: http://localhost:{args.port}")
    if ADMIN_TOKEN:
        print("Admin token required for /api/start and /api/load/* (dashboard is locked)")
    else:
        print("No admin token set — anyone who can reach this dashboard can start jobs on it.")
    app.run(host=args.host, port=args.port, threaded=True)
