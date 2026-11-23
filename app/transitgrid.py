"""Door-to-centre public transport times for any address in Budapest and its agglomeration.

Built from timetables:
  * BKK GTFS (Budapest buses, trams, metro, HÉV, night buses)   – public
  * Volánbusz GTFS (agglomeration buses)                          – public
  * MÁV trains: from the official MÁV GTFS if you have it (MAV_GTFS_URL), otherwise from the
    MÁV journey planner, station by station (see mavrail.py).

Two measures per transit stop, computed with the Connection Scan Algorithm:
  * day:   minutes from the stop to the centre on a weekday morning (median over arrival deadlines
           07:30–08:30, so the slack of a badly timed last connection doesn't dominate)
  * night: minutes from the centre home when leaving at 00:00, 00:30, … 04:00 on a Saturday night
           (includes waiting for the next night bus/train, so a 2-hour gap shows as a long trip)

A house's time = walk to the best nearby stop + that stop's time.
"""
import csv
import io
import json
import math
import os
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta

from .common import haversine_km, http, log

CENTRE = {"name": "Blaha Lujza tér (Erzsébet körút)", "lat": 47.4967, "lon": 19.0704}
RADIUS_KM = float(os.environ.get("BP_RADIUS_KM") or 45)
FEEDS = {
    "bkk": os.environ.get("BKK_GTFS_URL") or "https://go.bkk.hu/api/static/v1/public-gtfs/budapest_gtfs.zip",
    "volan": os.environ.get("VOLAN_GTFS_URL") or "https://gtfs.kti.hu/public-gtfs/volanbusz_gtfs.zip",
}
if os.environ.get("MAV_GTFS_URL"):
    FEEDS["mav"] = os.environ["MAV_GTFS_URL"]

# Weekday: arrive at the centre by 07:30, 07:40, … 08:30; the median trip length is the "typical" time.
ARRIVE_BYS = [7 * 3600 + 30 * 60 + m * 60 for m in range(0, 61, 10)]
NIGHT_DEPS = [24 * 3600 + m * 60 for m in range(0, 241, 30)]   # Sat night 00:00 … 04:00 (as 24:00+)
WALK_KMH = 4.8
WALK_DETOUR = 1.25
TRANSFER_M = 350           # walk between stops when changing
ACCESS_M = 1200            # walk from a house to a stop
INF = 10 ** 9


def walk_sec(km):
    return km * WALK_DETOUR / WALK_KMH * 3600


# ------------------------------------------------------------------ feed loading
def download(feed_dir):
    os.makedirs(feed_dir, exist_ok=True)
    import urllib.request
    for name, url in FEEDS.items():
        path = os.path.join(feed_dir, f"{name}.zip")
        log.info("downloading %s GTFS", name)
        tmp = path + ".part"
        req = urllib.request.Request(url, headers={"User-Agent": "househunt-personal/1.0"})
        with urllib.request.urlopen(req, timeout=900) as r, open(tmp, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        os.replace(tmp, path)


def _rows(zf, name):
    if name not in zf.namelist():
        return
    with zf.open(name) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))


def _active_services(zf, day: date):
    ymd, wd = day.strftime("%Y%m%d"), day.weekday()
    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    active = set()
    for r in _rows(zf, "calendar.txt") or []:
        if r["start_date"] <= ymd <= r["end_date"] and r[days[wd]] == "1":
            active.add(r["service_id"])
    for r in _rows(zf, "calendar_dates.txt") or []:
        if r["date"] == ymd:
            (active.add if r["exception_type"] == "1" else active.discard)(r["service_id"])
    return active


def _t(s):
    h, m, sec = s.split(":")
    return int(h) * 3600 + int(m) * 60 + int(sec)


