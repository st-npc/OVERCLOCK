"""Helper service: runs on a device that has spare capacity to lend.

Exposes:
  GET  /              -> the Receiver dashboard (self stats + pause control)
  GET  /stats          -> current CPU/RAM load + busy/paused flag
  POST /process         -> process a chunk of work items with the shared task logic
  POST /api/toggle       -> pause/resume accepting work
  GET  /api/activity      -> recent incoming-job activity, for the Receiver page

Never trusts the network: malformed requests get a clean 400/500 JSON
response instead of an unhandled exception killing the process.

`_accepting` defaults to True so headless/scripted use (curl, tests) works
exactly as before with no UI involved — the Receiver page's button is a
real pause/resume control on top of that default, not a gate you must
unlock first.

`--passphrase` (or the OVERCLOCK_PASSPHRASE env var — preferred, since a CLI
arg is visible to other local users via `ps`), if set, is required (as an
X-Overclock-Passphrase header) on /process only — /stats stays open since
it's read-only telemetry the Receiver page's own UI depends on. `--relay`
puts this helper in relay mode alongside its normal direct listening: a
background thread polls a relay_server.py for requests tunneled from a
device on a different network and replays them against this same Flask app,
so /stats and /process behave identically either way.
"""
import argparse
import base64
import datetime
import json
import os
import re
import secrets
import threading
import time
from collections import deque

import psutil
import requests
from flask import Flask, Response, jsonify, render_template, request

import wire
from flask_common import install_error_handlers, install_security_headers
from security import RateLimiter, constant_time_eq, rate_limited
from tasks import DEFAULT_TASK, get_task, task_choices

app = Flask(__name__)
# /process legitimately carries a batch of images/render tiles (up to 500
# items per chunk); other routes here only ever exchange small JSON. 64MB
# comfortably covers a real chunk while still bounding memory from an
# oversized/malicious body before a route body is even read.
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = datetime.timedelta(days=365)

RELAY_SECRET_HEADER = "X-Overclock-Relay-Secret"  # see relay_server.py's comment on the same name

HELPER_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)


install_security_headers(app, csp=HELPER_CSP)


_process_limiter = RateLimiter(max_requests=30, window_seconds=10)
_toggle_limiter = RateLimiter(max_requests=10, window_seconds=10)
_read_limiter = RateLimiter(max_requests=120, window_seconds=10)

# job_id/chunk_id/task_type arrive in an authenticated-or-not /process body
# and get echoed straight back into the response and this helper's own
# Receiver page (via /api/activity). They're free-form strings from
# whoever's driving a job against this helper, so cap their length and strip
# anything that isn't printable before they're stored or rendered anywhere.
_UNPRINTABLE_RE = re.compile(r"[\x00-\x1f\x7f]")
_ACTIVITY_FIELD_MAX_LEN = 64


def _clean_activity_field(value, max_len: int = _ACTIVITY_FIELD_MAX_LEN):
    if not isinstance(value, str):
        return None
    cleaned = _UNPRINTABLE_RE.sub("", value).strip()
    return cleaned[:max_len] if cleaned else None


_busy_lock = threading.Lock()
_busy = False

_accepting_lock = threading.Lock()
_accepting = True

_activity_lock = threading.Lock()
_activity = deque(maxlen=50)

_passphrase = os.environ.get("OVERCLOCK_PASSPHRASE") or None  # None means /process is open to anyone


def _set_busy(value: bool):
    global _busy
    with _busy_lock:
        _busy = value


def _is_busy() -> bool:
    with _busy_lock:
        return _busy


def _is_accepting() -> bool:
    with _accepting_lock:
        return _accepting


def _record_activity(job_id, chunk_id, task_type, count, elapsed_seconds):
    with _activity_lock:
        _activity.appendleft(
            {
                "ts": time.time(),
                "job_id": job_id,
                "chunk_id": chunk_id,
                "task_type": task_type,
                "count": count,
                "elapsed_seconds": round(elapsed_seconds, 2),
            }
        )


def _current_status() -> str:
    if _is_busy():
        return "busy"
    if not _is_accepting():
        return "paused"
    return "idle"


