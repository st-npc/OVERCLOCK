"""Demo workload: Mandelbrot-set tile rendering.

A genuinely CPU-heavy, embarrassingly-splittable workload — the kind of
thing this project exists to offload, unlike the image task's filter
chain which is comparatively light. Implements the task contract
described in tasks/__init__.py.

Each work unit is a JSON string describing one vertical strip of a shared
viewport (not an image) — proof that a work unit doesn't have to be image
data at all, just an opaque string the same module knows how to consume.
"""
import json
import time

from PIL import Image

from tasks.image_task import decode_image, encode_image

DISPLAY_NAME = "Fractal rendering (Mandelbrot tiles)"

VIEW_X0, VIEW_X1 = -2.5, 1.0
VIEW_Y0, VIEW_Y1 = -1.25, 1.25
DEFAULT_WIDTH = 640
DEFAULT_HEIGHT = 480
DEFAULT_MAX_ITER = 250

# A /process caller controls width/height/max_iter/x_start/x_end directly
# (they're read straight out of the request's JSON items — see tasks/__init__.py's
# contract: a task never gets Flask/validation help beyond what it does itself).
# Image.new() has no built-in bomb guard the way Image.open() does, so without
# a cap here a single crafted item (e.g. a 50000x50000 tile) can OOM-kill this
# process from one unauthenticated /process call. These caps comfortably cover
# every size this app itself ever generates (DEFAULT_WIDTH/HEIGHT above).
MAX_TILE_DIMENSION = 4096
MAX_TILE_PIXELS = 4096 * 4096
MAX_ITER_CAP = 10_000


class InvalidRenderSpec(ValueError):
    """Raised when a work-item's render spec is outside the sizes this task
    will ever legitimately produce itself."""


def generate_work(n: int, width: int = DEFAULT_WIDTH, height: int = DEFAULT_HEIGHT, max_iter: int = DEFAULT_MAX_ITER) -> list:
    """Split the viewport into n contiguous vertical strips, one work unit each."""
    edges = [round(i * width / n) for i in range(n + 1)]
    items = []
    for i in range(n):
        x_start, x_end = edges[i], edges[i + 1]
        if x_end <= x_start:
            x_end = x_start + 1
        items.append(
            json.dumps(
                {"index": i, "x_start": x_start, "x_end": x_end, "width": width, "height": height, "max_iter": max_iter}
            )
        )
    return items


def _escape_iter(cx: float, cy: float, max_iter: int) -> int:
    x, y = 0.0, 0.0
    for i in range(max_iter):
        x2, y2 = x * x, y * y
        if x2 + y2 > 4.0:
            return i
        y = 2 * x * y + cy
        x = x2 - y2 + cx
    return max_iter


def _color(i: int, max_iter: int) -> tuple:
    if i >= max_iter:
        return (8, 8, 20)
    t = i / max_iter
    r = int(9 * (1 - t) * t ** 3 * 255)
    g = int(15 * (1 - t) ** 2 * t ** 2 * 255)
    b = int(8.5 * (1 - t) ** 3 * t * 255)
    return (r, g, b)


def _render_tile(spec: dict) -> Image.Image:
    x_start, x_end = spec["x_start"], spec["x_end"]
    width, height, max_iter = spec["width"], spec["height"], spec["max_iter"]
    tile_w = x_end - x_start

    if not all(isinstance(v, int) and not isinstance(v, bool) for v in (x_start, x_end, width, height, max_iter)):
        raise InvalidRenderSpec("x_start/x_end/width/height/max_iter must all be integers")
    if tile_w <= 0 or height <= 0 or width <= 0:
        raise InvalidRenderSpec("width/height/tile width must be positive")
    if width > MAX_TILE_DIMENSION or height > MAX_TILE_DIMENSION or tile_w > MAX_TILE_DIMENSION:
        raise InvalidRenderSpec(f"width/height/tile width must each be <= {MAX_TILE_DIMENSION}")
    if tile_w * height > MAX_TILE_PIXELS:
        raise InvalidRenderSpec(f"tile is too large ({tile_w}x{height} exceeds {MAX_TILE_PIXELS} pixels)")
    if max_iter <= 0 or max_iter > MAX_ITER_CAP:
        raise InvalidRenderSpec(f"max_iter must be between 1 and {MAX_ITER_CAP}")

    xs = [VIEW_X0 + ((x_start + px) / width) * (VIEW_X1 - VIEW_X0) for px in range(tile_w)]
    ys = [VIEW_Y0 + (py / height) * (VIEW_Y1 - VIEW_Y0) for py in range(height)]

    img = Image.new("RGB", (tile_w, height))
    pixels = img.load()
    for px, cx in enumerate(xs):
        for py, cy in enumerate(ys):
            pixels[px, py] = _color(_escape_iter(cx, cy, max_iter), max_iter)
    return img


def _process_one(item: str) -> str:
    spec = json.loads(item)
    return encode_image(_render_tile(spec))


def process_batch(items: list) -> dict:
    start = time.monotonic()
    results = [_process_one(item) for item in items]
    elapsed = time.monotonic() - start
    return {"items": results, "elapsed_seconds": elapsed, "count": len(results)}


def save_result(item: str, out_path_base: str) -> str:
    path = out_path_base + ".png"
    decode_image(item).save(path)
    return path
