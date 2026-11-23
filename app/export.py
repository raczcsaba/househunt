"""Merge duplicates across portals and write data.json for the dashboard."""
import json
import os
from datetime import date, timedelta

from .locate import RANK

KEEP_REMOVED_DAYS = 30


def _same(a, b):
    if a["town"] != b["town"] or not a["price"] or not b["price"]:
        return False
    if abs(a["price"] - b["price"]) > 0.015 * max(a["price"], b["price"]):
        return False
    if not a["size"] or not b["size"] or abs(a["size"] - b["size"]) > 3:
        return False
    if a["rooms"] and b["rooms"] and int(a["rooms"]) != int(b["rooms"]):
        return False
    if a["plot"] and b["plot"] and abs(a["plot"] - b["plot"]) > 0.05 * max(a["plot"], b["plot"]):
        return False
    return True


def cluster(con):
    """Union-find over listings that look like the same house on different portals."""
    rows = [dict(r) for r in con.execute(
        "SELECT uid, source, town, price, size, rooms, plot FROM listings WHERE active=1 OR removed_on IS NOT NULL")]
    parent = {r["uid"]: r["uid"] for r in rows}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    by_town = {}
    for r in rows:
        by_town.setdefault(r["town"], []).append(r)
    for lst in by_town.values():
        lst.sort(key=lambda r: r["price"] or 0)
        for i, a in enumerate(lst):
            for b in lst[i + 1:]:
                if (b["price"] or 0) > (a["price"] or 0) * 1.016:
                    break
                if a["source"] != b["source"] and _same(a, b):
                    ra, rb = find(a["uid"]), find(b["uid"])
                    if ra != rb:
                        parent[max(ra, rb)] = min(ra, rb)
    for r in rows:
        con.execute("UPDATE listings SET cluster=? WHERE uid=?", (find(r["uid"]), r["uid"]))
    con.commit()


def export(con, towns_cfg, out_path, run_info):
    cutoff = (date.today() - timedelta(days=KEEP_REMOVED_DAYS)).isoformat()
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM listings WHERE active=1 OR removed_on>=? ORDER BY cluster", (cutoff,))]
    groups = {}
    for r in rows:
        groups.setdefault(r["cluster"] or r["uid"], []).append(r)
    out = []
    for cid, g in groups.items():
        active = [r for r in g if r["active"]] or g
        best_loc = max(g, key=lambda r: (RANK.get(r["loc_quality"], 0), -(r["dist_km"] or 99)))
        main = max(active, key=lambda r: (len(r["description"] or ""), r["source"]))
        if not main["price"] or not (3_000_000 <= main["price"] <= 5_000_000_000):
            continue   # rentals, auctions and typos ("0,1 M Ft") are not houses for sale
        bm = best_loc if best_loc["pt_day"] is not None else next((r for r in g if r["pt_day"] is not None), best_loc)
        hist = json.loads(main["price_history"] or "[]")
        first_price = hist[0][1] if hist else main["price"]
        out.append(dict(
            id=cid, town=main["town"], title=main["title"], price=main["price"], size=main["size"],
            plot=max((r["plot"] or 0 for r in g), default=0) or None, rooms=main["rooms"],
            cond=next((r["condition"] for r in g if r["condition"]), None),
            dist=best_loc["dist_km"], bike=best_loc["bike_min"], locq=best_loc["loc_quality"] or "none",
            locnote=best_loc["loc_note"], near_text=any(r["near_station_text"] for r in g),
            first_seen=min(r["first_seen"] for r in g), last_seen=max(r["last_seen"] for r in g),
            active=any(r["active"] for r in g), removed_on=None if any(r["active"] for r in g) else max(r["removed_on"] or "" for r in g),
            price_drop=(first_price - main["price"]) if first_price and main["price"] and main["price"] < first_price else 0,
            hist=hist, agency=next((r["agency"] for r in g if r["agency"]), None),
            links=sorted([{"s": r["source"], "u": r["url"], "a": bool(r["active"])} for r in g], key=lambda x: x["s"]),
            # Budapest-area travel times (only for Budapest districts and agglomeration towns)
            pt=bm["pt_day"], nw=bm["night_worst"], nt=bm["night_typ"],
            walk=bm["walk_stop"], stop=bm["stop_name"], car_bp=bm["car_bp"], bpx=bool(bm["bp_approx"]),
        ))
    grid_meta = None
    grid_path = os.path.join(os.path.dirname(os.path.dirname(out_path)), "transit_grid.json")
    if os.path.exists(grid_path):
        with open(grid_path) as f:
            head = json.load(f)
        grid_meta = {k: v for k, v in head.items() if k != "stops"}
    data = dict(generated=run_info.get("finished"), run=run_info,
                transit_checked=towns_cfg.get("transit_checked"), grid=grid_meta,
                bp_centre=towns_cfg.get("bp_centre"),
                towns=[{k: v for k, v in t.items() if k not in ("stations",)} for t in towns_cfg["towns"]],
                listings=out)
    with open(out_path, "w") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    return len(out)