class Network:
    def __init__(self):
        self.stop_idx, self.lat, self.lon, self.name = {}, [], [], []
        self.conns = []          # (dep_t, arr_t, dep_stop, arr_stop, trip)
        self.ntrips = 0

    def stop(self, key, lat, lon, name):
        i = self.stop_idx.get(key)
        if i is None:
            i = self.stop_idx[key] = len(self.lat)
            self.lat.append(lat), self.lon.append(lon), self.name.append(name)
        return i

    def add_feed(self, path, prefix, windows):
        """windows: list of (service_date, t_from, t_to, offset) – keep connections departing in
        [t_from, t_to] of that service day; offset shifts them onto a shared timeline."""
        zf = zipfile.ZipFile(path)
        keep = {}
        for r in _rows(zf, "stops.txt"):
            try:
                la, lo = float(r["stop_lat"]), float(r["stop_lon"])
            except (TypeError, ValueError):
                continue
            if haversine_km(la, lo, CENTRE["lat"], CENTRE["lon"]) <= RADIUS_KM:
                keep[r["stop_id"]] = (la, lo, r["stop_name"])
        trips = defaultdict(list)    # trip_id -> [(window index, offset)]
        services = [(_active_services(zf, d), a, b, off) for d, a, b, off in windows]
        for r in _rows(zf, "trips.txt"):
            for wi, (act, a, b, off) in enumerate(services):
                if r["service_id"] in act:
                    trips[r["trip_id"]].append(wi)
        n0 = len(self.conns)
        cur, prev = None, None
        trip_ids = {}
        for r in _rows(zf, "stop_times.txt"):
            tid = r["trip_id"]
            if tid not in trips:
                continue
            if tid != cur:
                cur, prev = tid, None
            sid = r["stop_id"]
            if sid not in keep:
                continue
            arr = _t(r["arrival_time"] or r["departure_time"])
            dep = _t(r["departure_time"] or r["arrival_time"])
            if prev is not None:
                psid, pdep = prev
                for wi in trips[tid]:
                    _, a, b, off = services[wi]
                    if a <= pdep <= b:
                        key = (tid, wi)
                        tr = trip_ids.get(key)
                        if tr is None:
                            tr = trip_ids[key] = self.ntrips
                            self.ntrips += 1
                        ps = self.stop(f"{prefix}:{psid}", *keep[psid])
                        cs = self.stop(f"{prefix}:{sid}", *keep[sid])
                        self.conns.append((pdep + off, arr + off, ps, cs, tr))
            prev = (sid, dep)
        log.info("%s: %d connections, %d stops so far", prefix, len(self.conns) - n0, len(self.lat))

    def add_direct(self, items):
        """Synthetic connections: (dep_t, arr_t, (lat, lon, name), (lat, lon, name))."""
        for dep, arr, a, b in items:
            sa = self.stop(f"rail:{a[2]}", *a)
            sb = self.stop(f"rail:{b[2]}", *b)
            self.conns.append((dep, arr, sa, sb, self.ntrips))
            self.ntrips += 1

    def footpaths(self):
        """Walking links between stops closer than TRANSFER_M (grid bucketing)."""
        cell = 0.004
        grid = defaultdict(list)
        for i, (la, lo) in enumerate(zip(self.lat, self.lon)):
            grid[(int(la / cell), int(lo / cell))].append(i)
        foot = [[] for _ in self.lat]
        for (gx, gy), members in grid.items():
            near = [j for dx in (-1, 0, 1) for dy in (-1, 0, 1) for j in grid.get((gx + dx, gy + dy), ())]
            for i in members:
                for j in near:
                    if i != j:
                        km = haversine_km(self.lat[i], self.lon[i], self.lat[j], self.lon[j])
                        if km * 1000 <= TRANSFER_M:
                            foot[i].append((j, int(walk_sec(km)) + 60))
        self.foot = foot

    def centre_stops(self):
        out = []
        for i, (la, lo) in enumerate(zip(self.lat, self.lon)):
            km = haversine_km(la, lo, CENTRE["lat"], CENTRE["lon"])
            if km <= 0.45:
                out.append((i, int(walk_sec(km))))
        return out


# ------------------------------------------------------------------ Connection Scan Algorithm
def earliest_arrival(net, sources, t0, horizon=4 * 3600):
    """Forward CSA. sources: [(stop, walk_sec)]. Returns arrival time per stop."""
    S = [INF] * len(net.lat)
    reached = bytearray(net.ntrips)
    for s, w in sources:
        S[s] = min(S[s], t0 + w)
    for s, w in sources:
        for j, fw in net.foot[s]:
            S[j] = min(S[j], S[s] + fw)
    conns, foot, end = net.conns_dep, net.foot, t0 + horizon
    import bisect
    i = bisect.bisect_left(net.dep_keys, t0)
    n = len(conns)
    while i < n:
        dep, arr, a, b, tr = conns[i]
        i += 1
        if dep > end:
            break
        if reached[tr] or S[a] <= dep:
            reached[tr] = 1
            if arr < S[b]:
                S[b] = arr
                for j, fw in foot[b]:
                    if arr + fw < S[j]:
                        S[j] = arr + fw
    return S


def latest_departure(net, targets, t_arr, horizon=3 * 3600):
    """Reverse CSA. targets: [(stop, walk_sec)]. Returns the latest departure per stop to arrive by t_arr."""
    L = [-INF] * len(net.lat)
    ok = bytearray(net.ntrips)
    for s, w in targets:
        L[s] = max(L[s], t_arr - w)
    for s, w in targets:
        for j, fw in net.foot[s]:
            L[j] = max(L[j], L[s] - fw)
    conns, foot, start = net.conns_rev, net.foot, t_arr - horizon
    for dep, arr, a, b, tr in conns:
        if dep > t_arr:
            continue
        if dep < start:
            break
        if ok[tr] or arr <= L[b]:
            ok[tr] = 1
            if dep > L[a]:
                L[a] = dep
                for j, fw in foot[a]:
                    if dep - fw > L[j]:
                        L[j] = dep - fw
    return L


