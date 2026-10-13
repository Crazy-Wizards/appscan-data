"""Build AppScan data packs (catalog + daily prices) for Magic: The Gathering.

Outputs (in --out):
  catalog_mtg.sqlite.gz  compact card catalog with FTS5 search index
  prices_mtg.jsonl.gz    today's prices per Scryfall id, per source and finish
  manifest.json          versions, hashes and sizes the app checks before downloading

Standard library only, so the GitHub Action needs no pip install.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import hashlib
import json
import lzma
import shutil
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

USER_AGENT = "AppScanData/0.1 (+https://github.com/pumpedkk/appscan-data)"
SCRYFALL_IMG = "https://cards.scryfall.io/normal/"

SCRYFALL_BULK = "https://api.scryfall.com/bulk-data/default-cards"
SCRYFALL_SETS = "https://api.scryfall.com/sets"
MTGJSON_PRICES = "https://mtgjson.com/api/v5/AllPricesToday.json.xz"
MTGJSON_IDS = "https://mtgjson.com/api/v5/csv/cardIdentifiers.csv.xz"
CARDKINGDOM = "https://api.cardkingdom.com/api/v2/pricelist"
CARDMARKET = "https://downloads.s3.cardmarket.com/productCatalog/priceGuide/price_guide_1.json"
FX = "https://api.frankfurter.dev/v1/latest?base=USD&symbols=BRL,EUR"

CATALOG_SCHEMA_VERSION = 1
PRICES_SCHEMA_VERSION = 1

# Price pack keys (kept short: the file ships to phones).
#   finish: n = nonfoil, f = foil, e = etched
#   fields: r = market/retail value used for valuation, b = buylist,
#           low / avg = marketplace stats, nm/ex/vg/g = per-condition retail
SOURCES = {
    "tcgplayer": {"name": "TCGplayer", "currency": "USD", "origin": "MTGJSON"},
    "cardkingdom": {"name": "Card Kingdom", "currency": "USD", "origin": "Card Kingdom"},
    "cardmarket": {"name": "Cardmarket", "currency": "EUR", "origin": "Cardmarket"},
}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def fetch(url: str, dest: Path, cache: bool) -> Path:
    """Download url to dest (reused when cache=True and the file exists)."""
    if cache and dest.exists():
        log(f"cache hit {dest.name}")
        return dest
    log(f"GET {url}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=300) as r, open(dest, "wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
            return dest
        except Exception as e:  # noqa: BLE001 - retry any network failure
            if attempt == 3:
                raise
            log(f"retry {attempt + 1} after error: {e}")
            time.sleep(5 * (attempt + 1))
    return dest


def fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def num(v) -> float | None:
    """Parse a price; zero/empty/invalid -> None."""
    if v is None or v == "":
        return None
    try:
        f = round(float(v), 2)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def img_path(uris: dict | None) -> str | None:
    url = (uris or {}).get("normal")
    return url[len(SCRYFALL_IMG):] if url and url.startswith(SCRYFALL_IMG) else url


# ---------------------------------------------------------------- catalog

CATALOG_DDL = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE sets (
  code TEXT PRIMARY KEY, name TEXT NOT NULL, set_type TEXT, released_at TEXT,
  card_count INTEGER, parent_code TEXT, icon_svg TEXT
);
CREATE TABLE cards (
  id TEXT PRIMARY KEY,            -- Scryfall id
  oracle_id TEXT,
  name TEXT NOT NULL,
  printed_name TEXT,              -- non-English printed name
  lang TEXT NOT NULL,
  set_code TEXT NOT NULL,
  collector_number TEXT NOT NULL,
  rarity TEXT,
  type_line TEXT,
  mana_cost TEXT,
  cmc REAL,
  colors TEXT,                    -- e.g. "WU"
  color_identity TEXT,
  finishes TEXT NOT NULL,         -- subset of "nfe" (nonfoil, foil, etched)
  layout TEXT,
  oracle_text TEXT,
  artist TEXT,
  released_at TEXT,
  img_front TEXT,                 -- path after https://cards.scryfall.io/<size>/
  img_back TEXT,
  tcgplayer_id INTEGER,
  cardmarket_id INTEGER,
  promo INTEGER NOT NULL DEFAULT 0,
  variation INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX cards_set_num ON cards(set_code, collector_number);
CREATE INDEX cards_oracle ON cards(oracle_id);
CREATE INDEX cards_name ON cards(name COLLATE NOCASE);
CREATE VIRTUAL TABLE cards_fts USING fts5(
  name, printed_name, content='cards', content_rowid='rowid',
  tokenize='unicode61 remove_diacritics 2'
);
"""

