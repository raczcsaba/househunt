"""Value parsing and personal-data stripping in app/extract.py."""
import pytest

from app.extract import clean_text, finish, parse_area, parse_price, parse_rooms

PAGE = "https://www.example.hu/elado-haz/budaors"


@pytest.mark.parametrize("raw, expected", [
    (45900000, 45_900_000),
    (45.9, 45_900_000),                      # a small number means millions
    (129.99, 129_990_000),
    ("45 900 000 Ft", 45_900_000),
    ("45\xa0900\xa0000 Ft", 45_900_000),     # non-breaking spaces
    ("45 900 000 Ft", 45_900_000), # narrow non-breaking spaces
    ("45.900.000 Ft", 45_900_000),           # dots as thousands separators
    ("45,9 M Ft", 45_900_000),               # Hungarian decimal comma
    ("45,9 MFt", 45_900_000),
    ("45.9 millió", 45_900_000),
    ("89.9 M Ft", 89_900_000),
    ("129 M", 129_000_000),
    ("Ár: 89 990 000 Ft", 89_990_000),
    ("45 900 000,- Ft", 45_900_000),
    ("1 250 000 000 Ft", 1_250_000_000),
    ("1,2 milliárd Ft", 1_200_000_000),
    ("1,2 Mrd Ft", 1_200_000_000),
])
def test_parse_price(raw, expected):
    assert parse_price(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "nincs ár", "Ár megegyezés szerint", True, False, 0, "0"])
def test_parse_price_rejects_non_prices(raw):
    assert parse_price(raw) is None


@pytest.mark.parametrize("raw, expected", [
    (120, 120),
    (95.5, 95),
    ("120 m²", 120),
    ("1 200 m2", 1200),
    ("1\xa0200 m²", 1200),
    ("1.200 m²", 1200),                      # thousands separator
    ("95.5 m²", 95),                         # decimal point, not 955
    ("95,5 m²", 95),
    ("1 200.5 m2", 1200),
    ({"value": 95, "unitCode": "MTK"}, 95),
    ({"amount": "650 m²"}, 650),
])
def test_parse_area(raw, expected):
    assert parse_area(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "nincs adat", True, {}])
def test_parse_area_rejects_non_areas(raw):
    assert parse_area(raw) is None


@pytest.mark.parametrize("raw, expected", [
    (3, 3.0),
    (2.5, 2.5),
    ("4", 4.0),
    ("4 szoba", 4.0),
    ("2,5", 2.5),
    ("3 + 2 fél", 4.0),
    ("1 + 1 fél szoba", 1.5),
])
def test_parse_rooms(raw, expected):
    assert parse_rooms(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "sok", True])
def test_parse_rooms_rejects_non_rooms(raw):
    assert parse_rooms(raw) is None


@pytest.mark.parametrize("phone", [
    "+36 30 123 4567",
    "+36301234567",
    "06301234567",
    "06-20/123-4567",
    "06 1 234 5678",
    "+36 (30) 123 4567",
    "+36-30-123-45-67",
    "0630 123 4567",
])
def test_clean_text_removes_phone_numbers(phone):
    out = clean_text(f"Hívjon most: {phone}, köszönöm!")
    assert out == "Hívjon most: [phone removed], köszönöm!"


@pytest.mark.parametrize("text", [
    "Ár: 36 900 000 Ft",
    "2006 300 000 Ft-ért eladó",
    "36 m² nappali, 2020-ban épült",
    "Telek: 1 200 m², 06. hónapban költözhető",
])
def test_clean_text_keeps_prices_and_years(text):
    assert clean_text(text) == text


def test_clean_text_strips_html():
    assert clean_text("<p>Szép ház<br>nagy kert&nbsp;&amp; garázs</p>") == "Szép ház\nnagy kert & garázs"
    assert clean_text(None) is None
    assert clean_text("") is None


def test_finish_normalises_a_listing():
    item = finish({"url": "/hirdetes/1234567?ref=x", "title": "<b>Ház</b>", "price": "45,9 M Ft",
                   "size": "120 m²", "plot": "800 m²", "rooms": "3 + 1 fél", "address": "Budaörs",
                   "description": "Hívjon: 06 30 123 4567", "lat": "47.46", "lon": "18.95"}, PAGE)
    assert item == {
        "source_id": "1234567", "url": "https://www.example.hu/hirdetes/1234567?ref=x", "title": "Ház",
        "price": 45_900_000, "size": 120, "plot": 800, "rooms": 3.5, "address": "Budaörs",
        "description": "Hívjon: [phone removed]", "lat": 47.46, "lon": 18.95,
        "loc_quality": "exact", "loc_note": "map pin from the listing",
    }


def test_finish_drops_coordinates_outside_hungary():
    item = finish({"url": "/hirdetes/1234567", "price": 50_000_000, "lat": 52.52, "lon": 13.40}, PAGE)
    assert "lat" not in item and "loc_quality" not in item


def test_finish_hashes_urls_without_an_id():
    a = finish({"url": "/haz/szep-haz-budaors", "price": 50_000_000}, PAGE)
    b = finish({"url": "/haz/szep-haz-budaors?utm=1#top", "price": 50_000_000}, PAGE)
    assert len(a["source_id"]) == 16 and a["source_id"] == b["source_id"]


def test_finish_builds_url_from_id_template():
    item = finish({"source_id": 42, "price": 50_000_000}, PAGE, id_from="https://www.example.hu/h/{id}")
    assert item["url"] == "https://www.example.hu/h/42"


def test_finish_rejects_empty_items():
    assert finish({"url": "/hirdetes/1234567"}, PAGE) is None          # nothing about the house
    assert finish({"price": 50_000_000}, PAGE) is None                 # no link and no id
