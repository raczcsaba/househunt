"""SQLite storage. One row per listing per source; history of prices kept as JSON."""
import json
import sqlite3
from datetime import date, datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
  uid TEXT PRIMARY KEY,           -- source:source_id
  source TEXT NOT NULL,
  source_id TEXT NOT NULL,
  town TEXT NOT NULL,
  url TEXT,
  title TEXT,
  price INTEGER,
  size INTEGER,
  plot INTEGER,
  rooms REAL,
  condition TEXT,
  address TEXT,
  description TEXT,
  agency TEXT,
  lat REAL, lon REAL,
  loc_quality TEXT,               -- exact | area | street | claude | town | none
  loc_note TEXT,
  near_station_text INTEGER,      -- ad text says it is near the station (Claude)
  dist_km REAL, bike_min INTEGER,
  detail_done INTEGER DEFAULT 0,
  enrich_done INTEGER DEFAULT 0,
  first_seen TEXT, last_seen TEXT,
  active INTEGER DEFAULT 1,
  removed_on TEXT,
  price_history TEXT DEFAULT '[]',
  cluster TEXT
);
CREATE INDEX IF NOT EXISTS ix_town ON listings(town);
CREATE INDEX IF NOT EXISTS ix_src_town ON listings(source, town);
CREATE TABLE IF NOT EXISTS geocache (q TEXT PRIMARY KEY, lat REAL, lon REAL, label TEXT);
CREATE TABLE IF NOT EXISTS carcache (k TEXT PRIMARY KEY, minutes REAL);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, started TEXT, finished TEXT, stats TEXT
);
"""

UPDATABLE = ("url", "title", "price", "size", "plot", "rooms", "condition", "address",
             "agency", "description")


# Columns added after the first release; added to older databases on start.
MIGRATIONS = {"pt_day": "INTEGER", "night_worst": "INTEGER", "night_typ": "INTEGER", "walk_stop": "INTEGER",
              "stop_name": "TEXT", "car_bp": "INTEGER", "bp_approx": "INTEGER"}


def connect(path):
    con = sqlite3.connect(path, timeout=120)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    have = {r["name"] for r in con.execute("PRAGMA table_info(listings)")}
    for col, typ in MIGRATIONS.items():
        if col not in have:
            con.execute(f"ALTER TABLE listings ADD COLUMN {col} {typ}")
    return con


def today():
    return date.today().isoformat()


def upsert(con, item: dict, run_day: str):
    """Insert a new listing or refresh an existing one. Returns 'new', 'price', or 'seen'."""
    uid = f"{item['source']}:{item['source_id']}"
    row = con.execute("SELECT * FROM listings WHERE uid=?", (uid,)).fetchone()
    if row is None:
        cols = ["uid", "source", "source_id", "town", "first_seen", "last_seen", "active",
                "price_history"] + [c for c in UPDATABLE if item.get(c) is not None]
        vals = [uid, item["source"], str(item["source_id"]), item["town"], run_day, run_day, 1,
                json.dumps([[run_day, item.get("price")]])] + [item[c] for c in cols[8:]]
        for k in ("lat", "lon", "loc_quality", "loc_note"):
            if item.get(k) is not None:
                cols.append(k)
                vals.append(item[k])
        con.execute(f"INSERT INTO listings ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
        return "new"
    status = "seen"
    hist = json.loads(row["price_history"] or "[]")
    if item.get("price") and row["price"] and item["price"] != row["price"]:
        hist.append([run_day, item["price"]])
        status = "price"
    sets = {c: item[c] for c in UPDATABLE if item.get(c) not in (None, "")}
    # Only take coordinates from the list page if we have nothing better yet.
    if item.get("lat") is not None and row["loc_quality"] in (None, "none", "town", "area"):
        sets.update(lat=item["lat"], lon=item["lon"], loc_quality=item.get("loc_quality"),
                    loc_note=item.get("loc_note"))
    sets.update(last_seen=run_day, active=1, removed_on=None, price_history=json.dumps(hist))
    con.execute(f"UPDATE listings SET {','.join(k + '=?' for k in sets)} WHERE uid=?",
                [*sets.values(), uid])
    return status


def mark_removed(con, source, town, run_day):
    """Listings of a source+town not seen in this (successful) scrape are gone."""
    cur = con.execute(
        "UPDATE listings SET active=0, removed_on=? WHERE source=? AND town=? AND active=1 AND last_seen<?",
        (run_day, source, town, run_day))
    return cur.rowcount


def log_run(con, started, stats):
    con.execute("INSERT INTO runs (started, finished, stats) VALUES (?,?,?)",
                (started, datetime.now().isoformat(timespec="seconds"), json.dumps(stats, ensure_ascii=False)))
    con.commit()
