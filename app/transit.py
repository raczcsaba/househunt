"""Refresh train/bus times and night departures from the MÁV journey planner (jegy.mav.hu).

Run monthly: MÁV changes its timetable every December and adds/removes night trains.
"""
import json
import uuid
from datetime import date, datetime, timedelta

from .common import http, log

API = "https://jegy-a.mav.hu/IK_API_PROD/api/OfferRequestApi/GetOfferRequest"
CITY_CODE = {"Budapest": "005510009", "Szeged": "005517228"}
HDRS = {"Language": "hu", "UserSessionId": str(uuid.uuid4())}


def _offers(frm, to, local_dt: datetime):
    start = (local_dt - timedelta(hours=_utc_offset(local_dt))).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    body = {"offerkind": "1", "startStationCode": frm, "innerStationsCodes": [], "endStationCode": to,
            "passangers": [{"passengerCount": 1, "passengerId": 0, "customerTypeKey": "HU_44_025-065",
                            "customerDiscountsKeys": []}],
            "isOneWayTicket": True, "isTravelEndTime": False, "isSupplementaryTicketsOnly": False,
            "travelStartDate": start, "travelReturnDate": start, "selectedServices": [52],
            "selectedSearchServices": ["BOOK_INTEGRATED"], "eszkozSzamok": [], "isOfDetailedSearch": False}
    r = http(API, data=body, headers=HDRS, as_json=True)
    out = []
    for x in r.get("route") or []:
        h, m = map(int, x["travelTimeMin"].split(":"))
        out.append((datetime.fromisoformat(x["departure"]["time"][:16]), h * 60 + m))
    return out


def _utc_offset(dt):
    # Hungary: CEST (UTC+2) from last Sunday of March to last Sunday of October, else CET.
    def last_sunday(y, mth):
        d = date(y, mth + 1, 1) - timedelta(days=1) if mth < 12 else date(y, 12, 31)
        return d - timedelta(days=(d.weekday() + 1) % 7)
    return 2 if last_sunday(dt.year, 3) <= dt.date() < last_sunday(dt.year, 10) else 1


def _next_weekday(wd):
    d = date.today() + timedelta(days=1)
    while d.weekday() != wd:
        d += timedelta(days=1)
    return d


def night(code, city, d: date):
    """Departures from the city between 00:00 and 04:30 after the evening of day d (trip <= 150 min)."""
    t = datetime.combine(d, datetime.min.time()) + timedelta(hours=23)
    end = t + timedelta(hours=6, minutes=30)
    seen = {}
    for _ in range(4):
        r = _offers(city, code, t)
        if not r:
            break
        for dep, mins in r:
            if mins <= 150:
                seen[dep] = mins
        last = max(dep for dep, _ in r)
        if last >= end:
            break
        t = last + timedelta(minutes=1)
    w0 = datetime.combine(d + timedelta(days=1), datetime.min.time())
    w1 = w0 + timedelta(hours=4, minutes=30)
    deps = sorted(seen)
    inwin = [x for x in deps if w0 <= x <= w1]
    before = [x for x in deps if x < w0]
    after = [x for x in deps if x > w1]
    seq = ([before[-1] if before else w0 - timedelta(hours=1)] + inwin
           + [after[0] if after else w1 + timedelta(minutes=90)])
    gap = max(int((b - a).total_seconds() // 60) for a, b in zip(seq, seq[1:]))
    return [x.strftime("%H:%M") for x in inwin], gap


def day_times(code, city):
    d = _next_weekday(1)  # Tuesday
    mins = []
    for h in (6, 9, 13, 16):
        for f, t in ((code, city), (city, code)):
            mins += [m for _, m in _offers(f, t, datetime.combine(d, datetime.min.time()) + timedelta(hours=h))]
    mins.sort()
    return (mins[0], mins[len(mins) // 2]) if mins else (None, None)


def refresh(towns_path_in, towns_path_out):
    with open(towns_path_in) as f:
        cfg = json.load(f)
    for t in cfg["towns"]:
        city = CITY_CODE[t["city"]]
        try:
            f, typ = day_times(t["mav_code"], city)
            sat, gap = night(t["mav_code"], city, _next_weekday(5))
            wed, _ = night(t["mav_code"], city, _next_weekday(2))
        except Exception as e:
            log.warning("transit refresh failed for %s: %s", t["name"], e)
            continue
        if f:
            t.update(pt_fastest=f, pt_typical=typ)
        t.update(night_sat=sat, night_maxgap=gap, night_wed=wed)
        t["meets_all"] = bool(f and f <= 60 and typ <= 75 and gap <= 130 and t["car_min"] <= 90)
        log.info("transit %s: %s/%s min, night %s (gap %s)", t["name"], f, typ, sat, gap)
    cfg["transit_checked"] = date.today().isoformat()
    with open(towns_path_out, "w") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
