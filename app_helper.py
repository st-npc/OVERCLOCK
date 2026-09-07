"""Helper service: runs on a device that has spare capacity to lend.

Exposes:
  GET  /stats    -> current CPU/RAM load + busy flag
  POST /process  -> process a chunk of images with the shared task logic

Never trusts the network: malformed requests get a clean 400/500 JSON
response instead of an unhandled exception killing the process.
"""
import argparse
import threading

import psutil
from flask import Flask, jsonify, request

import task

app = Flask(__name__)

_busy_lock = threading.Lock()
_busy = False


def _set_busy(value: bool):
    global _busy
    with _busy_lock:
        _busy = value


def _is_busy() -> bool:
    with _busy_lock:
        return _busy


@app.get("/stats")
def stats():
    vm = psutil.virtual_memory()
    return jsonify(
        {
            "cpu_percent": psutil.cpu_percent(interval=None),
            "ram_percent": vm.percent,
            "ram_free_gb": round(vm.available / (1024 ** 3), 3),
            "ram_total_gb": round(vm.total / (1024 ** 3), 3),
            "status": "busy" if _is_busy() else "idle",
        }
    )


@app.post("/process")
def process():
    if _is_busy():
        return jsonify({"error": "helper is already processing another job"}), 409

    try:
        payload = request.get_json(force=False, silent=True)
    except Exception:
        payload = None

    if not isinstance(payload, dict):
        return jsonify({"error": "request body must be JSON"}), 400

    images = payload.get("images")
    if not isinstance(images, list) or not images or not all(isinstance(x, str) for x in images):
        return jsonify({"error": "'images' must be a non-empty list of base64 strings"}), 400

    if len(images) > 500:
        return jsonify({"error": "chunk too large"}), 413

    _set_busy(True)
    try:
        result = task.process_batch(images)
    except Exception as exc:
        return jsonify({"error": f"processing failed: {exc}"}), 500
    finally:
        _set_busy(False)

    return jsonify(
        {
            "job_id": payload.get("job_id"),
            "chunk_id": payload.get("chunk_id"),
            "images": result["images"],
            "elapsed_seconds": result["elapsed_seconds"],
        }
    )


@app.errorhandler(404)
def not_found(_err):
    return jsonify({"error": "not found"}), 404


@app.errorhandler(500)
def server_error(_err):
    return jsonify({"error": "internal server error"}), 500


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Overclock helper service")
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    print(f"Helper listening on {args.host}:{args.port}  (GET /stats, POST /process)")
    app.run(host=args.host, port=args.port, threaded=True)
