"""Perceptual hashes of card art, for the app's scanner.

Each Scryfall illustration (art_crop image) gets a 64-bit pHash. The app
computes the same hash from the camera frame (app/lib/scanner/art_hash.dart)
and compares Hamming distances to tell printings with different art apart.

Hashes are cached per illustration id in art_hashes_mtg.json.gz (published
next to the catalog), so each daily run only downloads new illustrations.

Needs Pillow + numpy (installed by the workflow). Without them the catalog is
built without the art table.
"""

from __future__ import annotations

import gzip
import io
import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
from PIL import Image

HASH_SIZE = 8  # 8x8 low frequencies -> 64 bits
SAMPLE = 32  # image is reduced to 32x32 before the DCT
INNER = 0.8  # keep the central 80% (the camera crop is approximate)

_N = np.arange(SAMPLE)
# DCT-II basis, rows = frequency: C[k, n] = cos(pi * (2n + 1) * k / (2N)).
_DCT = np.cos(np.pi * (2 * _N[None, :] + 1) * _N[:, None] / (2 * SAMPLE))


def phash_from_gray(gray: np.ndarray) -> int:
    """pHash of a 32x32 grayscale array (0..255). Mirrors phashFromSamples in
    app/lib/scanner/art_hash.dart: low 8x8 of the 2D DCT, bit = coef > median,
    row-major, first coefficient in the most significant bit. Returned as a
    signed 64-bit int (SQLite INTEGER / Dart int)."""
    d = _DCT @ gray.astype(np.float64) @ _DCT.T
    low = d[:HASH_SIZE, :HASH_SIZE].flatten()
    med = float(np.median(low))
    v = 0
    for c in low:
        v = (v << 1) | (1 if c > med else 0)
    return v - (1 << 64) if v >= 1 << 63 else v


def phash_image(img: Image.Image) -> int:
    g = img.convert("L")
    w, h = g.size
    mx, my = w * (1 - INNER) / 2, h * (1 - INNER) / 2
    g = g.crop((round(mx), round(my), round(w - mx), round(h - my))).resize((SAMPLE, SAMPLE), Image.BOX)
    return phash_from_gray(np.asarray(g, dtype=np.float64))


def _download_hash(url: str, user_agent: str) -> int | None:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept": "image/*"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return phash_image(Image.open(io.BytesIO(r.read())))
        except Exception:  # noqa: BLE001 - retry any network/decode failure
            time.sleep(2 * (attempt + 1))
    return None


def load_cache(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def save_cache(path: Path, cache: dict[str, int]) -> None:
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=9) as f:
        json.dump(cache, f, separators=(",", ":"), sort_keys=True)


def update_hashes(
    wanted: dict[str, str], cache: dict[str, int], *, user_agent: str, max_new: int, workers: int = 16, log=print
) -> int:
    """Hashes illustrations in `wanted` (illustration id -> art_crop URL) that
    are not cached yet, at most `max_new` per run. Returns how many were added.
    Scryfall's image CDN (cards.scryfall.io) is not rate limited."""
    todo = [(iid, url) for iid, url in wanted.items() if iid not in cache][:max_new]
    log(f"art: {len(cache)} cached, {len(todo)} to hash")
    added = 0
    with ThreadPoolExecutor(workers) as pool:
        futures = {pool.submit(_download_hash, url, user_agent): iid for iid, url in todo}
        for i, fut in enumerate(as_completed(futures), 1):
            h = fut.result()
            if h is not None:
                cache[futures[fut]] = h
                added += 1
            if i % 2000 == 0:
                log(f"art: {i}/{len(todo)}")
    return added
