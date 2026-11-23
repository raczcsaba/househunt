"""A source defined in config/sources.toml (or the SOURCE_URLS env var) instead of in code.

    [[source]]
    name = "example"
    url = "https://www.example.hu/elado-haz/{town_slug}?oldal={page}"

Placeholders in `url`: {town} (URL-encoded name), {town_slug} (budaors), {district} (11),
{district_roman} (xi), {page}. With {town...} the search runs once per town; without, the URL is one
area-wide search and each listing is matched to a town by its address.
See config/sources.example.toml for every option.
"""
import re
import urllib.parse

from .. import extract, fetch
from ..common import ROMAN, FetchError, Throttled, district_from_text, district_name, log, slug

PER_TOWN = re.compile(r"\{(town|town_slug|district|district_roman)\}")


class ConfiguredSource:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.NAME = cfg["name"]
        self.LABEL = cfg.get("label") or cfg["name"]
        self.url = cfg["url"]
        self.max_pages = int(cfg.get("max_pages", 20))
        self.first_page = int(cfg.get("first_page", 1))
        self.delay = float(cfg.get("delay", 0)) or None
        self.browser = cfg.get("render") == "browser"
        self.method = None                 # which extractor worked, for the logs
        self.llm_pages = int(cfg.get("claude_pages_per_run", 40))
        if PER_TOWN.search(self.url):
            self.scrape = self._scrape_town
        else:
            self.scrape_area = self._scrape_area
        if cfg.get("details"):
            self.detail = self._detail

    # -------------------------------------------------------------- fetching
    def _page(self, url):
        try:
            return fetch.render(url, delay=self.delay) if self.browser else fetch.get(url, delay=self.delay)
        except fetch.Blocked:
            raise
        except fetch.FetchFailed as e:
            raise FetchError(str(e)) from e

    def _pages(self, url_tpl, fill):
        """Yield the items of each results page until a page brings nothing new."""
        seen = set()
        pages = range(self.first_page, self.first_page + self.max_pages) if "{page}" in url_tpl else [None]
        for n in pages:
            url = url_tpl.format(**fill, page=n if n is not None else "")
            html = self._page(url)
            allow_llm = self.llm_pages > 0
            items, method = extract.extract(html, url, self.cfg, allow_llm=allow_llm)
            if method == "claude":
                self.llm_pages -= 1
            if method and method != self.method:
                log.info("%s: listings found via %s", self.NAME, method)
                self.method = method
            new = [it for it in items if it["source_id"] not in seen]
            if not new:
                if n == self.first_page and not items:
                    log.warning("%s: no listings recognised on %s (see `python -m app.main probe <url>`)",
                                self.NAME, url)
                break
            seen.update(it["source_id"] for it in new)
            yield from new

    # -------------------------------------------------------------- per town
    def _fill(self, town):
        d = town.get("district")
        return {"town": urllib.parse.quote(town["name"]), "town_slug": slug(town["name"]),
                "district": d or "", "district_roman": ROMAN[d - 1].lower() if d else ""}

    def _scrape_town(self, town):
        try:
            items = list(self._pages(self.url, self._fill(town)))
        except fetch.Blocked as e:
            raise Throttled(str(e)) from e
        for it in items:
            it.update(source=self.NAME, town=town["name"])
        return items

    # -------------------------------------------------------------- one area-wide search
    def _scrape_area(self, towns):
        try:
            items = list(self._pages(self.url, {}))
        except fetch.Blocked as e:
            raise Throttled(str(e)) from e
        by_slug = {slug(t["name"]): t["name"] for t in towns}
        out = []
        for it in items:
            town = match_town(" ".join(str(it.get(k) or "") for k in ("address", "title")), by_slug)
            if town:
                it.update(source=self.NAME, town=town)
                out.append(it)
        log.info("%s: %d of %d listings matched to a town", self.NAME, len(out), len(items))
        return out

    # -------------------------------------------------------------- optional detail pages
    def _detail(self, row):
        try:
            html = self._page(row["url"])
        except fetch.Blocked as e:
            raise Throttled(str(e)) from e
        items, _ = extract.extract(html, row["url"], dict(self.cfg, extract="auto"), allow_llm=False)
        mine = next((i for i in items if i["source_id"] == row["source_id"]), items[0] if len(items) == 1 else None)
        if not mine:
            return {}
        return {k: mine[k] for k in ("lat", "lon", "loc_quality", "loc_note", "description", "address", "condition")
                if k in mine}


def match_town(text, by_slug):
    """Find which configured town a listing's address belongs to. Budapest districts via 'XI. kerület'."""
    d = district_from_text(text)
    if d:
        name = district_name(d)
        return name if slug(name) in by_slug else None
    s = "-" + slug(text) + "-"
    hits = [name for sl, name in by_slug.items() if f"-{sl}-" in s]
    return max(hits, key=len) if hits else None
