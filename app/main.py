"""Entry point.

  python -m app.main run          one full scrape + locate + export
  python -m app.main probe <url>  test a search URL: robots.txt, which extractor works, first listings
  python -m app.main grid         rebuild the Budapest public-transport travel-time grid
  python -m app.main transit      refresh commuter train times (needs MAV_PLANNER=1)
  python -m app.main <plugin>     run a job plugin (see plugins/README.md)
  python -m app.main serve        dashboard on :8080 + scheduler (what the container runs)
"""
import http.server
import json
import logging
import os
import shutil
import sys
import threading
import time
from datetime import datetime

from croniter import croniter

from . import bpmetrics, db, export, locate, transit
from .common import FetchError, Throttled, log, notify
from .sources import ALL as SOURCES, JOB_PLUGINS

DATA = (os.environ.get("DATA_DIR") or "/data")
HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.join(DATA, "site")
TOWNS_DEFAULT = os.path.join(HERE, "..", "config", "towns.json")
TOWNS_LIVE = os.path.join(DATA, "towns.json")   # refreshed copy lives in the volume
BP_AREA = os.path.join(HERE, "..", "config", "budapest_area.json")
GRID = os.path.join(DATA, "transit_grid.json")
_lock = threading.Lock()
# MÁV's journey planner has no public API terms; it is only queried when you opt in. The official,
# free MÁV GTFS (MAV_GTFS_URL) is the recommended way to get train times.
MAV_PLANNER = (os.environ.get("MAV_PLANNER") or "0") == "1"


def load_towns():
    """Commuter towns (towns.json) plus Budapest districts and agglomeration (budapest_area.json).
    A town can be both, e.g. Dunakeszi. AREAS=commuter or AREAS=budapest limits the scrape."""
    path = TOWNS_LIVE if os.path.exists(TOWNS_LIVE) else TOWNS_DEFAULT
    with open(path) as f:
        cfg = json.load(f)
    areas = {a.strip() for a in (os.environ.get("AREAS") or "commuter,budapest").split(",")}
    by_name = {}
    if "commuter" in areas:
        for t in cfg["towns"]:
            by_name[t["name"]] = dict(t, commuter=True, centre=t["stations"][0])
    with open(BP_AREA) as f:
        bp = json.load(f)
    if "budapest" in areas:
        for p in bp["places"]:
            t = by_name.setdefault(p["name"], {"name": p["name"], "city": "Budapest", "commuter": False})
            t.update(bp=True, kind=p["kind"], district=p.get("district"))
            t.setdefault("centre", [p["lat"], p["lon"]])
            t["bp_centre"] = [p["lat"], p["lon"]]
            if p.get("jofogas_county"):
                t.setdefault("jofogas_county", p["jofogas_county"])
    only = {x.strip() for x in (os.environ.get("TOWNS") or "").split(",") if x.strip()}
    cfg["towns"] = [t for t in by_name.values() if not only or t["name"] in only]
    cfg["bp_centre"] = bp["centre"]
    return cfg


def scrape_plan(mod, towns):
    """(label, function, towns covered). Budapest districts come from one city-wide search when the
    source supports it; everything else is scraped town by town."""
    if hasattr(mod, "scrape_area"):    # one search covering the whole area
        return [("all towns", lambda: mod.scrape_area(towns), [t["name"] for t in towns])]
    districts = [t for t in towns if t.get("district")]
    plan = []
    if districts and hasattr(mod, "scrape_budapest"):
        plan.append(("Budapest", lambda: mod.scrape_budapest(districts), [t["name"] for t in districts]))
        rest = [t for t in towns if not t.get("district")]
    else:
        rest = towns
    plan += [(t["name"], (lambda t=t: mod.scrape(t)), [t["name"]]) for t in rest]
    return plan


