"""Duplicate merging across portals and the dashboard's data.json (app/export.py, app/db.py)."""
import json

import pytest

from app import db, export

DAY = "2026-11-20"
TOWNS = {"towns": [{"name": "Budaörs", "stations": [[47.46, 18.95]]}, {"name": "Szentendre"}],
         "transit_checked": "2026-10-16", "bp_centre": {"lat": 47.4967, "lon": 19.0704}}


def house(source, sid, town="Budaörs", price=60_000_000, size=100, rooms=3, plot=None, **kw):
    return dict(source=source, source_id=sid, town=town, price=price, size=size, rooms=rooms, plot=plot,
                url=f"https://{source}.example/h/{sid}", title=f"Ház {sid}", **kw)


@pytest.fixture
def con():
    c = db.connect(":memory:")
    yield c
    c.close()


def clusters(con):
    groups = {}
    for r in con.execute("SELECT uid, cluster FROM listings ORDER BY uid"):
        groups.setdefault(r["cluster"], []).append(r["uid"])
    return sorted(groups.values())


def add(con, *items, day=DAY):
    for it in items:
        db.upsert(con, it, day)
    con.commit()


def row(**kw):
    base = dict(town="Budaörs", price=60_000_000, size=100, rooms=3, plot=None)
    base.update(kw)
    return base


@pytest.mark.parametrize("a, b, same", [
    (row(), row(), True),
    (row(), row(price=60_800_000), True),                  # within 1.5 %
    (row(), row(price=61_000_000), False),                 # 1.67 % apart
    (row(), row(size=103), True),                          # within 3 m²
    (row(), row(size=104), False),
    (row(), row(rooms=4), False),
    (row(), row(rooms=3.5), True),                         # 3 + 1 half room vs 3 rooms
    (row(), row(rooms=None), True),                        # unknown rooms don't block a match
    (row(plot=800), row(plot=830), True),
    (row(plot=800), row(plot=900), False),
    (row(plot=800), row(plot=None), True),
    (row(), row(town="Szentendre"), False),
    (row(price=None), row(price=None), False),             # no price, no match
    (row(size=None), row(size=None), False),               # no size, no match
])
def test_same_house(a, b, same):
    assert export._same(a, b) is same
    assert export._same(b, a) is same


def test_cluster_merges_across_portals_only(con):
    add(con,
        house("alpha", "1"), house("beta", "9", price=60_500_000, size=101),   # same house, two portals
        house("alpha", "2"),                                                    # same portal: a separate ad
        house("gamma", "5", town="Szentendre"),                                 # other town
        house("beta", "7", price=90_000_000, size=150))
    export.cluster(con)
    assert clusters(con) == [["alpha:1", "alpha:2", "beta:9"], ["beta:7"], ["gamma:5"]]
    # alpha:2 joins through beta:9, which matches both: one house reposted on alpha and also on beta


def test_cluster_is_order_independent(con):
    add(con, house("c", "1", price=60_000_000), house("a", "1", price=60_400_000), house("b", "1", price=60_800_000))
    export.cluster(con)
    (group,) = clusters(con)
    assert group == ["a:1", "b:1", "c:1"]
    assert {r["cluster"] for r in con.execute("SELECT cluster FROM listings")} == {"a:1"}   # smallest uid


def test_cluster_ignores_removed_listings_without_date(con):
    add(con, house("alpha", "1"), house("beta", "1"))
    con.execute("UPDATE listings SET active=0, removed_on=NULL WHERE uid='beta:1'")
    export.cluster(con)
    assert con.execute("SELECT cluster FROM listings WHERE uid='beta:1'").fetchone()[0] is None


def run_export(con, tmp_path):
    out = tmp_path / "site" / "data.json"
    out.parent.mkdir()
    n = export.export(con, TOWNS, str(out), {"finished": "2026-11-20T06:30:00"})
    data = json.loads(out.read_text())
    assert n == len(data["listings"])
    return data


