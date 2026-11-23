"""Template for a listing-source plugin. Copy to plugins/<yoursite>.py and edit; files named example*
are not loaded."""
from app import extract, fetch
from app.common import FetchError, Throttled, slug

NAME = "yoursite"
LABEL = "Your Site"
SEARCH = "https://www.your-site.hu/houses/{town}?page={page}"


def scrape(town):
    items = []
    for page in range(1, 11):
        url = SEARCH.format(town=slug(town["name"]), page=page)
        try:
            html = fetch.get(url, delay=5)            # robots.txt, pacing, bot-check detection
        except fetch.Blocked as e:
            raise Throttled(str(e)) from e
        except fetch.FetchFailed as e:
            raise FetchError(str(e)) from e
        found, _ = extract.extract(html, url)         # JSON-LD / embedded data / Claude
        if not found:
            break
        items += [dict(it, source=NAME, town=town["name"]) for it in found]
    return items
