"""Finding listings in pages: JSON-LD, embedded JSON, CSS selectors and the extract() driver."""
from app import extract

PAGE = "https://www.example.hu/elado-haz/budaors"
SELECTORS = {"item": "article.card", "url": "a.link@href", "title": "h2", "price": ".price", "size": ".area",
             "plot": ".plot", "rooms": ".rooms", "address": ".location"}


def test_jsonld_finds_listings_in_graph_and_offers(fixture_html):
    items = extract.from_jsonld(fixture_html("jsonld.html"), PAGE)
    assert [i["source_id"] for i in items] == ["1234567", "7654321"]
    house, offer = items
    assert house["url"] == "https://www.example.hu/hirdetes/1234567"
    assert house["price"] == 89_900_000
    assert (house["size"], house["plot"], house["rooms"]) == (120, 650, 4.0)
    assert house["address"] == "Kossuth utca 1., Budaörs"
    assert (house["lat"], house["lon"], house["loc_quality"]) == (47.4621, 18.9530, "exact")
    assert "[phone removed]" in house["description"] and "123 4567" not in house["description"]
    # an Offer wrapping the house in itemOffered
    assert offer["title"] == "Felújítandó ház"
    assert (offer["price"], offer["size"], offer["rooms"]) == (45_900_000, 75, 2.5)


def test_jsonld_ignores_pages_without_listings():
    html = '<script type="application/ld+json">{"@type": "WebSite", "url": "https://x.hu/", "name": "X"}</script>'
    assert extract.from_jsonld(html, PAGE) == []
    assert extract.from_jsonld("<html>no data</html>", PAGE) == []


def test_embedded_next_data(fixture_html):
    items = extract.from_embedded(fixture_html("next_data.html"), PAGE)
    assert [i["source_id"] for i in items] == ["9000001", "9000002", "9000003"]
    first = items[0]
    assert first["url"] == "https://www.example.hu/ingatlan/9000001"
    assert first["title"] == "Kertes ház Törökbálinton"
    assert (first["price"], first["size"], first["plot"], first["rooms"]) == (72_500_000, 110, 900, 3.5)
    assert first["address"] == "Törökbálint"
    assert (first["lat"], first["lon"]) == (47.4357, 18.9134)


def test_embedded_window_state_with_id_template(fixture_html):
    html = fixture_html("window_state.html")
    assert extract.from_embedded(html, PAGE) == []                      # ids but no links: needs item_url
    items = extract.from_embedded(html, PAGE, "https://www.example.hu/hirdetes/{id}")
    assert [(i["url"], i["price"], i["size"]) for i in items] == [
        ("https://www.example.hu/hirdetes/A-55501", 59_900_000, 101),
        ("https://www.example.hu/hirdetes/A-55502", 61_500_000, 99),
    ]


def test_embedded_prefers_the_list_that_looks_most_like_listings():
    html = """<script type="application/json">{
      "menu": [{"title": "A", "url": "/a"}, {"title": "B", "url": "/b"}],
      "promo": [{"title": "Hitel", "url": "/hitel", "price": 1}, {"title": "Biztosítás", "url": "/b"}],
      "ads": [{"id": 111111, "title": "Ház 1", "url": "/h/111111", "price": 50000000},
              {"id": 222222, "title": "Ház 2", "url": "/h/222222", "price": 60000000},
              {"id": 333333, "title": "Ház 3", "url": "/h/333333", "price": 70000000}]}</script>"""
    items = extract.from_embedded(html, PAGE)
    assert [i["source_id"] for i in items] == ["111111", "222222", "333333"]


def test_selectors(fixture_html):
    items = extract.from_selectors(fixture_html("cards.html"), PAGE, SELECTORS)
    assert len(items) == 2                                              # the empty ad slot is skipped
    a, b = items
    assert a == {"source_id": "445566", "url": "https://www.example.hu/hirdetes/445566",
                 "title": "Családi ház, Pomáz", "price": 54_900_000, "size": 96, "plot": 720, "rooms": 3.0,
                 "address": "Pomáz, Mártírok útja"}
    assert b["source_id"] == "445567" and b["price"] == 79_000_000 and "plot" not in b


def test_selectors_deduplicate_repeated_cards(fixture_html):
    html = fixture_html("cards.html")
    html = html.replace("</section>", html.split('<section class="results">')[1])
    items = extract.from_selectors(html, PAGE, SELECTORS)
    assert [i["source_id"] for i in items] == ["445566", "445567"]


def test_extract_tries_methods_in_order(fixture_html):
    assert extract.extract(fixture_html("jsonld.html"), PAGE)[1] == "jsonld"
    assert extract.extract(fixture_html("next_data.html"), PAGE)[1] == "embedded"
    items, method = extract.extract(fixture_html("cards.html"), PAGE, {"selectors": SELECTORS})
    assert method == "selectors" and len(items) == 2


def test_extract_forced_method(fixture_html):
    html = fixture_html("cards.html")
    assert extract.extract(html, PAGE, {"extract": "jsonld", "selectors": SELECTORS}) == ([], None)
    assert extract.extract(html, PAGE, {"extract": "selectors", "selectors": SELECTORS})[1] == "selectors"
    assert extract.extract(fixture_html("jsonld.html"), PAGE, {"extract": "selectors"}) == ([], None)


def test_extract_without_listings_or_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert not extract.llm_enabled()
    assert extract.extract("<html><body><p>Nincs találat</p></body></html>", PAGE) == ([], None)


def test_llm_switch(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    assert extract.llm_enabled()
    monkeypatch.setenv("CLAUDE_ENABLED", "0")
    assert not extract.llm_enabled()


def test_page_text_keeps_links_and_drops_scripts(fixture_html):
    text = extract.page_text(fixture_html("cards.html") + "<script>var secret = 1;</script>")
    assert "[Részletek](/hirdetes/445566)" in text
    assert "54,9 M Ft" in text
    assert "secret" not in text


def test_embedded_single_result_page():
    """The last results page, or a small town, often has just one listing."""
    html = ('<script>window.__NUXT__ = {"data": [{"ads": [{"id": 444444, "title": "Az egyetlen ház",'
            ' "url": "/h/444444", "price": "39 900 000 Ft", "size": "80 m²"}]}]}</script>')
    items = extract.from_embedded(html, PAGE)
    assert [(i["source_id"], i["price"], i["size"]) for i in items] == [("444444", 39_900_000, 80)]