FINISH_CODE = {"nonfoil": "n", "foil": "f", "etched": "e"}


def card_row(c: dict) -> tuple | None:
    if "paper" not in c.get("games", []):
        return None  # skip Arena/MTGO-only printings
    faces = c.get("card_faces") or []
    front = c.get("image_uris") or (faces[0].get("image_uris") if faces else None)
    back = faces[1].get("image_uris") if len(faces) > 1 and not c.get("image_uris") else None
    oracle = c.get("oracle_text")
    if oracle is None and faces:
        oracle = "\n//\n".join(f.get("oracle_text", "") for f in faces)
    mana = c.get("mana_cost")
    if mana is None and faces:
        mana = " // ".join(f.get("mana_cost", "") for f in faces)
    return (
        c["id"],
        c.get("oracle_id") or (faces[0].get("oracle_id") if faces else None),
        c["name"],
        c.get("printed_name"),
        c.get("lang", "en"),
        c["set"],
        c["collector_number"],
        c.get("rarity"),
        c.get("type_line"),
        mana,
        c.get("cmc"),
        "".join(c.get("colors") or []),
        "".join(c.get("color_identity") or []),
        "".join(FINISH_CODE[f] for f in c.get("finishes", []) if f in FINISH_CODE),
        c.get("layout"),
        oracle,
        c.get("artist"),
        c.get("released_at"),
        img_path(front),
        img_path(back),
        c.get("tcgplayer_id"),
        c.get("cardmarket_id"),
        int(bool(c.get("promo"))),
        int(bool(c.get("variation"))),
    )


