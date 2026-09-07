"""Orchestrator + dashboard service. This is the app the user opens in a
browser and clicks "Start" on. HTTP wiring only — job logic lives in
orchestrator.py, device state lives in device_monitor.py.
"""
import argparse
import json
import queue
import threading
import time

from flask import Flask, Response, jsonify, render_template, request

from device_monitor import DeviceMonitor
from interfaces import list_network_interfaces
from orchestrator import JobAlreadyRunningError, Orchestrator

app = Flask(__name__)

monitor = DeviceMonitor()
monitor.start()


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
def api_status():
    return jsonify(
        {
            "devices": monitor.snapshot_all(),
            "job_running": orchestrator.is_running(),
            "job_id": orchestrator.current_job_id,
        }
    )


@app.get("/api/interfaces")
def api_interfaces():
    return jsonify({"interfaces": list_network_interfaces()})


@app.post("/api/start")
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

    helpers = []
    seen = set()
    for h in raw_helpers:
        if not isinstance(h, str):
            continue
        addr = h.strip()
        if addr and addr not in seen:
            seen.add(addr)
            helpers.append(addr)

    num_images = payload.get("num_images", 24)
    if not isinstance(num_images, int) or not (1 <= num_images <= 200):
        return jsonify({"error": "'num_images' must be an integer between 1 and 200"}), 400

    for addr in helpers:
        monitor.add_helper(addr)

    try:
        orchestrator.start_job_async(helpers, num_images)
    except JobAlreadyRunningError:
        return jsonify({"error": "a job is already running"}), 409

    return jsonify({"ok": True, "helpers": helpers, "num_images": num_images})


@app.get("/api/events")
def api_events():
    def format_sse(event: dict) -> str:
        return f"data: {json.dumps(event)}\n\n"

    def stream():
        client_queue = events.subscribe()
        try:
            # Send an immediate snapshot so a newly-opened tab isn't blank
            # until the next 1s tick.
            yield format_sse({"type": "stats", "devices": monitor.snapshot_all(), "ts": time.time()})
            last_stats = time.time()
            while True:
                try:
                    event = client_queue.get(timeout=1.0)
                    yield format_sse(event)
                except queue.Empty:
                    pass
                now = time.time()
                if now - last_stats >= 1.0:
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


@app.errorhandler(404)
def not_found(_err):
    return jsonify({"error": "not found"}), 404


@app.errorhandler(500)
def server_error(_err):
    return jsonify({"error": "internal server error"}), 500


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Overclock orchestrator + dashboard")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    print(f"Overclock dashboard: http://localhost:{args.port}")
    app.run(host=args.host, port=args.port, threaded=True)