# ------------------------------------------------------------------ build + query
def _next(wd, after: date):
    d = after + timedelta(days=1)
    while d.weekday() != wd:
        d += timedelta(days=1)
    return d


def build(feed_dir, out_path, rail_items_fn=None):
    """Compute per-stop times and save them to out_path (JSON)."""
    today = date.today()
    tue, sat = _next(1, today), _next(5, today)
    sun = sat + timedelta(days=1)
    windows = [(tue, 5 * 3600, 9 * 3600, 0),                       # weekday morning
               (sat, 23 * 3600, 30 * 3600, 0),                     # Sat service running past midnight
               (sun, 0, 6 * 3600, 24 * 3600)]                      # Sun service after midnight, shifted
    net = Network()
    for name in FEEDS:
        path = os.path.join(feed_dir, f"{name}.zip")
        if os.path.exists(path):
            net.add_feed(path, name, windows)
    if "mav" not in FEEDS and rail_items_fn:
        items = rail_items_fn(tue, sat, net)
        net.add_direct(items)
        log.info("rail (MÁV planner): %d direct connections", len(items))
    net.footpaths()
    net.conns_dep = sorted(net.conns)
    net.dep_keys = [c[0] for c in net.conns_dep]
    net.conns_rev = sorted(net.conns, key=lambda c: -c[0])
    centre = net.centre_stops()
    # The two windows share one timeline: weekday trips sit at 5–9 h, Saturday-night trips at 23–30 h,
    # so a scan in one window never meets the other's connections within its horizon.
    per_deadline = []
    for t_arr in ARRIVE_BYS:
        L = latest_departure(net, centre, t_arr)
        per_deadline.append([(t_arr - l) / 60 if l > -INF else None for l in L])
    day = []
    for i in range(len(net.lat)):
        v = sorted(x[i] for x in per_deadline if x[i] is not None)
        day.append(round(v[len(v) // 2]) if len(v) >= len(ARRIVE_BYS) // 2 + 1 else None)
    night = []
    for t0 in NIGHT_DEPS:
        S = earliest_arrival(net, centre, t0, horizon=5 * 3600)
        night.append([round((s - t0) / 60) if s < INF else None for s in S])
    keep = [i for i in range(len(net.lat)) if day[i] is not None or any(n[i] is not None for n in night)]
    data = dict(built=datetime.now().isoformat(timespec="seconds"), centre=CENTRE,
                dates={"weekday": tue.isoformat(), "saturday_night": sat.isoformat()},
                feeds=sorted(FEEDS) + ([] if "mav" in FEEDS else ["mav-planner"]),
                night_deps=["%02d:%02d" % ((t // 3600) % 24, (t % 3600) // 60) for t in NIGHT_DEPS],
                stops=[[round(net.lat[i], 6), round(net.lon[i], 6), net.name[i], day[i], [n[i] for n in night]]
                       for i in keep])
    with open(out_path, "w") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    log.info("transit grid: %d stops with times", len(keep))
    return data


class Grid:
    """Answers 'how long from this house to the centre' using the saved per-stop times."""

    def __init__(self, path):
        d = json.load(open(path))
        self.meta = {k: v for k, v in d.items() if k != "stops"}
        self.cell = 0.01
        self.buckets = defaultdict(list)
        for s in d["stops"]:
            self.buckets[(int(s[0] / self.cell), int(s[1] / self.cell))].append(s)

    def query(self, lat, lon):
        gx, gy = int(lat / self.cell), int(lon / self.cell)
        best_day, best_night, near_stop = None, None, None
        for dx in (-2, -1, 0, 1, 2):
            for dy in (-2, -1, 0, 1, 2):
                for s in self.buckets.get((gx + dx, gy + dy), ()):
                    km = haversine_km(lat, lon, s[0], s[1])
                    if km * 1000 > ACCESS_M:
                        continue
                    w = walk_sec(km) / 60
                    if near_stop is None or w < near_stop[1]:
                        near_stop = (s[2], w)
                    if s[3] is not None and (best_day is None or s[3] + w < best_day):
                        best_day = s[3] + w
                    nt = [x + w if x is not None else None for x in s[4]]
                    best_night = nt if best_night is None else [
                        min((v for v in (a, b) if v is not None), default=None) for a, b in zip(best_night, nt)]
        if near_stop is None:
            return None
        nights = [round(x) for x in best_night or [] if x is not None]
        full = best_night and all(x is not None for x in best_night)
        return dict(pt_day=round(best_day) if best_day is not None else None,
                    night_worst=max(nights) if full else None,
                    night_typ=sorted(nights)[len(nights) // 2] if nights else None,
                    walk_stop=round(near_stop[1]), stop=near_stop[0])
