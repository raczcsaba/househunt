"""Find house listings in any page, without site-specific code.

Tried in order, first one that yields listings wins:
  1. schema.org JSON-LD (<script type="application/ld+json">): many portals publish this for search engines.
  2. Embedded page data: __NEXT_DATA__, __NUXT__, window.__INITIAL_STATE__ and other JSON in the page;
     the largest list of objects that look like listings (a price plus a link or id) is used.
  3. CSS selectors from the source's config, for sites with neither.
  4. Optional: Claude reads the page text (ANTHROPIC_API_KEY set and the source allows it).

Only facts about the house are kept (price, size, plot, rooms, address, coordinates, link, ad text).
Seller names and phone numbers are never extracted, and phone numbers are removed from ad text.
"""
import hashlib
import json
import os
import re
import urllib.parse

from .common import log, strip_html, to_int

FIELDS = ("source_id", "url", "title", "price", "size", "plot", "rooms", "address", "lat", "lon",
          "description", "condition")

# ---------------------------------------------------------------- value parsing
PHONE = re.compile(r"(?:\+?36|06)[\s/().-]*\d{1,2}[\s/().-]*\d{3}[\s/.-]*\d{3,4}")


def clean_text(s):
    return PHONE.sub("[phone removed]", strip_html(s or "")) if s else None


def parse_price(v):
    """45900000, '45 900 000 Ft', '45,9 M Ft', '45.9 millió', '129 M' -> HUF as int."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v * 1_000_000) if 0 < v < 10_000 else int(v)     # 45.9 meaning millions
    s = str(v).replace("\xa0", " ").lower()
    m = re.search(r"(\d[\d .]*(?:,\d+)?)\s*(m(?:illió)?\b|mft|m ft)?", s)
    if not m:
        return None
    num, mult = m.group(1).replace(" ", "").strip("."), m.group(2)
    if "," in num:                                   # Hungarian decimal comma: 45,9
        num = num.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d+\.\d{1,2}", num) and mult:  # 45.9 M
        pass
    else:                                            # 45.900.000 thousands separators
        num = num.replace(".", "")
    try:
        val = float(num)
    except ValueError:
        return None
    return int(val * 1_000_000) if mult or val < 10_000 else int(val)


def parse_area(v):
    if isinstance(v, dict):
        v = v.get("value") or v.get("amount")
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    m = re.search(r"\d[\d\s.]*", str(v).replace("\xa0", " "))
    return to_int(m.group(0)) if m else None


def parse_rooms(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    if not v:
        return None
    nums = re.findall(r"\d+(?:[.,]\d+)?", str(v))
    if not nums:
        return None
    total = float(nums[0].replace(",", "."))
    if len(nums) > 1 and "fél" in str(v):          # '3 + 2 fél' -> 4
        total += 0.5 * float(nums[1].replace(",", "."))
    return total


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def finish(item, page_url, id_from=None):
    """Normalise one raw listing dict. Returns None if it can't be a listing."""
    url = item.get("url")
    if url:
        url = urllib.parse.urljoin(page_url, str(url))
    sid = item.get("source_id") or (re.search(r"(\d{5,})(?!.*\d{5,})", url).group(1)
                                    if url and re.search(r"\d{5,}", url) else None)
    if not sid and url:
        sid = hashlib.sha1(url.split("#")[0].split("?")[0].encode()).hexdigest()[:16]
    if not sid or not (url or id_from):
        return None
    if not url and id_from:
        url = id_from.format(id=sid)
    out = {
        "source_id": str(sid), "url": url,
        "title": (strip_html(str(item.get("title") or "")) or None),
        "price": parse_price(item.get("price")),
        "size": parse_area(item.get("size")), "plot": parse_area(item.get("plot")),
        "rooms": parse_rooms(item.get("rooms")),
        "address": strip_html(item["address"]) if isinstance(item.get("address"), str) else None,
        "description": clean_text(item.get("description")) if isinstance(item.get("description"), str) else None,
        "condition": item.get("condition") if isinstance(item.get("condition"), str) else None,
    }
    lat, lon = _num(item.get("lat")), _num(item.get("lon"))
    if lat and lon and 45 < lat < 49 and 16 < lon < 23.5:            # inside Hungary
        out.update(lat=lat, lon=lon, loc_quality="exact", loc_note="map pin from the listing")
    if not (out["price"] or out["size"] or out["title"]):
        return None
    return {k: v for k, v in out.items() if v is not None}


