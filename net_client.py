"""Hardened HTTP client for talking to helper devices.

Every network boundary in this app goes through here so timeouts,
malformed-response handling, and error typing are consistent in one place
instead of scattered across orchestrator/monitor code.

Two transports, same validation and same call signatures either way:
  - direct: plain HTTP to the helper's own address.
  - relay: address of the form "relay://<relay-host:port>/<room>/<device_id>"
    — requests are tunneled through a relay_server.py both sides can
    reach, for devices on different networks. See relay_client.py.
"""
import json
from urllib.parse import urlparse

import requests

import relay_client
import wire

CONNECT_TIMEOUT = 2.0      # helper should ack fast if it's alive at all
STATS_READ_TIMEOUT = 2.5   # /stats is cheap, should return almost instantly
PROCESS_BASE_TIMEOUT = 5.0     # baseline read timeout for /process
PROCESS_TIMEOUT_PER_ITEM = 2.5  # extra allowance per work item in the chunk (heavier tasks need more)
RELAY_STATS_WAIT = 4.0    # a genuinely alive relay-connected helper is mid-poll almost constantly;
                           # anything much longer just delays detecting a dead one during the probe

PASSPHRASE_HEADER = "X-Overclock-Passphrase"

MAX_ADDRESS_LEN = 255

# This app deliberately lets a user hand it an arbitrary "helper" address and
# then makes an outbound HTTP request to it from the server process — that's
# the whole point (offload work to another device you point it at). The
# trade-off is a built-in SSRF primitive: anyone who can reach the dashboard
# can make it issue requests to addresses of their choosing. We can't (and
# shouldn't) block private/LAN ranges, since that's exactly where real
# helpers live, including 169.254.x.x for the USB-C link-local case
# documented in README.md. What we *can* block cheaply: the well-known cloud
# metadata endpoint (a classic SSRF target with zero legitimate use as an
# "Overclock helper"), and addresses smuggling credentials or control
# characters into the host portion.
_BLOCKED_HOSTS = {"169.254.169.254", "metadata.google.internal"}


class InvalidHelperAddress(ValueError):
    """Raised by validate_helper_address(); callers turn this into a 400
    rather than ever attempting a network call with the address."""


def validate_helper_address(address: str) -> str:
    """Validate (without connecting) a helper address of either form:
    `host:port` or `relay://relay-host:port/room/device_id`. Returns the
    stripped address on success; raises InvalidHelperAddress with a
    human-readable reason otherwise."""
    addr = (address or "").strip()
    if not addr:
        raise InvalidHelperAddress("address is empty")
    if len(addr) > MAX_ADDRESS_LEN:
        raise InvalidHelperAddress("address is too long")
    if any(ord(ch) < 0x20 for ch in addr):
        raise InvalidHelperAddress("address contains control characters")

    if is_relay_address(addr):
        try:
            relay_base, room, device_id = relay_client.parse_relay_address(addr)
        except ValueError as exc:
            raise InvalidHelperAddress(str(exc)) from exc
        if not room or not device_id or len(room) > 128 or len(device_id) > 128:
            raise InvalidHelperAddress("relay room/device id is empty or too long")
        host = urlparse(relay_base).hostname
        if not host:
            raise InvalidHelperAddress("relay address is missing a host")
        if host.lower() in _BLOCKED_HOSTS:
            raise InvalidHelperAddress("that relay host is not allowed")
        return addr

    parsed = urlparse(_base_url(addr))
    if parsed.username or parsed.password:
        raise InvalidHelperAddress("credentials in the address are not allowed")
    host = parsed.hostname
    if not host:
        raise InvalidHelperAddress("could not parse a host from the address")
    if host.lower() in _BLOCKED_HOSTS:
        raise InvalidHelperAddress("that address is not allowed")
    if parsed.port is None:
        # Without an explicit port this silently defaults to 80, which is
        # never where app_helper.py listens — it then just times out with a
        # confusing "unreachable" error instead of saying what's wrong.
        raise InvalidHelperAddress(
            "missing a port — enter it as host:port, e.g. 192.168.1.42:5001 "
            "(the port app_helper.py is running on for that device)"
        )
    return addr


class HelperError(Exception):
    """Base class for all helper-communication failures. Any caller that
    catches this treats the helper as unavailable for this attempt."""


class HelperUnreachable(HelperError):
    """Connection refused/timed out/DNS failed — helper is not responding."""


class HelperBadResponse(HelperError):
    """Helper responded, but with something we can't safely use (bad
    status code, non-JSON body, or JSON missing/mismatching expected shape)."""


class HelperAuthError(HelperBadResponse):
    """Helper responded 401 — passphrase missing or wrong. Still a
    HelperBadResponse subclass so existing fallback handling catches it."""


def is_relay_address(address: str) -> bool:
    return address.strip().startswith("relay://")


def _base_url(address: str) -> str:
    address = address.strip()
    if not address.startswith("http://") and not address.startswith("https://"):
        address = "http://" + address
    return address.rstrip("/")


def _auth_headers(passphrase: str) -> dict:
    return {PASSPHRASE_HEADER: passphrase} if passphrase else {}


def _validate_stats_dict(address: str, data) -> dict:
    required = {"cpu_percent", "ram_percent", "ram_free_gb", "ram_total_gb"}
    if not isinstance(data, dict) or not required.issubset(data.keys()):
        raise HelperBadResponse(f"{address} /stats missing required fields")
    for key in ("cpu_percent", "ram_percent", "ram_free_gb", "ram_total_gb"):
        if not isinstance(data[key], (int, float)):
            raise HelperBadResponse(f"{address} /stats field '{key}' is not numeric")
    data.setdefault("status", "idle")
    return data


