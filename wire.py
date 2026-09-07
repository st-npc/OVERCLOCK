"""Wire-format helpers shared by the direct and relay transports: zlib+JSON
payload compression, so both paths compress/decompress identically and
`/process` doesn't need to know which transport a request arrived over.
"""
import json
import zlib

COMPRESSION_HEADER = "X-Overclock-Compressed"
COMPRESSION_MARKER = "zlib"


def compress_payload(payload: dict) -> bytes:
    raw = json.dumps(payload).encode("utf-8")
    return zlib.compress(raw, level=6)


def decompress_payload(data: bytes) -> dict:
    raw = zlib.decompress(data)
    return json.loads(raw)


def ratio(original_bytes: int, compressed_bytes: int) -> float:
    """compressed/original — smaller is better; 1.0 = no savings."""
    if original_bytes <= 0:
        return 1.0
    return round(compressed_bytes / original_bytes, 4)
