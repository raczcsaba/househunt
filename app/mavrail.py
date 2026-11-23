"""MÁV trains for the transit grid, from the MÁV journey planner (used when no MÁV GTFS is configured).

For every rail station within the radius: direct trains to Budapest on a weekday morning and direct
trains from Budapest on Saturday night. Each becomes one direct connection between the Budapest
terminal and the station; stations are placed on the map by matching their names to bus/tram stops.
"""
import json
import os
import re
import uuid
from datetime import date, datetime, time as dtime, timedelta

from .common import haversine_km, http, log
from .transitgrid import CENTRE, RADIUS_KM

API = "https://jegy-a.mav.hu/IK_API_PROD/api/OfferRequestApi/GetOfferRequest"
STATIONS = "https://jegy-a.mav.hu/IK_API_PROD/api/OfferRequestApi/GetStationList"
BUDAPEST = "005510009"
HDRS = {"Language": "hu", "UserSessionId": str(uuid.uuid4())}


def _norm(s):
    s = s.lower()
    s = re.sub(r"^budapest[- ]", "", s)
    s = re.sub(r"\b(vasútállomás|vasúti megállóhely|megállóhely|pályaudvar|mh\.?|m)\b", " ", s)
    s = re.sub(r"\(.*?\)", " ", s)
    return re.sub(r"[\s,.]+", " ", s).strip()


def _utc(dt: datetime):
    from .transit import _utc_offset
    return (dt - timedelta(hours=_utc_offset(dt))).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _offers(frm, to, dt):
    body = {"offerkind": "1", "startStationCode": frm, "innerStationsCodes": [], "endStationCode": to,
            "passangers": [{"passengerCount": 1, "passengerId": 0, "customerTypeKey": "HU_44_025-065",
                            "customerDiscountsKeys": []}],
            "isOneWayTicket": True, "isTravelEndTime": False, "isSupplementaryTicketsOnly": False,
            "travelStartDate": _utc(dt), "travelReturnDate": _utc(dt), "selectedServices": [52],
            "selectedSearchServices": ["BOOK_INTEGRATED"], "eszkozSzamok": [], "isOfDetailedSearch": False}
    out = []
    for x in (http(API, data=body, headers=HDRS, as_json=True) or {}).get("route") or []:
        legs = (x.get("details") or {}).get("routes") or []
        if x.get("transfersCount") or len(legs) != 1:
            continue
        out.append((datetime.fromisoformat(x["departure"]["time"][:19]),
                    datetime.fromisoformat(x["arrival"]["time"][:19]),
                    legs[0]["startStation"]["name"], legs[0]["destionationStation"]["name"]))
    return out


def _station_list(cache):
    if os.path.exists(cache) and (datetime.now().timestamp() - os.path.getmtime(cache)) < 30 * 86400:
        return json.load(open(cache))
    d = http(STATIONS, data={}, headers=HDRS, as_json=True, timeout=180)
    rail = sorted({s["name"]: s["code"] for s in d["stations"]
                   if not s["isAlias"] and s.get("coutryIso") == "HU"
                   and any(m.get("code") == 100 for m in s.get("modalities") or [])}.items())
    json.dump(rail, open(cache, "w"), ensure_ascii=False)
    return rail


def _nominatim(name, cache):
    """Railway station coordinates from OpenStreetMap, limited to the Budapest area. Cached for good."""
    if cache.get(name):
        return cache[name]       # misses are retried on the next run
    import urllib.parse
    d = RADIUS_KM / 111
    box = f"{CENTRE['lon'] - d * 1.5},{CENTRE['lat'] + d},{CENTRE['lon'] + d * 1.5},{CENTRE['lat'] - d}"
    hit = None
    for q in (f"{name} vasútállomás", f"{name} megállóhely", name):
        url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
            {"q": q, "format": "jsonv2", "countrycodes": "hu", "limit": 5, "viewbox": box, "bounded": 1})
        try:
            res = http(url, headers={"User-Agent": "househunt-personal/1.0"}, as_json=True)
        except Exception:
            res = []
        rail = [r for r in res or [] if r.get("category") == "railway" and r.get("type") in ("station", "halt", "stop")]
        # Some stations exist in OSM only as the bus stop in front of them ("Vecsés, vasútállomás").
        stop = [r for r in res or [] if r.get("type") in ("bus_stop", "platform")
                and re.search(r"vasútállomás|megállóhely", r.get("display_name", "").split(",")[0] +
                              "," + r.get("display_name", "").split(",")[1] if "," in r.get("display_name", "") else "", re.I)]
        if rail or stop:
            r = (rail or stop)[0]
            hit = [float(r["lat"]), float(r["lon"])]
            break
    cache[name] = hit
    return hit


def place_stations(net, cache_dir):
    """MÁV station name -> (lat, lon, name, code) for stations within the radius."""
    idx, towns = {}, set()
    for la, lo, nm in zip(net.lat, net.lon, net.name):
        if re.search(r"vasútállomás|megállóhely|pályaudvar|kispest", nm, re.I):
            idx.setdefault(_norm(nm), (la, lo, nm))
        if "," in nm:
            towns.add(nm.split(",")[0].strip())
    cache_path = os.path.join(cache_dir, "rail_station_coords.json")
    try:
        cache = json.load(open(cache_path))
    except (OSError, ValueError):
        cache = {}
    where = {}
    for name, code in _station_list(os.path.join(cache_dir, "mav_stations.json")):
        p = idx.get(_norm(name))
        if p is None and (name.startswith("Budapest") or any(name == t or name.startswith(t + " ") or name.startswith(t + "-")
                                                             for t in towns)):
            c = _nominatim(name, cache)
            p = (c[0], c[1], name) if c else None
        if p and haversine_km(p[0], p[1], CENTRE["lat"], CENTRE["lon"]) <= RADIUS_KM:
            where[name] = (p[0], p[1], name, code)
    json.dump(cache, open(cache_path, "w"), ensure_ascii=False)
    return where


def rail_items(weekday: date, saturday: date, net, cache_dir="/data"):
    where = place_stations(net, cache_dir)
    log.info("rail: %d MÁV stations placed within %d km", len(where), RADIUS_KM)
    day0 = datetime.combine(weekday, dtime())
    sat0 = datetime.combine(saturday, dtime())
    items, failed = [], 0
    for name, (la, lo, _, code) in where.items():
        if name.startswith("Budapest-"):
            continue
        try:
            for h in (5, 7):            # towards Budapest, weekday morning
                for dep, arr, a, b in _offers(code, BUDAPEST, day0 + timedelta(hours=h)):
                    if b in where and arr <= day0 + timedelta(hours=9, minutes=30):
                        items.append((int((dep - day0).total_seconds()), int((arr - day0).total_seconds()),
                                      where[name][:3], where[b][:3]))
            t = sat0 + timedelta(hours=22, minutes=30)   # from Budapest, Saturday night
            for _ in range(4):
                res = _offers(BUDAPEST, code, t)
                for dep, arr, a, b in res:
                    if a in where:
                        items.append((int((dep - sat0).total_seconds()), int((arr - sat0).total_seconds()),
                                      where[a][:3], where[name][:3]))
                if not res or max(r[0] for r in res) >= sat0 + timedelta(hours=29, minutes=30):
                    break
                t = max(r[0] for r in res) + timedelta(minutes=1)
        except Exception as e:
            failed += 1
            log.warning("rail %s: %s", name, e)
    log.info("rail: %d connections, %d stations failed", len(items), failed)
    return items
