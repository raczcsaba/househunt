# Plugins

Put a Python file here when a site needs more than `config/sources.toml` can express. Files in this
folder (except this README and `example_site.py`) are git-ignored, so your plugins stay on your machine.

**Plugins are your own code for your own use.** househunt's built-in sources respect robots.txt and stop
at bot checks; a plugin is not checked by any of that. Read the site's terms of use first, keep request
rates low, and don't collect personal data.

## A listing source

```python
NAME = "mysite"                 # stored with each listing
LABEL = "My Site"               # shown on the dashboard

def scrape(town: dict) -> list[dict]:
    """Listings for one town. town has 'name', 'centre' [lat, lon], 'district' for Budapest."""
    return [dict(source=NAME, source_id="123", town=town["name"], url="https://...",
                 title="...", price=45_900_000, size=110, plot=600, rooms=4, address="...")]

# Optional:
# def scrape_area(towns) -> list[dict]        one search for every town instead of scrape(town)
# def scrape_budapest(districts) -> list      one search for all Budapest districts
# def detail(row) -> dict                     extra fields (lat, lon, description...) for one listing
```

Use `app.fetch.get(url)` for requests if you want the same robots.txt, pacing and bot-check rules as
the built-in sources. Raise `app.common.FetchError` to skip a town, `app.common.Throttled` to stop the
source for this run.

## A job plugin

A plugin with `JOB = True` runs on its own schedule and gets its own dashboard button. It defines
`scrape_all(towns, data_dir, site_dir) -> (results, errors)` and `store(con, results, errors, day) -> stats`.
Schedule: `<NAME>_SCHEDULE` (cron) and `<NAME>_ENABLED`, or set `SCHEDULE_ENV` / `ENABLED_ENV` /
`SCHEDULE_DEFAULT` in the module.
