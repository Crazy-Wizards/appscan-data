"""Set symbol masks, for the app's scanner.

Each set's icon SVG (Scryfall icon_svg_uri) is rendered and reduced to a
32x32 silhouette: holes filled, cropped to its bounding box and fitted with
the aspect ratio kept. The app builds the same silhouette from the symbol it
sees on the type line (app/lib/scanner/icon_mask.dart) and compares them.

Masks are cached per SVG URL in set_icons_mtg.json.gz (published next to the
catalog). Needs Pillow, numpy and resvg-py (installed by the workflow).
"""

from __future__ import annotations

import gzip
import io
import json
import time
import urllib.request
from collections import deque

import numpy as np
from PIL import Image

MASK = 32
RENDER = 128


def fill_holes(fg: np.ndarray) -> np.ndarray:
    """Background is what the border reaches through background pixels
    (4-neighbourhood); everything else is foreground."""
    h, w = fg.shape
    outside = np.zeros_like(fg, dtype=bool)
    q = deque()
    for y in range(h):
        for x in (0, w - 1):
            if not fg[y, x] and not outside[y, x]:
                outside[y, x] = True
                q.append((y, x))
    for x in range(w):
        for y in (0, h - 1):
            if not fg[y, x] and not outside[y, x]:
                outside[y, x] = True
                q.append((y, x))
    while q:
        y, x = q.popleft()
        for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
            if 0 <= ny < h and 0 <= nx < w and not fg[ny, nx] and not outside[ny, nx]:
                outside[ny, nx] = True
                q.append((ny, nx))
    return ~outside


def normalize(fg: np.ndarray) -> bytes | None:
    """32x32 silhouette of the foreground's bounding box, aspect kept and
    centered; a cell is set when at least half of its 3x3 samples are.
    Packed row-major, most significant bit first (128 bytes). Mirrors
    normalizeMask in app/lib/scanner/icon_mask.dart."""
    ys, xs = np.nonzero(fg)
    if len(ys) == 0:
        return None
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    bw, bh = x1 - x0, y1 - y0
    scale = MASK / max(bw, bh)
    ow, oh = bw * scale, bh * scale
    ox, oy = (MASK - ow) / 2, (MASK - oh) / 2
    bits = []
    for cy in range(MASK):
        for cx in range(MASK):
            hits = 0
            for sy in range(3):
                for sx in range(3):
                    px = (cx + (sx + 0.5) / 3 - ox) / scale
                    py = (cy + (sy + 0.5) / 3 - oy) / scale
                    if 0 <= px < bw and 0 <= py < bh and fg[y0 + int(py), x0 + int(px)]:
                        hits += 1
            bits.append(1 if hits >= 5 else 0)
    out = bytearray(MASK * MASK // 8)
    for i, b in enumerate(bits):
        if b:
            out[i >> 3] |= 0x80 >> (i & 7)
    return bytes(out)


def mask_from_svg(svg: str) -> bytes | None:
    import resvg_py

    png = resvg_py.svg_to_bytes(svg_string=svg, height=RENDER)
    alpha = np.asarray(Image.open(io.BytesIO(bytes(png))).convert("RGBA"))[:, :, 3]
    return normalize(fill_holes(alpha >= 128))


def _download_mask(url: str, user_agent: str) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept": "image/svg+xml,*/*"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return mask_from_svg(r.read().decode("utf-8"))
        except Exception:  # noqa: BLE001 - retry any network/render failure
            time.sleep(2 * (attempt + 1))
    return None


def load_cache(path) -> dict[str, str]:
    if not path.exists():
        return {}
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def save_cache(path, cache: dict[str, str]) -> None:
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=9) as f:
        json.dump(cache, f, separators=(",", ":"), sort_keys=True)


def update_masks(urls: set[str], cache: dict[str, str], *, user_agent: str, log=print) -> int:
    """Renders icons not cached yet (URL -> hex mask). Returns how many were added."""
    added = 0
    todo = sorted(u for u in urls if u not in cache)
    log(f"icons: {len(cache)} cached, {len(todo)} to render")
    for url in todo:
        m = _download_mask(url, user_agent)
        if m is not None:
            cache[url] = m.hex()
            added += 1
    return added
