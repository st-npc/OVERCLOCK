"""Demo workload: a batch image-processing task.

This is the ONLY file that should need to change to swap the demo task
for something else (compile job, ML batch, etc). It has no knowledge of
networking, Flask, or device state — it just transforms images.
"""
import base64
import io
import time

from PIL import Image, ImageFilter


def make_demo_images(count: int, size: tuple = (640, 480)) -> list:
    """Generate `count` synthetic images (no external assets needed) and
    return them as base64-encoded PNG strings, ready to feed into a batch."""
    images = []
    for i in range(count):
        img = Image.new("RGB", size)
        pixels = img.load()
        # Cheap deterministic gradient pattern so every image differs and
        # the demo doesn't need bundled asset files.
        r_shift = (i * 37) % 256
        g_shift = (i * 91) % 256
        for x in range(0, size[0], 4):
            for y in range(0, size[1], 4):
                r = (x + r_shift) % 256
                g = (y + g_shift) % 256
                b = (i * 23) % 256
                for dx in range(4):
                    for dy in range(4):
                        if x + dx < size[0] and y + dy < size[1]:
                            pixels[x + dx, y + dy] = (r, g, b)
        images.append(encode_image(img))
    return images


def encode_image(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def decode_image(data: str) -> Image.Image:
    raw = base64.b64decode(data, validate=True)
    return Image.open(io.BytesIO(raw))


def process_one(image_b64: str) -> str:
    """Simulate a moderately heavy per-image transform."""
    img = decode_image(image_b64)
    img = img.convert("RGB")
    img = img.filter(ImageFilter.GaussianBlur(radius=6))
    img = img.filter(ImageFilter.FIND_EDGES)
    img = img.filter(ImageFilter.SMOOTH_MORE)
    return encode_image(img)


def process_batch(images: list) -> dict:
    """Process a list of base64 images. Returns timing + results so
    callers (orchestrator, helper endpoint) can report throughput without
    re-deriving it themselves.
    """
    start = time.monotonic()
    results = [process_one(img) for img in images]
    elapsed = time.monotonic() - start
    return {"images": results, "elapsed_seconds": elapsed, "count": len(results)}
