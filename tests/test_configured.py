"""Config-driven sources: town matching, URL placeholders and paging (app/sources/configured.py)."""
import pytest

from app import fetch
from app.common import FetchError, Throttled, district_from_text, slug
from app.sources import configured
from app.sources.configured import ConfiguredSource, match_town

TOWNS = ["Budaörs", "Törökbálint", "Szentendre", "Budapest XI.", "Budapest II.", "Pilisborosjenő", "Üröm"]
BY_SLUG = {slug(t): t for t in TOWNS}


@pytest.mark.parametrize("text, town", [
    ("Budaörs, Kossuth Lajos utca", "Budaörs"),
    ("Eladó ház Törökbálinton", None),                       # inflected forms don't match
    ("TÖRÖKBÁLINT, Munkácsy Mihály u.", "Törökbálint"),      # case and accents don't matter
    ("Budapest XI. kerület, Gazdagrét", "Budapest XI."),
    ("Budapest, XI. ker., Sasad", "Budapest XI."),
    ("11. kerület, Kelenföld", "Budapest XI."),
    ("1118 Budapest, Rétköz utca", "Budapest XI."),
    ("Budapest XII. kerület", None),                         # district not in the list
    ("Pomáz, Szentendrei út", None),                         # 'Szentendrei' is not Szentendre
    ("Szentendre, Szentendrei út", "Szentendre"),
    ("Pilisborosjenő, Üröm határán", "Pilisborosjenő"),       # longest match wins
    ("", None),
])
def test_match_town(text, town):
    assert match_town(text, BY_SLUG) == town


@pytest.mark.parametrize("text, n", [
    ("XI. kerület", 11), ("Budapest II. ker", 2), ("23. kerület", 23), ("1011 Budapest", 1),
    ("XXIV. kerület", None), ("30. kerület", None), ("Budaörs", None), (None, None),
])
def test_district_from_text(text, n):
    assert district_from_text(text) == n


def page(*ids):
    rows = ",".join(f'{{"id": {i}, "title": "Ház {i}", "url": "/h/{i}", "price": {50_000_000 + i}}}' for i in ids)
    return f'<script type="application/json">{{"ads": [{rows}]}}</script>'


@pytest.fixture
def fake_site(monkeypatch):
    """Replace fetch.get with canned pages and record the URLs asked for."""
    pages, asked = {}, []

    def get(url, delay=None):
        asked.append(url)
        if url not in pages:
            return "<html><body>Nincs több találat</body></html>"
        value = pages[url]
        if isinstance(value, Exception):
            raise value
        return value
    monkeypatch.setattr(configured.fetch, "get", get)
    return pages, asked


def test_per_town_source_fills_placeholders_and_pages(fake_site):
    pages, asked = fake_site
    pages["https://x.hu/elado-haz/torokbalint?p=1"] = page(100001, 100002)
    pages["https://x.hu/elado-haz/torokbalint?p=2"] = page(100003)
    src = ConfiguredSource({"name": "x", "url": "https://x.hu/elado-haz/{town_slug}?p={page}", "max_pages": 5})
    assert hasattr(src, "scrape") and not hasattr(src, "scrape_area")
    items = src.scrape({"name": "Törökbálint"})
    assert [i["source_id"] for i in items] == ["100001", "100002", "100003"]
    assert {(i["source"], i["town"]) for i in items} == {("x", "Törökbálint")}
    assert asked == [f"https://x.hu/elado-haz/torokbalint?p={n}" for n in (1, 2, 3)]   # stops at an empty page
    assert src.method == "embedded"


def test_paging_stops_when_a_page_repeats(fake_site):
    pages, asked = fake_site
    for n in range(1, 10):
        pages[f"https://x.hu/s?p={n}"] = page(100001, 100002)      # site ignores the page number
    src = ConfiguredSource({"name": "x", "url": "https://x.hu/s?p={page}", "max_pages": 20})
    assert len(list(src._pages(src.url, {}))) == 2
    assert len(asked) == 2


