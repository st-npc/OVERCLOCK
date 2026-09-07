"""Scripted checkpoints for the Priority-1 fallback guarantee.

Run directly: `python tests/test_fallback.py`
No pytest dependency — plain functions + assertions so this stays runnable
with nothing beyond requirements.txt already installed.

Covers the three failure modes the fallback guarantee must survive:
  1. Zero helpers configured at all.
  2. A configured helper that was never reachable in the first place.
  3. A helper that answers the initial reachability probe fine, then is
     killed before it can finish processing its assigned chunk.
"""
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from queue import Queue

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import net_client
import orchestrator as orchestrator_module
from device_monitor import DeviceMonitor
from orchestrator import Orchestrator

PASS = []
FAIL = []


def check(name, condition, detail=""):
    if condition:
        PASS.append(name)
        print(f"  PASS  {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}  {detail}")


def make_orchestrator():
    monitor = DeviceMonitor()
    events = Queue()
    orch = Orchestrator(monitor, events)
    return orch, events


def run_job_sync(orch, helpers, num_images):
    job_id = uuid.uuid4().hex[:8]
    orch._run_job(job_id, helpers, num_images)
    return job_id


def cleanup_job_dir(job_id):
    path = os.path.join(orchestrator_module.OUTPUT_DIR, job_id)
    shutil.rmtree(path, ignore_errors=True)


def find_free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------------------------------------------------------------
def test_zero_helpers():
    print("\n[1] Zero helpers configured")
    orch, _ = make_orchestrator()
    num_images = 10
    job_id = run_job_sync(orch, [], num_images)
    out_dir = os.path.join(orchestrator_module.OUTPUT_DIR, job_id)
    saved = len(os.listdir(out_dir)) if os.path.isdir(out_dir) else 0
    check("all images processed locally with no helpers", saved == num_images, f"saved={saved}")
    cleanup_job_dir(job_id)


def test_helper_never_reachable():
    print("\n[2] Helper configured but never reachable")
    orch, _ = make_orchestrator()
    num_images = 10
    dead_addr = "127.0.0.1:59999"  # nothing listens here
    job_id = run_job_sync(orch, [dead_addr], num_images)
    out_dir = os.path.join(orchestrator_module.OUTPUT_DIR, job_id)
    saved = len(os.listdir(out_dir)) if os.path.isdir(out_dir) else 0
    check("job completes fully despite unreachable helper", saved == num_images, f"saved={saved}")

    rec = orch.monitor.get(dead_addr)
    check("unreachable helper marked as such in monitor", rec is not None and rec.status == "unreachable")
    cleanup_job_dir(job_id)


def test_helper_dies_mid_job():
    print("\n[3] Helper reachable at probe time, killed before it can finish")
    port = find_free_port()
    helper_script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app_helper.py")
    proc = subprocess.Popen(
        [sys.executable, helper_script, "--port", str(port), "--host", "127.0.0.1"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    addr = f"127.0.0.1:{port}"
    try:
        # Wait for the helper to actually come up.
        up = False
        for _ in range(50):
            try:
                net_client.fetch_stats(addr)
                up = True
                break
            except net_client.HelperError:
                time.sleep(0.1)
        check("helper came up and answered /stats", up)
        if not up:
            return

        orch, _ = make_orchestrator()
        num_images = 12

        # Monkey-patch the probe to succeed (helper is alive), then kill the
        # helper immediately after the probe so the *dispatch* call fails —
        # reproducing "reachable at start, dead by the time work arrives."
        original_probe = orch._probe_helpers

        def probe_then_kill(helper_addresses):
            reachable = original_probe(helper_addresses)
            proc.terminate()
            proc.wait(timeout=5)
            return reachable

        orch._probe_helpers = probe_then_kill

        job_id = run_job_sync(orch, [addr], num_images)
        out_dir = os.path.join(orchestrator_module.OUTPUT_DIR, job_id)
        saved = len(os.listdir(out_dir)) if os.path.isdir(out_dir) else 0
        check(
            "job completes fully after helper dies mid-dispatch (reassigned to local)",
            saved == num_images,
            f"saved={saved}",
        )
        cleanup_job_dir(job_id)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_malformed_helper_response():
    print("\n[4] Helper returns malformed JSON on /process")
    from flask import Flask, jsonify

    fake = Flask("fake-helper")

    @fake.get("/stats")
    def stats():
        return jsonify(
            {"cpu_percent": 5.0, "ram_percent": 10.0, "ram_free_gb": 8.0, "ram_total_gb": 16.0, "status": "idle"}
        )

    @fake.post("/process")
    def process():
        # Wrong shape on purpose: missing 'images' key entirely.
        return jsonify({"ok": True})

    port = find_free_port()
    import threading
    from werkzeug.serving import make_server

    server = make_server("127.0.0.1", port, fake)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    addr = f"127.0.0.1:{port}"

    try:
        orch, _ = make_orchestrator()
        num_images = 8
        job_id = run_job_sync(orch, [addr], num_images)
        out_dir = os.path.join(orchestrator_module.OUTPUT_DIR, job_id)
        saved = len(os.listdir(out_dir)) if os.path.isdir(out_dir) else 0
        check(
            "job completes fully despite malformed helper response",
            saved == num_images,
            f"saved={saved}",
        )
        cleanup_job_dir(job_id)
    finally:
        server.shutdown()
        thread.join(timeout=5)


if __name__ == "__main__":
    test_zero_helpers()
    test_helper_never_reachable()
    test_helper_dies_mid_job()
    test_malformed_helper_response()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)