def _auth_ok() -> bool:
    if not _passphrase:
        return True
    provided = request.headers.get("X-Overclock-Passphrase", "")
    return constant_time_eq(provided, _passphrase)


@app.get("/")
def receiver_page():
    return render_template("receiver.html")


@app.get("/stats")
@rate_limited(_read_limiter)
def stats():
    vm = psutil.virtual_memory()
    return jsonify(
        {
            "cpu_percent": psutil.cpu_percent(interval=None),
            "ram_percent": vm.percent,
            "ram_free_gb": round(vm.available / (1024 ** 3), 3),
            "ram_total_gb": round(vm.total / (1024 ** 3), 3),
            "status": _current_status(),
            "accepting": _is_accepting(),
            "auth_required": bool(_passphrase),
        }
    )


@app.get("/api/activity")
@rate_limited(_read_limiter)
def api_activity():
    with _activity_lock:
        items = list(_activity)
    return jsonify({"accepting": _is_accepting(), "busy": _is_busy(), "activity": items, "auth_required": bool(_passphrase)})


@app.get("/api/tasks")
@rate_limited(_read_limiter)
def api_tasks():
    return jsonify({"tasks": task_choices(), "default": DEFAULT_TASK})


@app.post("/api/toggle")
@rate_limited(_toggle_limiter)
def api_toggle():
    if not _auth_ok():
        return jsonify({"error": "invalid or missing passphrase"}), 401
    global _accepting
    with _accepting_lock:
        _accepting = not _accepting
        new_value = _accepting
    return jsonify({"accepting": new_value})


@app.post("/process")
@rate_limited(_process_limiter)
def process():
    if not _auth_ok():
        return jsonify({"error": "invalid or missing passphrase"}), 401

    if not _is_accepting():
        return jsonify({"error": "not accepting work right now (paused by operator)"}), 503

    if _is_busy():
        return jsonify({"error": "helper is already processing another job"}), 409

    compressed_in = request.headers.get(wire.COMPRESSION_HEADER) == wire.COMPRESSION_MARKER
    try:
        if compressed_in:
            payload = wire.decompress_payload(request.get_data())
        else:
            payload = request.get_json(force=False, silent=True)
    except Exception:
        payload = None

    if not isinstance(payload, dict):
        return jsonify({"error": "request body must be JSON"}), 400

    items = payload.get("items")
    if not isinstance(items, list) or not items or not all(isinstance(x, str) for x in items):
        return jsonify({"error": "'items' must be a non-empty list of strings"}), 400

    if len(items) > 500:
        return jsonify({"error": "chunk too large"}), 413

    task_type = payload.get("task_type", DEFAULT_TASK)
    if not isinstance(task_type, str):
        return jsonify({"error": "'task_type' must be a string"}), 400
    task_mod = get_task(task_type)

    # job_id/chunk_id are free-form strings supplied by whoever is driving
    # this job (the orchestrator, ordinarily — but nothing stops a direct
    # caller from sending anything). They're only ever used for display
    # (this response, and the Receiver page's activity list), never for a
    # file path or a lookup key, but they're sanitized and length-capped
    # anyway so nothing oversized or full of control characters ends up
    # stored or echoed back.
    job_id = _clean_activity_field(payload.get("job_id"))
    chunk_id = _clean_activity_field(payload.get("chunk_id"))

    _set_busy(True)
    try:
        result = task_mod.process_batch(items)
    except Exception as exc:
        # The exception message can be useful during LAN-local debugging
        # (that's this project's whole demo philosophy), but cap it so a
        # pathological error can't blow up the response.
        return jsonify({"error": f"processing failed: {str(exc)[:300]}"}), 500
    finally:
        _set_busy(False)

    _record_activity(job_id, chunk_id, _clean_activity_field(task_type) or task_type[:64], len(items), result["elapsed_seconds"])

    response_payload = {
        "job_id": job_id,
        "chunk_id": chunk_id,
        "items": result["items"],
        "elapsed_seconds": result["elapsed_seconds"],
    }
    compressed_out = wire.compress_payload(response_payload)
    return Response(compressed_out, mimetype="application/octet-stream", headers={wire.COMPRESSION_HEADER: wire.COMPRESSION_MARKER})