def build_catalog(work: Path, out: Path, cache: bool) -> dict:
    bulk = fetch_json(SCRYFALL_BULK)
    jsonl = fetch(bulk["jsonl_download_uri"], work / "default_cards.jsonl.gz", cache)

    db_path = work / "catalog_mtg.sqlite"
    db_path.unlink(missing_ok=True)
    db = sqlite3.connect(db_path)
    db.executescript(CATALOG_DDL)

    sets = fetch_json(SCRYFALL_SETS)["data"]
    db.executemany(
        "INSERT INTO sets VALUES (?,?,?,?,?,?,?)",
        [(s["code"], s["name"], s.get("set_type"), s.get("released_at"), s.get("card_count"),
          s.get("parent_set_code"), s.get("icon_svg_uri")) for s in sets],
    )

    digest = hashlib.sha256()
    rows, skipped = [], 0
    with gzip.open(jsonl, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip().rstrip(",")
            if not line or line in ("[", "]"):
                continue
            row = card_row(json.loads(line))
            if row is None:
                skipped += 1
                continue
            rows.append(row)
    rows.sort(key=lambda r: r[0])
    for r in rows:  # structural hash: ignores image cache-busters and text tweaks
        digest.update(f"{r[0]}|{r[2]}|{r[5]}|{r[6]}|{r[13]}\n".encode())
    db.executemany(f"INSERT INTO cards VALUES ({','.join('?' * 24)})", rows)
    db.execute("INSERT INTO cards_fts(cards_fts) VALUES ('rebuild')")

    version = digest.hexdigest()[:16]
    built = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    db.executemany("INSERT INTO meta VALUES (?,?)", [
        ("game", "mtg"),
        ("schema_version", str(CATALOG_SCHEMA_VERSION)),
        ("version", version),
        ("built_at", built),
        ("card_count", str(len(rows))),
        ("image_base", "https://cards.scryfall.io/{size}/"),
        ("attribution", "Card data and images: Scryfall (scryfall.com). Wizards of the Coast."),
    ])
    db.commit()
    db.execute("VACUUM")
    db.close()
    log(f"catalog: {len(rows)} cards, {len(sets)} sets, skipped {skipped} digital-only")

    gz = out / "catalog_mtg.sqlite.gz"
    with open(db_path, "rb") as src, gzip.open(gz, "wb", compresslevel=9) as dst:
        shutil.copyfileobj(src, dst, 1 << 20)
    return {
        "file": gz.name, "version": version, "schema_version": CATALOG_SCHEMA_VERSION,
        "built_at": built, "cards": len(rows), "size": gz.stat().st_size,
        "raw_size": db_path.stat().st_size, "sha256": sha256(gz),
    }


# ---------------------------------------------------------------- prices

def put(cards: dict, finishes: dict[str, str], sid: str, source: str, finish: str, fields: dict) -> None:
    fields = {k: v for k, v in fields.items() if v is not None}
    if not fields or finish not in finishes.get(sid, ""):
        return  # skip finishes the printing doesn't exist in (e.g. Cardmarket "low" on foil-only cards)
    slot = cards.setdefault(sid, {}).setdefault(source, {}).setdefault(finish, {})
    for k, v in fields.items():
        slot.setdefault(k, v)  # first writer wins: direct feeds run before fallbacks


def load_catalog_ids(work: Path) -> tuple[dict[str, str], dict[int, str]]:
    """Scryfall id -> finishes, and cardmarket_id -> Scryfall id."""
    db = sqlite3.connect(work / "catalog_mtg.sqlite")
    ids, cm = {}, {}
    for sid, finishes, cmid in db.execute("SELECT id, finishes, cardmarket_id FROM cards"):
        ids[sid] = finishes
        if cmid:
            cm.setdefault(cmid, sid)
    db.close()
    return ids, cm


def ingest_cardkingdom(work: Path, cache: bool, cards: dict, known: dict[str, str]) -> int:
    path = fetch(CARDKINGDOM, work / "cardkingdom.json", cache)
    data = json.loads(path.read_text(encoding="utf-8"))["data"]
    n = 0
    for it in data:
        sid = it.get("scryfall_id")
        if sid not in known:
            continue
        foil = str(it.get("is_foil")).lower() == "true"
        finish = "e" if foil and "etched" in (it.get("variation") or "").lower() else ("f" if foil else "n")
        cv = it.get("condition_values") or {}
        put(cards, known, sid, "cardkingdom", finish, {
            "r": num(it.get("price_retail")), "b": num(it.get("price_buy")),
            "nm": num(cv.get("nm_price")), "ex": num(cv.get("ex_price")),
            "vg": num(cv.get("vg_price")), "g": num(cv.get("g_price")),
        })
        n += 1
    return n


def ingest_cardmarket(work: Path, cache: bool, cards: dict, known: dict[str, str], cm_map: dict[int, str]) -> int:
    path = fetch(CARDMARKET, work / "cardmarket.json", cache)
    n = 0
    for g in json.loads(path.read_text(encoding="utf-8"))["priceGuides"]:
        sid = cm_map.get(g.get("idProduct"))
        if not sid:
            continue
        put(cards, known, sid, "cardmarket", "n", {"r": num(g.get("trend")), "low": num(g.get("low")), "avg": num(g.get("avg"))})
        put(cards, known, sid, "cardmarket", "f", {"r": num(g.get("trend-foil")), "low": num(g.get("low-foil")), "avg": num(g.get("avg-foil"))})
        n += 1
    return n


def latest(series: dict | None) -> float | None:
    if not series:
        return None
    return num(series[max(series)])


def ingest_mtgjson(work: Path, cache: bool, cards: dict, known: dict[str, str]) -> int:
    """TCGplayer prices, plus fallbacks for CK/Cardmarket gaps."""
    ids_path = fetch(MTGJSON_IDS, work / "cardIdentifiers.csv.xz", cache)
    uuid_to_sid: dict[str, str] = {}
    with lzma.open(ids_path, "rt", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("scryfallId") in known:
                uuid_to_sid[row["uuid"]] = row["scryfallId"]

    prices_path = fetch(MTGJSON_PRICES, work / "AllPricesToday.json.xz", cache)
    with lzma.open(prices_path, "rt", encoding="utf-8") as f:
        data = json.load(f)["data"]

    finish_map = {"normal": "n", "foil": "f", "etched": "e"}
    n = 0
    for uuid, entry in data.items():
        sid = uuid_to_sid.get(uuid)
        if not sid:
            continue
        paper = entry.get("paper") or {}
        for source in SOURCES:
            p = paper.get(source)
            if not p:
                continue
            for mj_finish, code in finish_map.items():
                put(cards, known, sid, source, code, {
                    "r": latest((p.get("retail") or {}).get(mj_finish)),
                    "b": latest((p.get("buylist") or {}).get(mj_finish)),
                })
        n += 1
    return n


def build_prices(work: Path, out: Path, cache: bool) -> dict:
    known, cm_map = load_catalog_ids(work)
    cards: dict = {}
    counts = {
        "cardkingdom": ingest_cardkingdom(work, cache, cards, known),
        "cardmarket": ingest_cardmarket(work, cache, cards, known, cm_map),
        "mtgjson": ingest_mtgjson(work, cache, cards, known),
    }
    fx = fetch_json(FX)
    today = dt.date.today().isoformat()
    pack = {
        "v": PRICES_SCHEMA_VERSION,
        "game": "mtg",
        "date": today,
        "fx": {"base": "USD", "date": fx["date"], "rates": {"USD": 1.0, **fx["rates"]}},
        "sources": SOURCES,
        "cards": cards,
    }
    # JSONL: line 1 = header (everything but cards), then one {"id", "p"} per card,
    # so phones can stream-parse instead of loading the whole document.
    gz = out / "prices_mtg.jsonl.gz"
    header = {k: v for k, v in pack.items() if k != "cards"}
    with gzip.open(gz, "wt", encoding="utf-8", compresslevel=9, newline="\n") as f:
        f.write(json.dumps(header, separators=(",", ":"), sort_keys=True) + "\n")
        for sid in sorted(cards):
            f.write(json.dumps({"id": sid, "p": cards[sid]}, separators=(",", ":"), sort_keys=True) + "\n")
    log(f"prices: {len(cards)} cards priced, ingest counts {counts}")
    return {
        "file": gz.name, "date": today, "schema_version": PRICES_SCHEMA_VERSION,
        "cards": len(cards), "size": gz.stat().st_size, "sha256": sha256(gz),
        "fx": pack["fx"],
    }


# ---------------------------------------------------------------- main

def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", default="work", help="download/scratch dir")
    ap.add_argument("--out", default="out", help="output dir for release assets")
    ap.add_argument("--cache", action="store_true", help="reuse downloaded source files (local dev)")
    args = ap.parse_args(argv)

    work, out = Path(args.work), Path(args.out)
    work.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)

    catalog = build_catalog(work, out, args.cache)
    prices = build_prices(work, out, args.cache)
    manifest = {
        "manifest_version": 1,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "games": {"mtg": {"catalog": catalog, "prices": prices}},
        "attribution": [
            "Card data and images courtesy of Scryfall (scryfall.com).",
            "TCGplayer prices via MTGJSON (mtgjson.com, MIT).",
            "Prices from Card Kingdom (cardkingdom.com) and Cardmarket (cardmarket.com).",
            "Exchange rates: Frankfurter / European Central Bank.",
        ],
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log(f"done: {json.dumps({k: v['size'] for k, v in manifest['games']['mtg'].items()})}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
