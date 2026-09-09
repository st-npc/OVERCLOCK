"""Wire-format helpers shared by the direct and relay transports: zlib+JSON
payload compression, so both paths compress/decompress identically and
`/process` doesn't need to know which transport a request arrived over.
"""
import json
import zlib

COMPRESSION_HEADER = "X-Overclock-Compressed"
COMPRESSION_MARKER = "zlib"

# zlib can expand a tiny compressed payload into a huge one (a "zip bomb")
# — every caller of decompress_payload is fed data from the network (a
# helper's /process body, or a peer's /stats response), so this cap turns an
# unbounded-memory DoS into a clean, catchable error instead. Comfortably
# above any real work chunk this app produces (500 images/tiles per chunk).
MAX_DECOMPRESSED_BYTES = 96 * 1024 * 1024


def compress_payload(payload: dict) -> bytes:
    raw = json.dumps(payload).encode("utf-8")
    return zlib.compress(raw, level=6)


def decompress_payload(data: bytes) -> dict:
    decompressor = zlib.decompressobj()
    try:
        out = decompressor.decompress(data, MAX_DECOMPRESSED_BYTES + 1)
    except zlib.error as exc:
        raise ValueError(f"invalid compressed payload: {exc}") from exc
    if decompressor.unconsumed_tail:
        raise ValueError("decompressed payload exceeds the size limit")
    out += decompressor.flush()
    if len(out) > MAX_DECOMPRESSED_BYTES:
        raise ValueError("decompressed payload exceeds the size limit")
    return json.loads(out)


def ratio(original_bytes: int, compressed_bytes: int) -> float:
    """compressed/original — smaller is better; 1.0 = no savings."""
    if original_bytes <= 0:
        return 1.0
    return round(compressed_bytes / original_bytes, 4)
