"""Find where a listing is, and how far it is from the station.

Order of trust: exact map pin > street/landmark geocoded (from the address, or from the ad text via
Claude) > the portal's blurred area pin > shared town-centre pin > nothing.
Claude is optional: set ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN) to enable it.
"""
import json
import os
import re
import urllib.parse

from .common import FetchError, bike_minutes, haversine_km, http, log

NOMINATIM = (os.environ.get("NOMINATIM_URL") or "https://nominatim.openstreetmap.org")
CONTACT = (os.environ.get("CONTACT_EMAIL") or "")
GEO_UA = {"User-Agent": f"househunt-personal/1.0 ({CONTACT or 'personal use'})"}
MAX_TOWN_KM = 9.0   # a geocode further than this from the town's station is a wrong match

RANK = {"exact": 5, "street": 4, "claude": 4, "area": 3, "town": 1, "none": 0, None: 0}


# ---------------------------------------------------------------- geocoding (Nominatim, cached)
def geocode(con, query: str, town: dict):
    key = query.lower().strip()
    row = con.execute("SELECT lat, lon FROM geocache WHERE q=?", (key,)).fetchone()
    if row is not None:
        return (row["lat"], row["lon"]) if row["lat"] is not None else None
    params = urllib.parse.urlencode({"q": query, "format": "jsonv2", "countrycodes": "hu", "limit": 3,
                                     **({"email": CONTACT} if CONTACT else {})})
    try:
        res = http(f"{NOMINATIM}/search?{params}", headers=GEO_UA, as_json=True)
    except FetchError as e:
        log.warning("geocode failed %s: %s", query, e)
        return None
    st = (town.get("stations") or [town["centre"]])[0]
    best = None
    for r in res or []:
        lat, lon = float(r["lat"]), float(r["lon"])
        if haversine_km(lat, lon, st[0], st[1]) <= MAX_TOWN_KM:
            best = (lat, lon)
            break
    con.execute("INSERT OR REPLACE INTO geocache VALUES (?,?,?,?)",
                (key, best[0] if best else None, best[1] if best else None, query))
    return best


STREET_TYPES = r"utca|útja|út|u\.|körút|tér|köz|sor|fasor|dűlő|sétány|lakópark|park|liget"
_CAP = r"[A-ZÁÉÍÓÖŐÚÜŰ][\w.\-]*"
# A street name is one to four capitalised words (or numbers, "Május 1. utca") before the street type,
# so "Eladó ház a Kossuth Lajos utca 12" gives "Kossuth Lajos utca 12". A sentence-initial article is skipped.
STREET_RE = re.compile(rf"\b(?!(?:A|Az|Egy)\s)({_CAP}(?:\s+(?:{_CAP}|\d+\.?)){{0,3}}\s+(?:{STREET_TYPES}))"
                       r"(?!\w)(\s*\d+)?", re.U)


def street_from_text(*texts):
    for t in texts:
        if not t:
            continue
        m = STREET_RE.search(t)
        if m:
            street = re.sub(r"\bu\.$", "utca", m.group(1))
            return (street + (m.group(2) or "")).strip()
    return None


# ---------------------------------------------------------------- Claude extraction (optional)
SCHEMA = {
    "type": "object",
    "properties": {
        "street": {"type": ["string", "null"],
                   "description": "Street name with type, e.g. 'Kossuth Lajos utca'. Null if not stated."},
        "house_number": {"type": ["string", "null"]},
        "landmarks": {"type": "array", "items": {"type": "string"},
                      "description": "Named places the ad says are close by (school, Tesco, strand, templom...)."},
        "neighbourhood": {"type": ["string", "null"],
                          "description": "Named part of the town (városrész), e.g. 'Újváros'."},
        "station_mention": {"type": "string", "enum": ["near", "walking", "far", "none"],
                            "description": "What the ad says about the railway/HÉV/tram-train station: near "
                                           "(a few minutes), walking (stated walking distance), far, or none."},
        "station_minutes": {"type": ["integer", "null"],
                            "description": "Minutes to the station if the ad states it (any mode)."},
        "condition": {"type": "string", "enum": ["new", "renovated", "good", "average", "needs_renovation", "unknown"]},
    },
    "required": ["street", "house_number", "landmarks", "neighbourhood", "station_mention",
                 "station_minutes", "condition"],
    "additionalProperties": False,
}