# ---------------------------------------------------------------- 1. JSON-LD
SKIP_TYPES = {"organization", "website", "webpage", "breadcrumblist", "searchaction", "person", "imageobject",
              "realestateagent", "localbusiness", "sitenavigationelement", "faqpage", "question", "answer"}


def _ld_objects(html):
    for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            yield json.loads(m.group(1).strip())
        except ValueError:
            continue


def _walk_ld(o):
    if isinstance(o, list):
        for x in o:
            yield from _walk_ld(x)
    elif isinstance(o, dict):
        yield o
        for k in ("@graph", "itemListElement", "mainEntity", "item", "about"):
            if k in o:
                yield from _walk_ld(o[k])


def _ld_address(a):
    if isinstance(a, str):
        return a
    if isinstance(a, dict):
        return ", ".join(str(a[k]) for k in ("streetAddress", "addressLocality", "addressRegion") if a.get(k))


def from_jsonld(html, page_url):
    items = []
    for root in _ld_objects(html):
        for o in _walk_ld(root):
            types = o.get("@type") or ""
            types = {t.lower() for t in (types if isinstance(types, list) else [types])}
            if types & SKIP_TYPES or "listitem" in types:
                continue
            offers = o.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            obj = o.get("itemOffered") if isinstance(o.get("itemOffered"), dict) else o
            price = offers.get("price") or (offers.get("priceSpecification") or {}).get("price") or o.get("price")
            geo = obj.get("geo") or o.get("geo") or {}
            raw = {
                "url": o.get("url") or offers.get("url") or (o.get("@id") if str(o.get("@id", "")).startswith("http") else None),
                "title": o.get("name") or obj.get("name"), "price": price,
                "size": obj.get("floorSize") or o.get("floorSize"),
                "plot": obj.get("lotSize") or o.get("lotSize"),
                "rooms": obj.get("numberOfRooms") or o.get("numberOfRooms") or obj.get("numberOfBedrooms"),
                "address": _ld_address(obj.get("address") or o.get("address")),
                "lat": geo.get("latitude"), "lon": geo.get("longitude"),
                "description": o.get("description") or obj.get("description"),
            }
            if raw["url"] and (price or raw["size"]):
                it = finish(raw, page_url)
                if it:
                    items.append(it)
    return _dedupe(items)


# ---------------------------------------------------------------- 2. embedded JSON
SYN = {
    "price": ("price", "pricevalue", "pricehuf", "priceamount", "amount", "ar", "vetelar", "salesprice", "saleprice"),
    "plot": ("plot", "plotarea", "plotareasize", "plotsize", "lotsize", "lotarea", "landarea", "gardensize",
             "telek", "telekterulet", "telekmeret"),
    "size": ("size", "area", "floorarea", "floorareasize", "floorsize", "livingarea", "alapterulet", "terulet",
             "squaremeters", "sqm", "m2", "usablearea"),
    "rooms": ("rooms", "room", "numberofrooms", "roomcount", "roomsnumber", "szobak", "szoba", "szobaszam"),
    "title": ("title", "subject", "name", "headline", "adverttitle", "listingtitle", "heading"),
    "url": ("url", "link", "href", "canonicalurl", "permalink", "detailurl", "adurl", "path"),
    "source_id": ("id", "adid", "advertid", "listid", "listingid", "propertyid", "code", "uuid"),
    "lat": ("lat", "latitude", "geolat"),
    "lon": ("lon", "lng", "long", "longitude", "geolng", "geolon"),
    "address": ("address", "fulladdress", "location", "locationname", "city", "street", "cim", "settlement"),
    "description": ("description", "body", "text", "desc", "leiras"),
    "condition": ("condition", "realestatecondition", "allapot"),
}
_KEY = {syn: field for field, syns in SYN.items() for syn in syns}


