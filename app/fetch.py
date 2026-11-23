"""Polite fetching for config-driven sources.

Rules this module enforces, so a configured site is only read the way it allows:
  * robots.txt is checked for every URL; a disallowed URL is never requested.
  * Requests identify themselves honestly (USER_AGENT names this tool and, if set, CONTACT_EMAIL).
  * At least MIN_DELAY seconds between requests to the same host (more if robots.txt asks via
    Crawl-delay or the source config sets `delay`).
  * Bot protection (403/429 challenges, captchas, "are you human" pages) is never worked around:
    the source is stopped for this run with a Blocked error, and the dashboard shows why.
"""
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser

from .common import log

CONTACT = (os.environ.get("CONTACT_EMAIL") or "").strip()
USER_AGENT = (os.environ.get("USER_AGENT") or "").strip() or (
    "househunt/2.0 (+https://github.com/raczcsaba/househunt"
    + (f"; {CONTACT}" if CONTACT else "") + ")")
ROBOTS_TOKEN = "househunt"
MIN_DELAY = 1.5
DEFAULT_DELAY = max(MIN_DELAY, float(os.environ.get("REQUEST_DELAY") or "3"))

_robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
_last_hit: dict[str, float] = {}

# Phrases that mean "a bot check is in the way". Matching one stops the source; we never solve them.
CHALLENGE = re.compile(
    r"cf-chl-|challenge-platform|just a moment\.\.\.|attention required|captcha|"
    r"are you (a )?human|verify you are human|access denied|csak egy gyors ellenőrzés|"
    r"datadome|perimeterx|px-captcha|_incapsula_", re.I)


class Blocked(Exception):
    """The site refuses automated access (robots.txt, bot check, 401/403/429). Stop this source."""


class FetchFailed(Exception):
    """Network error, 404, 5xx. Skip this page; the source may continue."""


def _robots_for(url):
    p = urllib.parse.urlparse(url)
    root = f"{p.scheme}://{p.netloc}"
    if root not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            req = urllib.request.Request(root + "/robots.txt", headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=20) as r:
                rp.parse(r.read().decode("utf-8", "replace").splitlines())
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                rp.disallow_all = True          # robots.txt itself refused: treat as "keep out"
            else:
                rp.allow_all = True             # no robots.txt: everything allowed
        except Exception as e:
            log.warning("robots.txt for %s unreadable (%s); not fetching from it this run", root, e)
            rp = None
        _robots[root] = rp
    return _robots[root]


def allowed(url):
    rp = _robots_for(url)
    return bool(rp) and rp.can_fetch(ROBOTS_TOKEN, url) and rp.can_fetch(USER_AGENT, url)


def _wait(host, delay):
    rp = _robots.get(f"https://{host}") or _robots.get(f"http://{host}")
    crawl = rp.crawl_delay(ROBOTS_TOKEN) if rp else None
    gap = max(MIN_DELAY, delay or DEFAULT_DELAY, float(crawl or 0))
    wait = gap - (time.time() - _last_hit.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    _last_hit[host] = time.time()


def get(url, *, delay=None, accept="text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8"):
    """GET a page politely. Returns the body as text. Raises Blocked or FetchFailed."""
    if not allowed(url):
        raise Blocked(f"robots.txt does not allow {url}")
    host = urllib.parse.urlparse(url).netloc
    for attempt in range(2):
        _wait(host, delay)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept,
                                                       "Accept-Language": "hu,en;q=0.8"})
            with urllib.request.urlopen(req, timeout=45) as r:
                body = r.read().decode(r.headers.get_content_charset() or "utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 429) or e.headers.get("cf-mitigated"):
                raise Blocked(f"HTTP {e.code} from {host}: the site refuses automated access") from e
            if e.code >= 500 and attempt == 0:
                time.sleep(10)
                continue
            raise FetchFailed(f"HTTP {e.code} {url}") from e
        except Exception as e:
            if attempt == 0:
                time.sleep(10)
                continue
            raise FetchFailed(f"{type(e).__name__}: {e} {url}") from e
        check_not_challenge(body, host)
        return body


def check_not_challenge(body, host):
    # Only look at the start of the page: real listings pages can mention "captcha" in a footer script.
    head = body[:6000]
    if len(body) < 60000 and CHALLENGE.search(head):
        raise Blocked(f"{host} showed a bot check; househunt does not work around these")


def render(url, *, delay=None, wait_ms=2500):
    """Load a JavaScript-rendered page in headless Chromium (Playwright) and return its HTML.
    Same robots.txt, pacing and bot-check rules as get(); no stealth plugins or fingerprint tricks."""
    if not allowed(url):
        raise Blocked(f"robots.txt does not allow {url}")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise FetchFailed("render = \"browser\" needs Playwright: pip install playwright && "
                          "playwright install chromium") from e
    host = urllib.parse.urlparse(url).netloc
    _wait(host, delay)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=USER_AGENT, locale="hu-HU")
            resp = page.goto(url, wait_until="domcontentloaded", timeout=60000)
            if resp and resp.status in (401, 403, 429):
                raise Blocked(f"HTTP {resp.status} from {host}: the site refuses automated access")
            page.wait_for_timeout(wait_ms)
            body = page.content()
        finally:
            browser.close()
    check_not_challenge(body, host)
    return body
