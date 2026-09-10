"""Scripted checkpoints for the security fixes found during review:
  1. render_task rejects oversized/out-of-range tile specs instead of
     letting Image.new() OOM the process.
  2. image_task caps the total decoded-pixel count across a whole chunk,
     not just per image.
  3. app_helper's /api/toggle requires the configured passphrase, same as
     /process.
  4. relay_server binds a (room, device_id) to whoever polls it first, so a
     second connection that merely knows the id can't race it for traffic.

Run directly: `python tests/test_security.py`
No pytest dependency, same reasoning as test_fallback.py: plain functions +
assertions so this stays runnable with nothing beyond requirements.txt.
"""
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image

PASS = []
FAIL = []


def check(name, condition, detail=""):
    if condition:
        PASS.append(name)
        print(f"  PASS  {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}  {detail}")


# ---------------------------------------------------------------------
def test_render_task_rejects_oversized_spec():
    print("\n[1] render_task rejects out-of-bounds tile specs")
    from tasks import render_task

    legit = {"index": 0, "x_start": 0, "x_end": 10, "width": 640, "height": 480, "max_iter": 50}
    out = render_task.process_batch([json.dumps(legit)])
    check("legitimate small spec still processes", out["count"] == 1)

    huge = {"index": 0, "x_start": 0, "x_end": 50000, "width": 50000, "height": 50000, "max_iter": 1}
    try:
        render_task.process_batch([json.dumps(huge)])
        check("oversized tile spec is rejected", False, "no exception raised")
    except render_task.InvalidRenderSpec:
        check("oversized tile spec is rejected", True)

    bad_iter = {"index": 0, "x_start": 0, "x_end": 10, "width": 640, "height": 480, "max_iter": 10_000_000}
    try:
        render_task.process_batch([json.dumps(bad_iter)])
        check("excessive max_iter is rejected", False, "no exception raised")
    except render_task.InvalidRenderSpec:
        check("excessive max_iter is rejected", True)


def test_image_task_aggregate_pixel_cap():
    print("\n[2] image_task enforces an aggregate decoded-pixel cap per batch")
    from tasks import image_task

    original_cap = image_task.MAX_TOTAL_DECODED_PIXELS
    try:
        def make(size):
            return image_task.encode_image(Image.new("RGB", size))

        out = image_task.process_batch([make((64, 64))])
        check("a single normal-sized image still processes", out["count"] == 1)

        image_task.MAX_TOTAL_DECODED_PIXELS = 100 * 100  # artificially tiny, for the test
        try:
            image_task.process_batch([make((200, 200))])
            check("a batch exceeding the aggregate cap is rejected", False, "no exception raised")
        except ValueError:
            check("a batch exceeding the aggregate cap is rejected", True)
    finally:
        image_task.MAX_TOTAL_DECODED_PIXELS = original_cap


def test_helper_toggle_requires_passphrase():
    print("\n[3] app_helper /api/toggle requires the configured passphrase")
    os.environ["OVERCLOCK_PASSPHRASE"] = "test-passphrase-123"
    import importlib

    import app_helper
    importlib.reload(app_helper)  # pick up the env var set just above

    client = app_helper.app.test_client()
    try:
        resp = client.post("/api/toggle")
        check("no passphrase -> 401", resp.status_code == 401, f"got {resp.status_code}")

        resp = client.post("/api/toggle", headers={"X-Overclock-Passphrase": "wrong"})
        check("wrong passphrase -> 401", resp.status_code == 401, f"got {resp.status_code}")

        resp = client.post("/api/toggle", headers={"X-Overclock-Passphrase": "test-passphrase-123"})
        check("correct passphrase -> 200", resp.status_code == 200, f"got {resp.status_code}")
    finally:
        os.environ.pop("OVERCLOCK_PASSPHRASE", None)


def test_relay_binds_device_to_first_poller():
    print("\n[4] relay_server binds a device_id to whoever polls it first")
    import relay_server

    client = relay_server.app.test_client()
    room = f"room-{uuid.uuid4().hex[:8]}"
    device = "dev1"

    resp = client.get(f"/relay/{room}/{device}/poll?timeout=1", headers={"X-Overclock-Relay-Secret": "secret-a"})
    check("first poller with a secret is accepted", resp.status_code == 200, f"got {resp.status_code}")

    resp = client.get(f"/relay/{room}/{device}/poll?timeout=1", headers={"X-Overclock-Relay-Secret": "secret-b"})
    check(
        "a second connection with a different secret is rejected while the first is active",
        resp.status_code == 409,
        f"got {resp.status_code}",
    )

    resp = client.get(f"/relay/{room}/{device}/poll?timeout=1", headers={"X-Overclock-Relay-Secret": "secret-a"})
    check("the original connection can keep polling with its own secret", resp.status_code == 200, f"got {resp.status_code}")

    resp = client.get(f"/relay/{room}/{device}/poll?timeout=1")
    check("polling with no secret at all is rejected", resp.status_code == 409, f"got {resp.status_code}")


if __name__ == "__main__":
    test_render_task_rejects_oversized_spec()
    test_image_task_aggregate_pixel_cap()
    test_helper_toggle_requires_passphrase()
    test_relay_binds_device_to_first_poller()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)
