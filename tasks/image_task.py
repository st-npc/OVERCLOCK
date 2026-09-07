"""Demo workload: batch image processing (blur + edge-detect filter chain).

Implements the task contract described in tasks/__init__.py. No
networking/Flask imports — it just transforms images.
"""
import base64
import io
import time

from PIL import Image, ImageFilter

DISPLAY_NAME = "Image batch (blur + edge detect)"


def encode_image(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def decode_image(data: str) -> Image.Image:
    raw = base64.b64decode(data, validate=True)
    return Image.open(io.BytesIO(raw))


def generate_work(n: int, size: tuple = (640, 480)) -> list:
    """Generate n synthetic source images (no bundled assets needed)."""
    items = []
    for i in range(n):
        img = Image.new("RGB", size)
        pixels = img.load()
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
        items.append(encode_image(img))
    return items


def _process_one(image_b64: str) -> str:
    img = decode_image(image_b64)
    img = img.convert("RGB")
    img = img.filter(ImageFilter.GaussianBlur(radius=6))
    img = img.filter(ImageFilter.FIND_EDGES)
    img = img.filter(ImageFilter.SMOOTH_MORE)
    return encode_image(img)


def process_batch(items: list) -> dict:
    start = time.monotonic()
    results = [_process_one(item) for item in items]
    elapsed = time.monotonic() - start
    return {"items": results, "elapsed_seconds": elapsed, "count": len(results)}


def save_result(item: str, out_path_base: str) -> str:
    path = out_path_base + ".png"
    decode_image(item).save(path)
    return path