def _norm_key(k):
    return re.sub(r"[^a-z0-9]", "", str(k).lower())


def _flatten(d, depth=0):
    """One listing object -> flat {normalised key: scalar}. Nested dicts and key/value parameter lists
    (e.g. [{'key': 'size', 'values': [{'label': '120 m²'}]}]) are merged in."""
    out = {}
    for k, v in d.items():
        nk = _norm_key(k)
        if isinstance(v, dict):
            if any(x in v for x in ("value", "amount", "label")) and not isinstance(v.get("value", ""), (dict, list)):
                out.setdefault(nk, v.get("value", v.get("amount", v.get("label"))))
            if depth < 2:
                for kk, vv in _flatten(v, depth + 1).items():
                    out.setdefault(kk, vv)
                    out.setdefault(nk + kk, vv)
        elif isinstance(v, list):
            if v and all(isinstance(x, dict) and ("key" in x or "name" in x) for x in v):
                for p in v:
                    val = p.get("value", p.get("values", p.get("label")))
                    if isinstance(val, list) and val:
                        val = val[0].get("label", val[0].get("value")) if isinstance(val[0], dict) else val[0]
                    out.setdefault(_norm_key(p.get("key") or p.get("name")), val)
        else:
            out.setdefault(nk, v)
    return out


def _map(flat):
    raw = {}
    for nk, v in flat.items():
        f = _KEY.get(nk)
        if f and f not in raw and v not in (None, "", [], {}):
            raw[f] = v
    if "url" in raw and not re.match(r"^(https?:)?/|^[\w-]+/", str(raw["url"])):
        raw.pop("url")
    return raw


def _embedded_json(html):
    for m in re.finditer(r'<script[^>]+type=["\']application/(?:json|ld\+json)["\'][^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            yield json.loads(m.group(1))
        except ValueError:
            pass
    dec = json.JSONDecoder()
    for m in re.finditer(r"(?:window\.)?__[A-Z0-9_]+__\s*=\s*", html):
        try:
            yield dec.raw_decode(html, m.end())[0]
        except ValueError:
            pass


def _lists(o, depth=0):
    if depth > 12:
        return
    if isinstance(o, list):
        if len(o) >= 2 and sum(isinstance(x, dict) for x in o) >= 0.8 * len(o):
            yield o
        for x in o:
            yield from _lists(x, depth + 1)
    elif isinstance(o, dict):
        for v in o.values():
            yield from _lists(v, depth + 1)


def from_embedded(html, page_url, id_url=None):
    best, best_score = [], 0
    for blob in _embedded_json(html):
        for lst in _lists(blob):
            mapped = [_map(_flatten(x)) for x in lst if isinstance(x, dict)]
            good = [m for m in mapped if "price" in m and ("url" in m or ("source_id" in m and id_url))
                    and ("size" in m or "title" in m)]
            score = len(good) * (1 + 0.5 * (len(good) == len(mapped)))
            if score > best_score:
                best, best_score = good, score
    return _dedupe([it for it in (finish(m, page_url, id_url) for m in best) if it])


# ---------------------------------------------------------------- 3. CSS selectors
def from_selectors(html, page_url, sel, id_url=None):
    """sel = {'item': 'article.card', 'url': 'a@href', 'title': 'h2', 'price': '.price', ...}"""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    items = []
    for card in soup.select(sel["item"]):
        raw = {}
        for field, spec in sel.items():
            if field == "item" or field not in SYN:
                continue
            css, _, attr = spec.partition("@")
            el = card.select_one(css) if css else card
            if el is not None:
                raw[field] = el.get(attr) if attr else el.get_text(" ", strip=True)
        it = finish(raw, page_url, id_url)
        if it:
            items.append(it)
    return _dedupe(items)


# ---------------------------------------------------------------- 4. Claude (optional)
LLM_SCHEMA = {
    "type": "object",
    "properties": {
        "listings": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Link to the listing's own page, exactly as given in the page."},
                "title": {"type": ["string", "null"]},
                "price_huf": {"type": ["integer", "null"], "description": "Asking price in forints (45,9 M Ft = 45900000)."},
                "size_m2": {"type": ["integer", "null"], "description": "Living area."},
                "plot_m2": {"type": ["integer", "null"], "description": "Plot / land area."},
                "rooms": {"type": ["number", "null"]},
                "address": {"type": ["string", "null"], "description": "Town, district or street as written."},
            },
            "required": ["url", "title", "price_huf", "size_m2", "plot_m2", "rooms", "address"],
            "additionalProperties": False,
        }},
    },
    "required": ["listings"],
    "additionalProperties": False,
}
LLM_PROMPT = """This is the text of a real-estate search results page ({url}). List every property for sale on it.
Links are written as [text](url). Only report what the page states; use null for anything not shown.
Do not include seller names or phone numbers. Skip ads for renting, and navigation or promotional links.

{text}"""

