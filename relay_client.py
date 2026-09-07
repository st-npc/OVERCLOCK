"""Client-side helper for talking to a relay_server.py-tunneled device.

Address format: "relay://<relay-host:port>/<room>/<device_id>" — used
transparently by net_client.py wherever a plain "host:port" address would
otherwise go. See relay_server.py for the server side and app_helper.py's
`_relay_loop` for how a helper answers requests delivered this way.
"""
import base64
from urllib.parse import urlparse

import requests

DEFAULT_POLL_TIMEOUT = 25  # how long the relay waits for the helper to be free, per request


def parse_relay_address(address: str):
    parsed = urlparse(address.strip())
    room_device = parsed.path.strip("/")
    if "/" not in room_device:
        raise ValueError(f"malformed relay address (expected relay://host:port/room/device_id): {address}")
    room, device_id = room_device.split("/", 1)
    relay_base = f"http://{parsed.netloc}"
    return relay_base, room, device_id


def relay_request(address: str, method: str, path: str, body: bytes, headers: dict, timeout: float = None):
    """Tunnel one request through the relay. Returns (status, headers, body)
    — status is None if the relay couldn't deliver it at all (relay
    unreachable, or no response within the wait window), which callers
    treat identically to a direct connection failure."""
    try:
        relay_base, room, device_id = parse_relay_address(address)
    except ValueError:
        return None, {}, b""

    wait = timeout or DEFAULT_POLL_TIMEOUT
    envelope = {
        "method": method,
        "path": path,
        "headers": headers or {},
        "body_b64": base64.b64encode(body).decode("ascii") if body else None,
        "wait": wait,
    }

    try:
        resp = requests.post(
            f"{relay_base}/relay/{room}/{device_id}/request",
            json=envelope,
            timeout=(3.0, wait + 8.0),
        )
    except requests.exceptions.RequestException:
        return None, {}, b""

    if resp.status_code != 200:
        return None, {}, b""
    try:
        data = resp.json()
    except ValueError:
        return None, {}, b""

    if not data.get("delivered"):
        return None, {}, b""

    body_b64 = data.get("body_b64")
    body_bytes = base64.b64decode(body_b64) if body_b64 else b""
    return data.get("status"), data.get("headers", {}), body_bytes