def test_district_placeholders(fake_site):
    _, asked = fake_site
    src = ConfiguredSource({"name": "x", "url": "https://x.hu/{district_roman}-kerulet/{district}"})
    src.scrape({"name": "Budapest XI.", "district": 11})
    assert asked == ["https://x.hu/xi-kerulet/11"]


def test_area_source_matches_listings_to_towns(fake_site):
    pages, _ = fake_site
    pages["https://x.hu/pest?p=1"] = (
        '<script type="application/json">{"ads": ['
        '{"id": 200001, "title": "Ház", "url": "/h/200001", "price": 60000000, "address": "Budaörs, Fő utca"},'
        '{"id": 200002, "title": "Ház XI. kerület", "url": "/h/200002", "price": 70000000},'
        '{"id": 200003, "title": "Ház", "url": "/h/200003", "price": 80000000, "address": "Debrecen"}]}</script>')
    src = ConfiguredSource({"name": "x", "url": "https://x.hu/pest?p={page}"})
    assert hasattr(src, "scrape_area") and not hasattr(src, "scrape")
    items = src.scrape_area([{"name": t} for t in TOWNS])
    assert [(i["source_id"], i["town"]) for i in items] == [("200001", "Budaörs"), ("200002", "Budapest XI.")]


def test_blocked_site_becomes_throttled(fake_site):
    pages, _ = fake_site
    pages["https://x.hu/budaors"] = fetch.Blocked("HTTP 403")
    src = ConfiguredSource({"name": "x", "url": "https://x.hu/{town_slug}"})
    with pytest.raises(Throttled):
        src.scrape({"name": "Budaörs"})


def test_failed_page_becomes_fetch_error(fake_site):
    pages, _ = fake_site
    pages["https://x.hu/budaors"] = fetch.FetchFailed("HTTP 500")
    src = ConfiguredSource({"name": "x", "url": "https://x.hu/{town_slug}"})
    with pytest.raises(FetchError):
        src.scrape({"name": "Budaörs"})


def test_detail_page_adds_location_and_text(fake_site):
    pages, _ = fake_site
    pages["https://x.hu/h/300001"] = (
        '<script type="application/ld+json">{"@type": "House", "url": "https://x.hu/h/300001", "price": 55000000,'
        '"name": "Ház", "geo": {"latitude": 47.5, "longitude": 19.0}, "description": "Szép ház. Tel: 06 30 111 2222"}'
        '</script>')
    src = ConfiguredSource({"name": "x", "url": "https://x.hu/{town_slug}", "details": True})
    info = src.detail({"url": "https://x.hu/h/300001", "source_id": "300001"})
    assert info == {"lat": 47.5, "lon": 19.0, "loc_quality": "exact", "loc_note": "map pin from the listing",
                    "description": "Szép ház. Tel: [phone removed]"}


def test_source_urls_env(monkeypatch):
    import app.sources as sources
    monkeypatch.setattr(sources, "SOURCES_FILE", "/nonexistent/sources.toml")
    monkeypatch.setenv("SOURCE_URLS", "https://www.example.hu/a/{town_slug} https://other.hu/b?p={page}")
    names = [(s.NAME, s.LABEL) for s in sources._configured()]
    assert names == [("example-hu", "example.hu"), ("other-hu", "other.hu")]


def test_sources_file(tmp_path, monkeypatch):
    import app.sources as sources
    f = tmp_path / "sources.toml"
    f.write_text('[[source]]\nname = "a"\nurl = "https://a.hu/{town}"\n\n'
                 '[[source]]\nname = "b"\nurl = "https://b.hu/"\nenabled = false\n')
    monkeypatch.setattr(sources, "SOURCES_FILE", str(f))
    assert [s.NAME for s in sources._configured()] == ["a"]
