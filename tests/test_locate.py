"""Finding a street in listing text (app/locate.py), used for geocoding."""
import pytest

from app.locate import street_from_text


@pytest.mark.parametrize("text, street", [
    ("Budaörs, Kossuth Lajos utca 12", "Kossuth Lajos utca 12"),
    ("Eladó ház a Kossuth Lajos utca 12 alatt", "Kossuth Lajos utca 12"),
    ("Szép ház Budaörsön, Petőfi Sándor u. 3", "Petőfi Sándor utca 3"),
    ("Csendes, Ady Endre út 5.", "Ady Endre út 5"),
    ("Pomáz, Szentendrei út", "Szentendrei út"),
    ("A Május 1. utca közelében", "Május 1. utca"),
    ("Fő tér mellett", "Fő tér"),
    ("Közel a Nagy körúthoz", None),
    ("Csendes utcában, jó helyen", None),
    ("", None),
])
def test_street_from_text(text, street):
    assert street_from_text(text) == street


def test_street_from_text_takes_the_first_text_with_a_street():
    assert street_from_text(None, "Ház eladó", "Az Arany János utca végén") == "Arany János utca"