def run_once():
    with _lock:
        started = datetime.now().isoformat(timespec="seconds")
        os.makedirs(SITE, exist_ok=True)
        con = db.connect(os.path.join(DATA, "listings.db"))
        cfg = load_towns()
        towns = {t["name"]: t for t in cfg["towns"]}
        day = db.today()
        enabled = [s.strip() for s in (os.environ.get("SOURCES") or ",".join(SOURCES)).split(",") if s.strip()]
        stats = {"new": 0, "price_changed": 0, "removed": 0, "errors": [], "per_source": {}}
        for sname in enabled:
            mod = SOURCES[sname]
            n_src = 0
            for label, fn, covers in scrape_plan(mod, cfg["towns"]):
                try:
                    items = fn()
                except Throttled as e:   # blocked or rate limited: stop this source, keep its listings
                    log.warning("%s stopped: %s", sname, e)
                    stats["errors"].append(f"{sname}: {e}")
                    break
                except FetchError as e:
                    log.warning("%s %s: %s", sname, label, e)
                    stats["errors"].append(f"{sname} {label}: {e}")
                    continue
                for it in items:
                    if it["town"] not in towns:
                        continue
                    if it.get("lat") is not None:
                        it["lat"], it["lon"] = float(it["lat"]), float(it["lon"])
                    res = db.upsert(con, it, day)
                    stats["new"] += res == "new"
                    stats["price_changed"] += res == "price"
                for name in covers:
                    stats["removed"] += db.mark_removed(con, sname, name, day)
                con.commit()
                n_src += len(items)
                log.info("%s %-18s %4d listings", sname, label, len(items))
            stats["per_source"][sname] = n_src
            # Per-listing detail pages, only for listings we have not opened yet.
            if hasattr(mod, "detail"):
                cap = int(os.environ.get("MAX_DETAILS_PER_RUN") or "400")
                pending = con.execute("SELECT * FROM listings WHERE source=? AND active=1 AND detail_done=0 "
                                      "ORDER BY first_seen DESC LIMIT ?", (sname, cap)).fetchall()
                log.info("%s: fetching %d detail pages", sname, len(pending))
                for i, row in enumerate(pending):
                    try:
                        upd = {k: v for k, v in mod.detail(dict(row)).items() if v not in (None, "")}
                    except Throttled as e:
                        log.warning("%s is throttling (%s); remaining details wait for the next run", sname, e)
                        stats["errors"].append(f"{sname} details: throttled after {i} pages")
                        break
                    except FetchError as e:
                        log.warning("detail %s: %s", row["uid"], e)
                        continue
                    if "lat" in upd:
                        upd["lat"], upd["lon"] = float(upd["lat"]), float(upd["lon"])
                    upd["detail_done"] = 1
                    con.execute(f"UPDATE listings SET {','.join(k + '=?' for k in upd)} WHERE uid=?",
                                [*upd.values(), row["uid"]])
                    if i % 50 == 0:
                        con.commit()
                con.commit()
        finish(con, cfg, stats, started)
        con.close()


def finish(con, cfg, stats, started):
    """Locate, measure, merge duplicates and publish data.json. Shared by every scraper entry point."""
    towns = {t["name"]: t for t in cfg["towns"]}
    budget = int(os.environ.get("CLAUDE_MAX_PER_RUN") or "400")
    stats["locate"] = locate.locate_pending(con, towns, budget)
    locate.compute_distances(con, towns)
    stats["bp_metrics"] = bpmetrics.update(con, towns, GRID)
    export.cluster(con)
    run_info = dict(started=started, finished=datetime.now().isoformat(timespec="seconds"),
                    **{k: v for k, v in stats.items() if k != "errors"}, errors=stats["errors"][:50])
    os.makedirs(SITE, exist_ok=True)
    n = export.export(con, cfg, os.path.join(SITE, "data.json"), run_info)
    db.log_run(con, started, run_info)
    shutil.copy(os.path.join(HERE, "web", "index.html"), os.path.join(SITE, "index.html"))
    log.info("done: %d houses (%d new, %d price changes, %d removed)", n, stats["new"],
             stats["price_changed"], stats["removed"])
    if stats["new"] or stats["price_changed"]:
        notify(f"househunt: {stats['new']} new ads, {stats['price_changed']} price changes, "
               f"{stats['removed']} removed ({', '.join(stats.get('per_source', {}))}).")


def run_transit():
    if not MAV_PLANNER:
        log.info("transit refresh skipped: set MAV_PLANNER=1 to query the MÁV journey planner")
        return
    with _lock:
        src = TOWNS_LIVE if os.path.exists(TOWNS_LIVE) else TOWNS_DEFAULT
        transit.refresh(src, TOWNS_LIVE)


def run_grid():
    """Download BKK/Volánbusz timetables, add MÁV trains, and rebuild the per-stop travel times."""
    from . import mavrail, transitgrid
    feed_dir = os.path.join(DATA, "gtfs")
    transitgrid.download(feed_dir)
    transitgrid.build(feed_dir, GRID + ".tmp",
                      rail_items_fn=(lambda tue, sat, net: mavrail.rail_items(tue, sat, net, cache_dir=DATA))
                      if MAV_PLANNER else None)
    os.replace(GRID + ".tmp", GRID)
    with _lock:   # re-publish so every house gets the new times
        con = db.connect(os.path.join(DATA, "listings.db"))
        finish(con, load_towns(), {"new": 0, "price_changed": 0, "removed": 0, "errors": []},
               datetime.now().isoformat(timespec="seconds"))
        con.close()


def run_job_plugin(mod):
    """A plugin with its own schedule (e.g. browser-based); it scrapes outside the lock, stores inside it."""
    started = datetime.now().isoformat(timespec="seconds")
    cfg = load_towns()
    results, errors = mod.scrape_all(cfg["towns"], DATA, SITE)
    with _lock:
        con = db.connect(os.path.join(DATA, "listings.db"))
        stats = mod.store(con, results, errors, db.today())
        finish(con, cfg, stats, started)
        con.close()


