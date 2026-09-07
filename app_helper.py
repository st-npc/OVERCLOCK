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

`--passphrase`, if set, is required (as an X-Overclock-Passphrase header)
on /process only — /stats stays open since it's read-only telemetry the
Receiver page's own UI depends on. `--relay` puts this helper in relay
mode alongside its normal direct listening: a background thread polls a
relay_server.py for requests tunneled from a device on a different
network and replays them against this same Flask app, so /stats and
/process behave identically either way.
"""
import argparse
import base64
import hmac
import json
import threading
import time
from collections import deque

import psutil
import requests
from flask import Flask, Response, jsonify, render_template, request

import wire
from tasks import DEFAULT_TASK, get_task, task_choices

app = Flask(__name__)

_busy_lock = threading.Lock()
_busy = False

_accepting_lock = threading.Lock()
_accepting = True

_activity_lock = threading.Lock()
_activity = deque(maxlen=50)

_passphrase = None  # set from --passphrase; None means /process is open to anyone


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
    return hmac.compare_digest(provided, _passphrase)


@app.get("/")
def receiver_page():
    return render_template("receiver.html")


@app.get("/stats")
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
def api_activity():
    with _activity_lock:
        items = list(_activity)
    return jsonify({"accepting": _is_accepting(), "busy": _is_busy(), "activity": items, "auth_required": bool(_passphrase)})


@app.get("/api/tasks")
def api_tasks():
    return jsonify({"tasks": task_choices(), "default": DEFAULT_TASK})


@app.post("/api/toggle")
def api_toggle():
    global _accepting
    with _accepting_lock:
        _accepting = not _accepting
        new_value = _accepting
    return jsonify({"accepting": new_value})


@app.post("/process")
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
    task_mod = get_task(task_type)

    _set_busy(True)
    try:
        result = task_mod.process_batch(items)
    except Exception as exc:
        return jsonify({"error": f"processing failed: {exc}"}), 500
    finally:
        _set_busy(False)

    _record_activity(payload.get("job_id"), payload.get("chunk_id"), task_type, len(items), result["elapsed_seconds"])

    response_payload = {
        "job_id": payload.get("job_id"),
        "chunk_id": payload.get("chunk_id"),
        "items": result["items"],
        "elapsed_seconds": result["elapsed_seconds"],
    }
    compressed_out = wire.compress_payload(response_payload)
    return Response(compressed_out, mimetype="application/octet-stream", headers={wire.COMPRESSION_HEADER: wire.COMPRESSION_MARKER})


@app.errorhandler(404)
def not_found(_err):
    return jsonify({"error": "not found"}), 404


@app.errorhandler(500)
def server_error(_err):
    return jsonify({"error": "internal server error"}), 500


# ---------------------------------------------------------------------
# Relay mode: poll a relay_server.py for tunneled requests (from a device
# on a different network) and replay them against this same app. Reuses
# every route above unchanged — auth, compression, and task dispatch all
# behave identically whether a request arrived directly or via relay.
def _relay_loop(relay_base: str, room: str, device_id: str):
    client = app.test_client()
    poll_url = f"{relay_base}/relay/{room}/{device_id}/poll"
    print(f"Relay mode: polling {poll_url} as '{device_id}' in room '{room}'")

    while True:
        try:
            resp = requests.get(poll_url, params={"timeout": 25}, timeout=(5.0, 30.0))
        except requests.exceptions.RequestException:
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
                requests.post(f"{relay_base}/relay/{room}/{device_id}/respond/{request_id}", json=out, timeout=(3.0, 10.0))
            except requests.exceptions.RequestException:
                pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Overclock helper service")
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--passphrase", default=None, help="require this passphrase on /process")
    parser.add_argument("--relay", default=None, help="relay_server.py base URL, e.g. http://relay.example.com:5090")
    parser.add_argument("--room", default="default", help="relay room code shared with the main device")
    parser.add_argument("--device-id", default=None, help="this device's id within the relay room (default: derived from port)")
    args = parser.parse_args()

    if args.passphrase:
        _passphrase = args.passphrase
        print("Passphrase required on /process")

    if args.relay:
        device_id = args.device_id or f"helper-{args.port}"
        thread = threading.Thread(target=_relay_loop, args=(args.relay.rstrip("/"), args.room, device_id), daemon=True)
        thread.start()
        print(f"Reachable via relay as: relay://{args.relay.split('://', 1)[-1]}/{args.room}/{device_id}")

    print(f"Helper listening on {args.host}:{args.port}  (open http://localhost:{args.port} for the Receiver page)")
    app.run(host=args.host, port=args.port, threaded=True)
