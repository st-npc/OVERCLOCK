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
import re
import threading
import time
import uuid

from flask import Flask, jsonify, request

from security import RateLimiter, apply_security_headers, rate_limited

app = Flask(__name__)
# This service is explicitly meant to be deployed on the open internet (see
# the module docstring), so unlike the LAN-facing services it gets a firmer
# body-size ceiling: it only ever tunnels one /stats or /process call at a
# time, and /process's own chunk cap (500 items) plus wire.py's
# decompression-bomb guard already bound legitimate payload size.
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024

RELAY_CSP = "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"


@app.after_request
def _add_security_headers(resp):
    return apply_security_headers(resp, csp=RELAY_CSP)


_request_limiter = RateLimiter(max_requests=40, window_seconds=10)
_poll_limiter = RateLimiter(max_requests=20, window_seconds=10)

_lock = threading.Lock()
_inboxes = {}          # (room, device_id) -> queue.Queue of request envelopes
_pending_results = {}   # request_id -> queue.Queue(maxsize=1)
_last_seen = {}         # (room, device_id) -> monotonic timestamp

PRESENCE_WINDOW_SECONDS = 60
MAX_WAIT_SECONDS = 90

# Anyone who can reach this server can pick any (room, device_id) pair —
# there's no signup step, by design (a room code is a shared meeting-place,
# not an account). That means an attacker could try to grow these dicts
# without bound; cap how many distinct (room, device_id) slots are tracked
# at once, and periodically sweep out ones nobody has polled recently.
MAX_TRACKED_DEVICES = 2000
STALE_ENTRY_SECONDS = PRESENCE_WINDOW_SECONDS * 10
_ID_RE = re.compile(r"^[A-Za-z0-9._\-]{1,128}$")


def _valid_id(value: str) -> bool:
    return bool(value) and bool(_ID_RE.match(value))


def _cleanup_loop():
    while True:
        time.sleep(60)
        now = time.monotonic()
        with _lock:
            stale_keys = [k for k, seen in _last_seen.items() if now - seen > STALE_ENTRY_SECONDS]
            for key in stale_keys:
                _last_seen.pop(key, None)
                _inboxes.pop(key, None)


def _inbox(room, device_id):
    key = (room, device_id)
    with _lock:
        if key not in _inboxes:
            if len(_inboxes) >= MAX_TRACKED_DEVICES:
                return None
            _inboxes[key] = queue.Queue()
        return _inboxes[key]


def _touch_presence(room, device_id):
    with _lock:
        _last_seen[(room, device_id)] = time.monotonic()


@app.post("/relay/<room>/<device_id>/request")
@rate_limited(_request_limiter)
def relay_request(room, device_id):
    if not _valid_id(room) or not _valid_id(device_id):
        return jsonify({"error": "room and device_id must be 1-128 chars of letters/digits/._- "}), 400

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "request body must be JSON"}), 400

    inbox = _inbox(room, device_id)
    if inbox is None:
        return jsonify({"error": "relay is at capacity, try again later"}), 503

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

    inbox.put(envelope)

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
@rate_limited(_poll_limiter)
def relay_poll(room, device_id):
    if not _valid_id(room) or not _valid_id(device_id):
        return jsonify({"error": "room and device_id must be 1-128 chars of letters/digits/._- "}), 400
    _touch_presence(room, device_id)
    inbox = _inbox(room, device_id)
    if inbox is None:
        return jsonify({"error": "relay is at capacity, try again later"}), 503
    timeout = request.args.get("timeout", 25, type=float)
    timeout = max(1.0, min(timeout, MAX_WAIT_SECONDS))
    try:
        envelope = inbox.get(timeout=timeout)
    except queue.Empty:
        return jsonify({"request": None})
    return jsonify({"request": envelope})


@app.post("/relay/<room>/<device_id>/respond/<request_id>")
@rate_limited(_request_limiter)
def relay_respond(room, device_id, request_id):
    if not _valid_id(room) or not _valid_id(device_id) or not re.match(r"^[0-9a-f]{32}$", request_id or ""):
        return jsonify({"error": "invalid room, device_id, or request_id"}), 400
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
@rate_limited(_poll_limiter)
def relay_devices(room):
    if not _valid_id(room):
        return jsonify({"error": "room must be 1-128 chars of letters/digits/._- "}), 400
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
    threading.Thread(target=_cleanup_loop, daemon=True).start()
    print(f"Overclock relay listening on {args.host}:{args.port}")
    print(
        "This process is meant to be reachable from the open internet — make sure it's the "
        "only thing exposed (e.g. no other services sharing this host's public IP/port range)."
    )
    app.run(host=args.host, port=args.port, threaded=True)