def probe(url):
    """Check one search URL the way a scrape would, and print what was found."""
    from . import extract, fetch
    print(f"user agent: {fetch.USER_AGENT}")
    print(f"robots.txt allows it: {fetch.allowed(url)}")
    try:
        html = fetch.get(url)
    except fetch.Blocked as e:
        print(f"BLOCKED: {e}\nThis site does not want automated visitors; househunt will not work around it.")
        return
    except fetch.FetchFailed as e:
        print(f"fetch failed: {e}")
        return
    print(f"page: {len(html):,} characters")
    for name, fn in (("jsonld", extract.from_jsonld), ("embedded", extract.from_embedded)):
        print(f"  {name:9} {len(fn(html, url)):4d} listings")
    items, method = extract.extract(html, url)
    print(f"used: {method or 'nothing found'}" + ("" if method or extract.llm_enabled()
          else " (set ANTHROPIC_API_KEY to let Claude read pages without structured data, or add selectors)"))
    for it in items[:5]:
        print("  ", {k: it.get(k) for k in ("price", "size", "plot", "rooms", "address", "title", "url")})


JOBS = {"scrape": run_once, "transit": run_transit, "grid": run_grid}
for _name, _mod in JOB_PLUGINS.items():
    JOBS[_name] = (lambda m=_mod: run_job_plugin(m))
_running: set = set()


def start_job(name):
    """Run a job in the background unless it is already running. Returns False if it was."""
    if name in _running:
        return False
    _running.add(name)

    def target():
        try:
            JOBS[name]()
        except Exception:
            log.exception("%s failed", name)
        finally:
            _running.discard(name)
    threading.Thread(target=target, daemon=True, name=name).start()
    return True


def scheduler():
    jobs = [((os.environ.get("SCHEDULE") or "0 6 * * *"), "scrape"),
            ((os.environ.get("GRID_SCHEDULE") or "0 2 * * 1"), "grid")]
    if MAV_PLANNER:
        jobs.append(((os.environ.get("TRANSIT_SCHEDULE") or "0 4 1 * *"), "transit"))
    for name, mod in JOB_PLUGINS.items():
        up = name.upper()
        if (os.environ.get(getattr(mod, "ENABLED_ENV", f"{up}_ENABLED")) or "1") == "1":
            jobs.append(((os.environ.get(getattr(mod, "SCHEDULE_ENV", f"{up}_SCHEDULE"))
                          or getattr(mod, "SCHEDULE_DEFAULT", "0 7 * * *")), name))
    nxt = [croniter(expr, datetime.now()).get_next(datetime) for expr, _ in jobs]
    for (expr, name), t in zip(jobs, nxt):
        log.info("scheduled %s: %s (next %s)", name, expr, t)
    while True:
        now = datetime.now()
        for i, ((expr, name), t) in enumerate(zip(jobs, nxt)):
            if now >= t:
                start_job(name)
                nxt[i] = croniter(expr, datetime.now()).get_next(datetime)
        time.sleep(30)


class Handler(http.server.SimpleHTTPRequestHandler):
    """Static dashboard plus POST /api/run/<job> and GET /api/jobs."""

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=SITE, **kw)

    def log_message(self, fmt, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/jobs"):
            return self._json(200, {"running": sorted(_running),
                                    "browser_url": (os.environ.get("BROWSER_VIEW_URL") or ""),
                                    "plugins": {n: getattr(m, "LABEL", n) for n, m in JOB_PLUGINS.items()},
                                    "sources": {n: getattr(m, "LABEL", n) for n, m in SOURCES.items()},
                                    "mav_planner": MAV_PLANNER})
        return super().do_GET()

    def do_POST(self):
        token = (os.environ.get("DASHBOARD_TOKEN") or "")
        if token and self.headers.get("X-Token") != token:
            return self._json(403, {"error": "wrong token"})
        name = self.path.rstrip("/").rsplit("/", 1)[-1]
        if not self.path.startswith("/api/run/") or name not in JOBS:
            return self._json(404, {"error": "unknown job"})
        return self._json(202 if start_job(name) else 409, {"job": name})


def serve():
    os.makedirs(SITE, exist_ok=True)
    shutil.copy(os.path.join(HERE, "web", "index.html"), os.path.join(SITE, "index.html"))
    port = int(os.environ.get("PORT") or "8080")
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("dashboard on http://localhost:%d", port)
    if (os.environ.get("RUN_ON_START") or "1") == "1":
        if not os.path.exists(GRID):
            start_job("grid")
        if not os.path.exists(os.path.join(SITE, "data.json")):
            start_job("scrape")
    scheduler()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "probe":
        probe(sys.argv[2])
    else:
        {"run": run_once, "serve": serve, **JOBS}[cmd]()
