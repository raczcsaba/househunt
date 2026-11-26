"""Shared helpers: HTTP with polite pacing, text normalisation, geo maths."""
import json
import logging
import math
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("househunt")

# Identify honestly by default (open data downloads, geocoding, routing). Plugins may override.
_CONTACT = (os.environ.get("CONTACT_EMAIL") or "").strip()
DEFAULT_UA = (os.environ.get("USER_AGENT") or "").strip() or (
    "househunt/2.0 (+https://github.com/raczcsaba/househunt" + (f"; {_CONTACT}" if _CONTACT else "") + ")")
# Seconds between requests to the same host. Keep this polite.
DELAY = max(1.5, float(os.environ.get("REQUEST_DELAY") or "3"))
# Per-host overrides (plugins can add entries).
HOST_DELAY: dict[str, float] = {}
_last_hit: dict[str, float] = {}


class Throttled(Exception):
    """The site answered but refused to give data (soft block). Stop and retry next run."""


class FetchError(Exception):
    pass


def http(url, *, data=None, headers=None, method=None, retries=3, timeout=45, as_json=False):
    """GET/POST with per-host pacing and backoff. Raises FetchError on final failure."""
    host = urllib.parse.urlparse(url).netloc
    hdrs = {"User-Agent": DEFAULT_UA, "Accept-Language": "hu"}
    hdrs.update(headers or {})
    body = None
    if data is not None:
        body = json.dumps(data).encode() if not isinstance(data, (bytes, str)) else (
            data.encode() if isinstance(data, str) else data)
        hdrs.setdefault("Content-Type", "application/json")
    for attempt in range(retries + 1):
        wait = HOST_DELAY.get(host, DELAY) - (time.time() - _last_hit.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        _last_hit[host] = time.time()
        try:
            req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", "replace")
                return json.loads(raw) if as_json else raw
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise FetchError(f"404 {url}") from e
            if e.code in (403, 429) and attempt < retries:
                time.sleep(30 * (attempt + 1))
                continue
            if attempt >= retries:
                raise FetchError(f"HTTP {e.code} {url}") from e
            time.sleep(5 * (attempt + 1))
        except Exception as e:  # network errors, JSON errors
            if attempt >= retries:
                raise FetchError(f"{type(e).__name__}: {e} {url}") from e
            time.sleep(5 * (attempt + 1))


def slug(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def to_int(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    m = re.search(r"\d[\d\s .]*", str(v))
    if not m:
        return None
    try:
        return int(re.sub(r"[\s .]", "", m.group(0)))
    except ValueError:
        return None


def strip_html(s: str) -> str:
    s = re.sub(r"<br\s*/?>", "\n", s or "", flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"')
    return re.sub(r"[ \t]+", " ", s).strip()


RENO_WORDS = re.compile(r"felújítandó|felújításra szorul|bontandó|felújítást igényel|romos", re.I)


def guess_condition(*texts):
    t = " ".join(x or "" for x in texts)
    return "Felújítandó" if RENO_WORDS.search(t) else None


def haversine_km(a_lat, a_lon, b_lat, b_lon):
    p = math.radians
    x = (math.sin(p(b_lat - a_lat) / 2) ** 2
         + math.cos(p(a_lat)) * math.cos(p(b_lat)) * math.sin(p(b_lon - a_lon) / 2) ** 2)
    return 2 * 6371 * math.asin(math.sqrt(x))


def bike_minutes(km_straight):
    # Straight line × 1.25 detour factor at 15 km/h.
    return round(km_straight * 1.25 / 15 * 60)


def notify(message: str):
    """Optional push message, e.g. NOTIFY_URL=https://ntfy.sh/<your-secret-topic>. Never raises."""
    url = (os.environ.get("NOTIFY_URL") or "").strip()
    if not url:
        return
    try:
        req = urllib.request.Request(url, data=message.encode("utf-8"), method="POST",
                                     headers={"Title": "househunt", "Content-Type": "text/plain; charset=utf-8"})
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:
        log.warning("notify failed: %s", e)


# ---------------------------------------------------------------- Budapest districts
ROMAN = ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII", "XIII", "XIV", "XV", "XVI",
         "XVII", "XVIII", "XIX", "XX", "XXI", "XXII", "XXIII"]


def district_name(n):
    """1 -> 'Budapest I.' (the town name used for Budapest districts everywhere in the app)."""
    return f"Budapest {ROMAN[n - 1]}." if 1 <= n <= 23 else None


def district_from_text(text):
    """'XI. kerület', 'Budapest XII. ker', '11. kerület' -> district number."""
    if not text:
        return None
    m = re.search(r"\b([IVX]{1,5})\.\s*(?:kerület|ker\b)", text)
    if m and m.group(1) in ROMAN:
        return ROMAN.index(m.group(1)) + 1
    m = re.search(r"\b(\d{1,2})\.\s*kerület", text)
    if m and 1 <= int(m.group(1)) <= 23:
        return int(m.group(1))
    m = re.search(r"\b1(\d\d)\d\s+Budapest", text)        # postcode 1116 -> XI.
    if m and 1 <= int(m.group(1)) <= 23:
        return int(m.group(1))
    return None
