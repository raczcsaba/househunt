"""Per-house travel times to the Budapest centre (Blaha Lujza tér) for Budapest and agglomeration ads.

Public transport comes from the timetable grid (transitgrid.py). Driving time comes from OSRM
(free-flow, no traffic), cached per ~100 m cell. Houses without a usable location use their
district's or town's centre and are flagged as approximate.
"""
import json
import os
import urllib.request

from .common import log
from .transitgrid import CENTRE, Grid

OSRM = os.environ.get("OSRM_URL") or "https://router.project-osrm.org"
GOOD = ("exact", "street", "claude", "area")


def _car_minutes(con, points):
    """points: {key: (lat, lon)} -> {key: minutes}. Cached in the carcache table."""
    out, todo = {}, []
    for k, (la, lo) in points.items():
        r = con.execute("SELECT minutes FROM carcache WHERE k=?", (k,)).fetchone()
        if r is not None:
            out[k] = r["minutes"]
        else:
            todo.append((k, la, lo))
    for i in range(0, len(todo), 80):
        chunk = todo[i:i + 80]
        coords = ";".join([f"{CENTRE['lon']},{CENTRE['lat']}"] + [f"{lo:.5f},{la:.5f}" for _, la, lo in chunk])
        url = f"{OSRM}/table/v1/driving/{coords}?sources={';'.join(str(j + 1) for j in range(len(chunk)))}&destinations=0"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "househunt-personal/1.0"})
            d = json.load(urllib.request.urlopen(req, timeout=60))
        except Exception as e:
            log.warning("OSRM failed: %s", e)
            break
        for (k, _, _), row in zip(chunk, d.get("durations") or []):
            if row and row[0] is not None:
                out[k] = round(row[0] / 60, 1)
                con.execute("INSERT OR REPLACE INTO carcache VALUES (?,?)", (k, out[k]))
        con.commit()
    return out


def update(con, towns: dict, grid_path):
    if not os.path.exists(grid_path):
        log.info("no transit grid yet; Budapest travel times will appear after the 'grid' job")
        return {"skipped": "no grid"}
    grid = Grid(grid_path)
    bp_towns = {n for n, t in towns.items() if t.get("bp")}
    rows = con.execute("SELECT uid, town, lat, lon, loc_quality FROM listings WHERE active=1").fetchall()
    points, todo = {}, []
    for r in rows:
        if r["town"] not in bp_towns:
            continue
        t = towns[r["town"]]
        if r["lat"] is not None and r["loc_quality"] in GOOD:
            la, lo, approx = float(r["lat"]), float(r["lon"]), 0
        else:
            la, lo = t["bp_centre"]
            approx = 1
        key = f"{la:.3f},{lo:.3f}"
        points[key] = (la, lo)
        todo.append((r["uid"], la, lo, approx, key))
    car = _car_minutes(con, points)
    n = 0
    for uid, la, lo, approx, key in todo:
        q = grid.query(la, lo) or {}
        con.execute("""UPDATE listings SET pt_day=?, night_worst=?, night_typ=?, walk_stop=?, stop_name=?,
                       car_bp=?, bp_approx=? WHERE uid=?""",
                    (q.get("pt_day"), q.get("night_worst"), q.get("night_typ"), q.get("walk_stop"), q.get("stop"),
                     round(car[key]) if key in car else None, approx, uid))
        n += 1
    con.commit()
    log.info("Budapest travel times updated for %d ads", n)
    return {"updated": n, "grid_built": grid.meta.get("built")}
