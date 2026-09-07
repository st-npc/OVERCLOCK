"""Standalone relay/signaling server for cross-network connections.

Overclock normally talks directly device-to-device over LAN/USB. When two
devices are on different networks (different Wi-Fi, behind different
NATs) with no direct path, they can instead both connect out to one
relay server that both of them can reach, and tunnel requests through it
— the same shape as a TURN relay for video calls, just over plain HTTP
long-polling instead of a media protocol, which keeps this file
dependency-free and easy to self-host anywhere Python runs.

This file is standalone on purpose: it has no import on the rest of the
Overclock codebase, so it can be copied to and run on a separate host
(a small VPS, Render/Fly/Railway free tier, another always-on machine —
anything both devices can reach on a port you open) without dragging the
whole project along. THIS SERVER IS INFRASTRUCTURE YOU MUST HOST
YOURSELF somewhere reachable by both devices — Overclock's authors are
not running a public instance of it.

Protocol (all JSON over HTTP, one room = one shared code both sides use):
  POST /relay/<room>/<device_id>/request
      body: {method, path, headers, body_b64, wait}
      Enqueues the request for that device and blocks (up to `wait`
      seconds) for its response. Returns {delivered, status, headers,
      body_b64} — delivered=false means the device never answered in time
      (treated as unreachable by the caller).
  GET  /relay/<room>/<device_id>/poll?timeout=25
      The device itself long-polls this to receive queued requests.
      Returns {request: {...}} or {request: null} on timeout.
  POST /relay/<room>/<device_id>/respond/<request_id>
      body: {status, headers, body_b64}
      The device posts its answer back here.
  GET  /relay/<room>/devices
      Lists device_ids seen recently in that room (last 60s of poll
      activity) — informational, for a "who's connected" UI.
"""
import argparse
import queue
import threading
import time
import uuid

from flask import Flask, jsonify, request

app = Flask(__name__)

_lock = threading.Lock()
_inboxes = {}          # (room, device_id) -> queue.Queue of request envelopes
_pending_results = {}   # request_id -> queue.Queue(maxsize=1)
_last_seen = {}         # (room, device_id) -> monotonic timestamp

PRESENCE_WINDOW_SECONDS = 60
MAX_WAIT_SECONDS = 90


def _inbox(room, device_id):
    key = (room, device_id)
    with _lock:
        if key not in _inboxes:
            _inboxes[key] = queue.Queue()
        return _inboxes[key]


def _touch_presence(room, device_id):
    with _lock:
        _last_seen[(room, device_id)] = time.monotonic()


@app.post("/relay/<room>/<device_id>/request")
def relay_request(room, device_id):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "request body must be JSON"}), 400

    wait = payload.get("wait", 25)
    try:
        wait = min(float(wait), MAX_WAIT_SECONDS)
    except (TypeError, ValueError):
        wait = 25.0

    request_id = uuid.uuid4().hex
    envelope = {
        "id": request_id,
        "method": payload.get("method", "GET"),
        "path": payload.get("path", "/"),
        "headers": payload.get("headers") or {},
        "body_b64": payload.get("body_b64"),
    }

    result_q = queue.Queue(maxsize=1)
    with _lock:
        _pending_results[request_id] = result_q

    _inbox(room, device_id).put(envelope)

    try:
        result = result_q.get(timeout=wait)
    except queue.Empty:
        result = None
    finally:
        with _lock:
            _pending_results.pop(request_id, None)

    if result is None:
        return jsonify({"delivered": False})

    return jsonify({"delivered": True, **result})


@app.get("/relay/<room>/<device_id>/poll")
def relay_poll(room, device_id):
    _touch_presence(room, device_id)
    timeout = request.args.get("timeout", 25, type=float)
    timeout = max(1.0, min(timeout, MAX_WAIT_SECONDS))
    try:
        envelope = _inbox(room, device_id).get(timeout=timeout)
    except queue.Empty:
        return jsonify({"request": None})
    return jsonify({"request": envelope})


@app.post("/relay/<room>/<device_id>/respond/<request_id>")
def relay_respond(room, device_id, request_id):
    _touch_presence(room, device_id)
    payload = request.get_json(silent=True) or {}
    result = {
        "status": payload.get("status", 502),
        "headers": payload.get("headers") or {},
        "body_b64": payload.get("body_b64"),
    }
    with _lock:
        result_q = _pending_results.get(request_id)
    if result_q is None:
        return jsonify({"ok": False, "error": "unknown or expired request_id"})
    try:
        result_q.put_nowait(result)
    except queue.Full:
        pass
    return jsonify({"ok": True})


@app.get("/relay/<room>/devices")
def relay_devices(room):
    now = time.monotonic()
    with _lock:
        devices = [
            device_id
            for (r, device_id), seen in _last_seen.items()
            if r == room and (now - seen) <= PRESENCE_WINDOW_SECONDS
        ]
    return jsonify({"room": room, "devices": sorted(devices)})


@app.get("/")
def index():
    return jsonify({"service": "overclock-relay", "status": "up"})


@app.errorhandler(404)
def not_found(_err):
    return jsonify({"error": "not found"}), 404


@app.errorhandler(500)
def server_error(_err):
    return jsonify({"error": "internal server error"}), 500


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Overclock relay/signaling server")
    parser.add_argument("--port", type=int, default=5090)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    print(f"Overclock relay listening on {args.host}:{args.port}")
    app.run(host=args.host, port=args.port, threaded=True)