PROMPT = """You read Hungarian house-for-sale ads and pull out where the house is.
Town: {town}. Only report what the ad states; do not guess a street that is not written.

Title: {title}
Address field: {address}
Ad text:
{desc}"""

_client = None


def claude_enabled():
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")) \
        and (os.environ.get("CLAUDE_ENABLED") or "1") != "0"


def claude_extract(row):
    global _client
    import anthropic
    if _client is None:
        _client = anthropic.Anthropic(max_retries=3)
    model = (os.environ.get("CLAUDE_MODEL") or "claude-opus-5-5")
    kwargs = dict(
        model=model, max_tokens=2000,
        output_config={"effort": (os.environ.get("CLAUDE_EFFORT") or "low"),
                       "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": PROMPT.format(
            town=row["town"], title=row["title"] or "", address=row["address"] or "",
            desc=(row["description"] or "")[:6000])}],
    )
    if model.startswith(("claude-opus", "claude-fable")):
        resp = _client.beta.messages.create(**kwargs, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    else:
        resp = _client.messages.create(**kwargs)
    if resp.stop_reason == "refusal":
        return None
    text = next((b.text for b in resp.content if b.type == "text"), None)
    return json.loads(text) if text else None


# ---------------------------------------------------------------- per-listing pass
def _place(town_name):
    """'Budapest XI.' -> 'XI. kerület, Budapest' so the geocoder finds the right district."""
    if town_name.startswith("Budapest "):
        return f"{town_name.split(' ', 1)[1]} kerület, Budapest"
    return town_name


def nearest_station(lat, lon, town):
    return min(haversine_km(lat, lon, s[0], s[1]) for s in town["stations"])


def locate_pending(con, towns: dict, claude_budget: int):
    """Improve location for active listings without an exact pin. Returns counters."""
    stats = {"geocoded": 0, "claude": 0, "claude_errors": 0}
    rows = con.execute("""SELECT * FROM listings WHERE active=1 AND enrich_done=0
                          AND (loc_quality IS NULL OR loc_quality NOT IN ('exact'))""").fetchall()
    use_claude = claude_enabled()
    for row in rows:
        town = towns.get(row["town"])
        if not town:
            continue
        upd = {"enrich_done": 1}
        best_q = row["loc_quality"]
        # 1) a street written in the address/title/description
        street = street_from_text(row["address"], row["title"], row["description"])
        if street:
            p = geocode(con, f"{street}, {_place(row['town'])}", town)
            if p and RANK["street"] > RANK.get(best_q, 0):
                upd.update(lat=p[0], lon=p[1], loc_quality="street", loc_note=f"geocoded: {street}")
                best_q = "street"
                stats["geocoded"] += 1
        # 2) Claude reads the ad when we still have no street-level location
        if use_claude and RANK.get(best_q, 0) < RANK["street"] and stats["claude"] < claude_budget \
                and (row["description"] or "").strip():
            try:
                info = claude_extract(row)
                stats["claude"] += 1
            except Exception as e:  # keep the run going; retry next time
                log.warning("claude failed for %s: %s", row["uid"], e)
                stats["claude_errors"] += 1
                upd["enrich_done"] = 0
                info = None
            if info:
                if info["station_mention"] in ("near", "walking") or (info["station_minutes"] or 99) <= 10:
                    upd["near_station_text"] = 1
                if info["condition"] == "needs_renovation" and not row["condition"]:
                    upd["condition"] = "Felújítandó"
                for q in ([f"{info['street']} {info['house_number'] or ''}".strip()] if info["street"] else []) \
                        + info["landmarks"][:2] + ([info["neighbourhood"]] if info["neighbourhood"] else []):
                    p = geocode(con, f"{q}, {_place(row['town'])}", town)
                    if p:
                        upd.update(lat=p[0], lon=p[1], loc_quality="claude", loc_note=f"from ad text: {q}")
                        break
        con.execute(f"UPDATE listings SET {','.join(k + '=?' for k in upd)} WHERE uid=?", [*upd.values(), row["uid"]])
    con.commit()
    return stats


def compute_distances(con, towns: dict):
    for row in con.execute("SELECT uid, town, lat, lon FROM listings WHERE lat IS NOT NULL").fetchall():
        town = towns.get(row["town"])
        if not town or not town.get("stations"):
            continue
        km = nearest_station(float(row["lat"]), float(row["lon"]), town)
        con.execute("UPDATE listings SET dist_km=?, bike_min=? WHERE uid=?",
                    (round(km, 2), bike_minutes(km), row["uid"]))
    con.commit()