_client = None


def llm_enabled():
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")) \
        and (os.environ.get("CLAUDE_ENABLED") or "1") != "0"


def page_text(html):
    """Visible text with links kept as [text](href); scripts, styles and markup removed."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "svg", "head", "iframe"]):
        t.decompose()
    for a in soup.find_all("a", href=True):
        a.replace_with(f" [{a.get_text(' ', strip=True)}]({a['href']}) ")
    return re.sub(r"\s*\n\s*", "\n", re.sub(r"[ \t]+", " ", soup.get_text("\n"))).strip()


def from_llm(html, page_url):
    global _client
    import anthropic
    if _client is None:
        _client = anthropic.Anthropic(max_retries=3)
    model = os.environ.get("CLAUDE_MODEL") or "claude-opus-5-5"
    kwargs = dict(
        model=model, max_tokens=16000,
        output_config={"effort": os.environ.get("CLAUDE_EFFORT") or "low",
                       "format": {"type": "json_schema", "schema": LLM_SCHEMA}},
        messages=[{"role": "user", "content": LLM_PROMPT.format(url=page_url, text=page_text(html))}],
    )
    if model.startswith(("claude-opus", "claude-fable")):
        resp = _client.beta.messages.create(**kwargs, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    else:
        resp = _client.messages.create(**kwargs)
    if resp.stop_reason == "refusal":
        log.warning("Claude declined to read %s", page_url)
        return []
    if resp.stop_reason == "max_tokens":
        log.warning("Claude's answer for %s was cut off; some listings on that page are missing", page_url)
        return []
    text = next((b.text for b in resp.content if b.type == "text"), "")
    rows = json.loads(text).get("listings", []) if text else []
    return _dedupe([it for it in (finish({"url": r["url"], "title": r["title"], "price": r["price_huf"],
                                          "size": r["size_m2"], "plot": r["plot_m2"], "rooms": r["rooms"],
                                          "address": r["address"]}, page_url) for r in rows) if it])


# ---------------------------------------------------------------- driver
def extract(html, page_url, cfg=None, allow_llm=True):
    """Returns (items, method). cfg is the source's config dict (selectors, extract, item_url)."""
    cfg = cfg or {}
    mode = cfg.get("extract", "auto")
    id_url = cfg.get("item_url")
    steps = {
        "jsonld": lambda: from_jsonld(html, page_url),
        "embedded": lambda: from_embedded(html, page_url, id_url),
        "selectors": lambda: from_selectors(html, page_url, cfg["selectors"], id_url) if cfg.get("selectors") else [],
        "claude": lambda: from_llm(html, page_url) if allow_llm and llm_enabled() and cfg.get("claude", True) else [],
    }
    order = list(steps) if mode == "auto" else [mode]
    for name in order:
        items = steps[name]()
        if items:
            return items, name
    return [], None


def _dedupe(items):
    seen, out = set(), []
    for it in items:
        if it["source_id"] not in seen:
            seen.add(it["source_id"])
            out.append(it)
    return out