install_error_handlers(app)


# ---------------------------------------------------------------------
# Relay mode: poll a relay_server.py for tunneled requests (from a device
# on a different network) and replay them against this same app. Reuses
# every route above unchanged — auth, compression, and task dispatch all
# behave identically whether a request arrived directly or via relay.
def _relay_loop(relay_base: str, room: str, device_id: str):
    client = app.test_client()
    poll_url = f"{relay_base}/relay/{room}/{device_id}/poll"
    print(f"Relay mode: polling {poll_url} as '{device_id}' in room '{room}'")

    # Generated once per process and sent on every poll/respond so
    # relay_server.py can tell this connection apart from anyone else who
    # merely knows the (room, device_id) pair — see relay_server.py's
    # RELAY_SECRET_HEADER comment for what this closes.
    relay_secret = secrets.token_hex(16)
    relay_headers = {RELAY_SECRET_HEADER: relay_secret}

    while True:
        try:
            resp = requests.get(poll_url, params={"timeout": 25}, headers=relay_headers, timeout=(5.0, 30.0))
        except requests.exceptions.RequestException:
            time.sleep(2.0)
            continue

        if resp.status_code == 409:
            print(f"Relay mode: device_id '{device_id}' in room '{room}' is already claimed by another "
                  "connection — pick a different --device-id or --room. Retrying...")
            time.sleep(2.0)
            continue
        if resp.status_code != 200:
            time.sleep(2.0)
            continue

        try:
            data = resp.json()
        except ValueError:
            continue

        envelope = data.get("request")
        if not envelope:
            continue

        request_id = envelope.get("id")
        method = envelope.get("method", "GET")
        path = envelope.get("path", "/")
        hdrs = envelope.get("headers") or {}
        body_b64 = envelope.get("body_b64")
        body = base64.b64decode(body_b64) if body_b64 else None

        try:
            if method == "GET":
                local_resp = client.get(path, headers=hdrs)
            else:
                local_resp = client.post(path, data=body, headers=hdrs)
            out = {
                "status": local_resp.status_code,
                "headers": {k: v for k, v in local_resp.headers.items() if k == wire.COMPRESSION_HEADER},
                "body_b64": base64.b64encode(local_resp.get_data()).decode("ascii"),
            }
        except Exception as exc:  # a bug in a route must not kill the relay loop
            out = {
                "status": 500,
                "headers": {},
                "body_b64": base64.b64encode(json.dumps({"error": str(exc)}).encode()).decode("ascii"),
            }

        if request_id:
            try:
                requests.post(
                    f"{relay_base}/relay/{room}/{device_id}/respond/{request_id}",
                    json=out,
                    headers=relay_headers,
                    timeout=(3.0, 10.0),
                )
            except requests.exceptions.RequestException:
                pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Overclock helper service")
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument(
        "--passphrase",
        default=None,
        help="require this passphrase on /process. Prefer the OVERCLOCK_PASSPHRASE env var instead — "
        "a CLI arg is visible to other local users via `ps`.",
    )
    parser.add_argument("--relay", default=None, help="relay_server.py base URL, e.g. http://relay.example.com:5090")
    parser.add_argument("--room", default="default", help="relay room code shared with the main device")
    parser.add_argument("--device-id", default=None, help="this device's id within the relay room (default: derived from port)")
    args = parser.parse_args()

    if args.passphrase:
        _passphrase = args.passphrase
    if _passphrase:
        print("Passphrase required on /process")
    else:
        print("No passphrase set — any device that can reach /process can send this helper work.")

    if args.relay:
        device_id = args.device_id or f"helper-{args.port}"
        thread = threading.Thread(target=_relay_loop, args=(args.relay.rstrip("/"), args.room, device_id), daemon=True)
        thread.start()
        print(f"Reachable via relay as: relay://{args.relay.split('://', 1)[-1]}/{args.room}/{device_id}")

    print(f"Helper listening on {args.host}:{args.port}  (open http://localhost:{args.port} for the Receiver page)")
    app.run(host=args.host, port=args.port, threaded=True)