def _validate_process_dict(address: str, data, expected_count: int) -> dict:
    if not isinstance(data, dict) or "items" not in data:
        raise HelperBadResponse(f"{address} /process response missing 'items'")
    result_items = data["items"]
    if not isinstance(result_items, list) or len(result_items) != expected_count:
        raise HelperBadResponse(
            f"{address} /process returned {len(result_items) if isinstance(result_items, list) else 'non-list'} "
            f"item(s), expected {expected_count}"
        )
    if not all(isinstance(x, str) for x in result_items):
        raise HelperBadResponse(f"{address} /process returned non-string item data")
    return data


def fetch_stats(address: str, passphrase: str = None) -> dict:
    """GET /stats on a helper (direct or relay). Raises HelperUnreachable /
    HelperBadResponse on any problem; never lets a low-level exception
    escape to the caller. /stats itself doesn't require the passphrase
    (read-only telemetry) — passphrase is only enforced on /process."""
    if is_relay_address(address):
        status, headers, body = relay_client.relay_request(address, "GET", "/stats", None, {}, timeout=RELAY_STATS_WAIT)
        return _finish_stats(address, status, headers, body)

    url = f"{_base_url(address)}/stats"
    try:
        resp = requests.get(url, timeout=(CONNECT_TIMEOUT, STATS_READ_TIMEOUT))
    except requests.exceptions.RequestException as exc:
        raise HelperUnreachable(f"{address} unreachable: {exc}") from exc

    if resp.status_code != 200:
        raise HelperBadResponse(f"{address} /stats returned HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise HelperBadResponse(f"{address} /stats returned non-JSON body") from exc
    return _validate_stats_dict(address, data)


def _finish_stats(address, status, headers, body):
    if status is None:
        raise HelperUnreachable(f"{address} unreachable (relay could not deliver the request)")
    if status != 200:
        raise HelperBadResponse(f"{address} /stats returned HTTP {status}")
    try:
        data = wire.decompress_payload(body) if _is_compressed(headers) else json.loads(body)
    except Exception as exc:
        raise HelperBadResponse(f"{address} /stats returned an unreadable body") from exc
    return _validate_stats_dict(address, data)


def _is_compressed(headers) -> bool:
    # `headers` is a requests.structures.CaseInsensitiveDict on the direct
    # path (it does NOT subclass dict) and a plain dict on the relay path
    # — duck-type on .get() rather than isinstance(..., dict) so both work.
    try:
        return headers.get(wire.COMPRESSION_HEADER) == wire.COMPRESSION_MARKER
    except AttributeError:
        return False


def send_process_chunk(address: str, job_id: str, chunk_id: str, items: list, task_type: str, passphrase: str = None) -> dict:
    """POST /process on a helper (direct or relay) with a chunk of opaque
    work-item strings. Raises HelperUnreachable / HelperBadResponse /
    HelperAuthError on any problem. Validates the response shape before
    returning. The request body is zlib-compressed on the wire; the
    returned dict carries a "_compression" entry with the measured
    request/response compression ratios for the dashboard's live stat."""
    payload = {"job_id": job_id, "chunk_id": chunk_id, "task_type": task_type, "items": items}
    compressed = wire.compress_payload(payload)
    original_len = len(json.dumps(payload).encode("utf-8"))
    req_ratio = wire.ratio(original_len, len(compressed))

    headers = {
        "Content-Type": "application/octet-stream",
        wire.COMPRESSION_HEADER: wire.COMPRESSION_MARKER,
        **_auth_headers(passphrase),
    }
    read_timeout = PROCESS_BASE_TIMEOUT + PROCESS_TIMEOUT_PER_ITEM * max(1, len(items))

    if is_relay_address(address):
        status, resp_headers, body = relay_client.relay_request(address, "POST", "/process", compressed, headers, timeout=read_timeout)
        if status is None:
            raise HelperUnreachable(f"{address} unreachable (relay could not deliver the request)")
        return _finish_process(address, status, resp_headers, body, items, req_ratio, len(compressed))

    url = f"{_base_url(address)}/process"
    try:
        resp = requests.post(url, data=compressed, headers=headers, timeout=(CONNECT_TIMEOUT, read_timeout))
    except requests.exceptions.RequestException as exc:
        raise HelperUnreachable(f"{address} unreachable during /process: {exc}") from exc

    return _finish_process(address, resp.status_code, resp.headers, resp.content, items, req_ratio, len(compressed))


def _finish_process(address, status, headers, body, items, req_ratio, compressed_len):
    if status == 401:
        raise HelperAuthError(f"{address} rejected the request — passphrase missing or wrong")
    if status != 200:
        detail = ""
        try:
            err = json.loads(body)
            if isinstance(err, dict) and isinstance(err.get("error"), str):
                detail = f": {err['error']}"
        except Exception:
            pass
        raise HelperBadResponse(f"{address} /process returned HTTP {status}{detail}")

    try:
        if _is_compressed(headers):
            data = wire.decompress_payload(body)
            resp_ratio = wire.ratio(len(json.dumps(data).encode("utf-8")), len(body))
        else:
            data = json.loads(body)
            resp_ratio = 1.0
    except Exception as exc:
        raise HelperBadResponse(f"{address} /process returned an unreadable body") from exc

    data = _validate_process_dict(address, data, len(items))
    data["_compression"] = {
        "request_ratio": req_ratio,
        "response_ratio": resp_ratio,
        "request_bytes": compressed_len,
    }
    return data
