"""Hardened HTTP client for talking to helper devices.

Every network boundary in this app goes through here so timeouts,
malformed-response handling, and error typing are consistent in one place
instead of scattered across orchestrator/monitor code.
"""
import requests

CONNECT_TIMEOUT = 2.0      # helper should ack fast if it's alive at all
STATS_READ_TIMEOUT = 2.5   # /stats is cheap, should return almost instantly
PROCESS_BASE_TIMEOUT = 5.0        # baseline read timeout for /process
PROCESS_TIMEOUT_PER_IMAGE = 1.5   # extra allowance per image in the chunk


class HelperError(Exception):
    """Base class for all helper-communication failures. Any caller that
    catches this treats the helper as unavailable for this attempt."""


class HelperUnreachable(HelperError):
    """Connection refused/timed out/DNS failed — helper is not responding."""


class HelperBadResponse(HelperError):
    """Helper responded, but with something we can't safely use (bad
    status code, non-JSON body, or JSON missing/mismatching expected shape)."""


def _base_url(address: str) -> str:
    address = address.strip()
    if not address.startswith("http://") and not address.startswith("https://"):
        address = "http://" + address
    return address.rstrip("/")


def fetch_stats(address: str) -> dict:
    """GET /stats on a helper. Raises HelperUnreachable / HelperBadResponse
    on any problem; never lets a requests exception escape to the caller."""
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

    required = {"cpu_percent", "ram_percent", "ram_free_gb", "ram_total_gb"}
    if not isinstance(data, dict) or not required.issubset(data.keys()):
        raise HelperBadResponse(f"{address} /stats missing required fields")

    for key in ("cpu_percent", "ram_percent", "ram_free_gb", "ram_total_gb"):
        if not isinstance(data[key], (int, float)):
            raise HelperBadResponse(f"{address} /stats field '{key}' is not numeric")

    data.setdefault("status", "idle")
    return data


def send_process_chunk(address: str, job_id: str, chunk_id: str, images: list) -> dict:
    """POST /process on a helper with a chunk of images. Raises
    HelperUnreachable / HelperBadResponse on any problem. Validates the
    response shape (count matches, images are strings) before returning."""
    url = f"{_base_url(address)}/process"
    payload = {"job_id": job_id, "chunk_id": chunk_id, "images": images}
    read_timeout = PROCESS_BASE_TIMEOUT + PROCESS_TIMEOUT_PER_IMAGE * max(1, len(images))

    try:
        resp = requests.post(url, json=payload, timeout=(CONNECT_TIMEOUT, read_timeout))
    except requests.exceptions.RequestException as exc:
        raise HelperUnreachable(f"{address} unreachable during /process: {exc}") from exc

    if resp.status_code != 200:
        raise HelperBadResponse(f"{address} /process returned HTTP {resp.status_code}")

    try:
        data = resp.json()
    except ValueError as exc:
        raise HelperBadResponse(f"{address} /process returned non-JSON body") from exc

    if not isinstance(data, dict) or "images" not in data:
        raise HelperBadResponse(f"{address} /process response missing 'images'")

    result_images = data["images"]
    if not isinstance(result_images, list) or len(result_images) != len(images):
        raise HelperBadResponse(
            f"{address} /process returned {len(result_images) if isinstance(result_images, list) else 'non-list'} "
            f"images, expected {len(images)}"
        )
    if not all(isinstance(x, str) for x in result_images):
        raise HelperBadResponse(f"{address} /process returned non-string image data")

    return data