def test_export_merges_a_cluster_into_one_row(con, tmp_path):
    add(con, house("alpha", "1", description="Rövid.", plot=700, lat=47.47, lon=18.94, loc_quality="exact"),
        house("beta", "1", price=60_500_000, description="Hosszú leírás a házról, sok részlettel.",
              condition="Felújítandó"))
    con.execute("UPDATE listings SET dist_km=1.2, bike_min=6 WHERE uid='alpha:1'")
    export.cluster(con)
    data = run_export(con, tmp_path)
    (item,) = data["listings"]
    assert item["id"] == "alpha:1"
    assert item["price"] == 60_500_000 and item["title"] == "Ház 1"       # main row: the longest ad text
    assert item["plot"] == 700 and item["cond"] == "Felújítandó"          # filled in from the other portal
    assert (item["locq"], item["dist"], item["bike"]) == ("exact", 1.2, 6)  # best location of the group
    assert [link["s"] for link in item["links"]] == ["alpha", "beta"]
    assert item["active"] is True and item["removed_on"] is None
    assert data["generated"] == "2026-11-20T06:30:00"
    assert data["towns"][0] == {"name": "Budaörs"}                         # station coordinates left out


def test_export_tracks_price_drops(con, tmp_path):
    add(con, house("alpha", "1", price=65_000_000), day="2026-11-01")
    add(con, house("alpha", "1", price=59_900_000), day="2026-11-10")
    export.cluster(con)
    (item,) = run_export(con, tmp_path)["listings"]
    assert item["hist"] == [["2026-11-01", 65_000_000], ["2026-11-10", 59_900_000]]
    assert item["price_drop"] == 5_100_000
    assert (item["first_seen"], item["last_seen"]) == ("2026-11-01", "2026-11-10")


def test_export_skips_rentals_and_typos(con, tmp_path):
    add(con, house("alpha", "1", price=250_000), house("alpha", "2", price=100_000),
        house("alpha", "3", price=None), house("alpha", "4", price=48_000_000, size=80))
    export.cluster(con)
    assert [i["id"] for i in run_export(con, tmp_path)["listings"]] == ["alpha:4"]


def test_export_keeps_recently_removed_only(con, tmp_path):
    from datetime import date, timedelta
    recent = (date.today() - timedelta(days=3)).isoformat()
    old = (date.today() - timedelta(days=export.KEEP_REMOVED_DAYS + 5)).isoformat()
    add(con, house("alpha", "1"), house("alpha", "2", price=70_000_000), house("alpha", "3", price=80_000_000))
    con.execute("UPDATE listings SET active=0, removed_on=? WHERE uid='alpha:2'", (recent,))
    con.execute("UPDATE listings SET active=0, removed_on=? WHERE uid='alpha:3'", (old,))
    export.cluster(con)
    items = {i["id"]: i for i in run_export(con, tmp_path)["listings"]}
    assert set(items) == {"alpha:1", "alpha:2"}
    assert items["alpha:2"]["active"] is False and items["alpha:2"]["removed_on"] == recent


def test_upsert_and_mark_removed(con):
    assert db.upsert(con, house("alpha", "1"), "2026-11-01") == "new"
    assert db.upsert(con, house("alpha", "1"), "2026-11-02") == "seen"
    assert db.upsert(con, house("alpha", "1", price=58_000_000), "2026-11-03") == "price"
    db.upsert(con, house("alpha", "2"), "2026-11-01")
    assert db.mark_removed(con, "alpha", "Budaörs", "2026-11-03") == 1
    gone = con.execute("SELECT active, removed_on FROM listings WHERE uid='alpha:2'").fetchone()
    assert tuple(gone) == (0, "2026-11-03")
    assert db.upsert(con, house("alpha", "2"), "2026-11-04") == "seen"     # back again
    back = con.execute("SELECT active, removed_on FROM listings WHERE uid='alpha:2'").fetchone()
    assert tuple(back) == (1, None)


def test_upsert_keeps_better_location(con):
    db.upsert(con, house("alpha", "1", lat=47.1, lon=19.1, loc_quality="street"), DAY)
    db.upsert(con, house("alpha", "1", lat=47.9, lon=19.9, loc_quality="exact"), DAY)
    r = con.execute("SELECT lat, loc_quality FROM listings").fetchone()
    assert tuple(r) == (47.1, "street")                                     # list pages don't overwrite it
