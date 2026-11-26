"""Polite fetching in app/fetch.py against a local HTTP server (no internet needed)."""
import http.server
import threading

import pytest

from app import fetch

ROBOTS = """User-agent: *
Disallow: /private/
Crawl-delay: 4
"""
# A group naming househunt replaces the * group for it (RFC 9309), so /private/ is allowed here.
ROBOTS_NAMED = """User-agent: *
Disallow: /private/

User-agent: househunt
Disallow: /no-bots/
"""
LISTINGS = "<html><body>" + "<p>Eladó ház</p>" * 50 + "</body></html>"


class Site:
    """A tiny scriptable website: routes map a path to (status, headers, body), or to a function returning it."""

    def __init__(self):
        self.routes = {}
        self.hits = []
        site = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                site.hits.append((self.path, self.headers.get("User-Agent")))
                route = site.routes.get(self.path, (404, {}, "not found"))
                status, headers, body = route() if callable(route) else route
                data = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def paths(self):
        return [p for p, _ in self.hits]


@pytest.fixture
def site():
    s = Site()
    yield s
    s.server.shutdown()
    s.server.server_close()


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    """Empty robots/pacing caches, and record sleeps instead of sleeping."""
    monkeypatch.setattr(fetch, "_robots", {})
    monkeypatch.setattr(fetch, "_robots_at", {})
    monkeypatch.setattr(fetch, "_last_hit", {})
    slept = []
    monkeypatch.setattr(fetch.time, "sleep", slept.append)
    return slept


def test_user_agent_identifies_the_tool():
    assert fetch.USER_AGENT.startswith("househunt/")
    assert "github.com/raczcsaba/househunt" in fetch.USER_AGENT


def test_robots_rules_are_obeyed(site):
    site.routes["/robots.txt"] = (200, {}, ROBOTS)
    assert fetch.allowed(site.url + "/elado-haz/budaors")
    assert not fetch.allowed(site.url + "/private/x")
    assert site.paths() == ["/robots.txt"]                        # read once, then cached


def test_robots_group_naming_househunt_wins(site):
    site.routes["/robots.txt"] = (200, {}, ROBOTS_NAMED)
    assert not fetch.allowed(site.url + "/no-bots/x")
    assert fetch.allowed(site.url + "/private/x")


def test_disallowed_url_is_never_requested(site):
    site.routes["/robots.txt"] = (200, {}, ROBOTS)
    site.routes["/private/x"] = (200, {}, LISTINGS)
    with pytest.raises(fetch.Blocked, match="robots.txt"):
        fetch.get(site.url + "/private/x")
    assert "/private/x" not in site.paths()


def test_missing_robots_allows_everything(site):
    site.routes["/page"] = (200, {}, LISTINGS)
    assert fetch.get(site.url + "/page") == LISTINGS
    assert site.paths() == ["/robots.txt", "/page"]
    assert site.hits[-1][1] == fetch.USER_AGENT


@pytest.mark.parametrize("status", [401, 403])
def test_forbidden_robots_means_keep_out(site, status):
    site.routes["/robots.txt"] = (status, {}, "no")
    assert not fetch.allowed(site.url + "/page")


def test_robots_server_error_blocks_for_now_and_is_retried(site, monkeypatch):
    site.routes["/robots.txt"] = (503, {}, "down")
    assert not fetch.allowed(site.url + "/page")
    site.routes["/robots.txt"] = (200, {}, ROBOTS)
    assert not fetch.allowed(site.url + "/page")                  # failure is cached for a while...
    now = fetch.time.time()
    monkeypatch.setattr(fetch.time, "time", lambda: now + fetch.ROBOTS_RETRY + 1)
    assert fetch.allowed(site.url + "/page")                      # ...then robots.txt is read again


def test_robots_is_refreshed_after_ttl(site, monkeypatch):
    site.routes["/robots.txt"] = (200, {}, "User-agent: *\nDisallow:\n")
    assert fetch.allowed(site.url + "/page")
    site.routes["/robots.txt"] = (200, {}, "User-agent: *\nDisallow: /\n")
    assert fetch.allowed(site.url + "/page")
    now = fetch.time.time()
    monkeypatch.setattr(fetch.time, "time", lambda: now + fetch.ROBOTS_TTL + 1)
    assert not fetch.allowed(site.url + "/page")


def test_crawl_delay_and_minimum_gap_are_honoured(site, fresh_state):
    site.routes["/robots.txt"] = (200, {}, ROBOTS)
    site.routes["/a"] = (200, {}, LISTINGS)
    fetch.get(site.url + "/a", delay=2)
    fetch.get(site.url + "/a", delay=2)
    assert fresh_state and max(fresh_state) > 3.5                 # Crawl-delay: 4 beats delay=2


@pytest.mark.parametrize("status, headers", [(403, {}), (429, {"Retry-After": "60"}), (401, {}),
                                             (503, {"cf-mitigated": "challenge"})])
def test_refusals_stop_the_source(site, status, headers):
    site.routes["/page"] = (status, headers, "nope")
    with pytest.raises(fetch.Blocked, match=f"HTTP {status}"):
        fetch.get(site.url + "/page")
    assert site.paths().count("/page") == 1                       # never retried


def test_not_found_is_a_plain_failure(site):
    with pytest.raises(fetch.FetchFailed, match="HTTP 404"):
        fetch.get(site.url + "/missing")


def test_server_error_is_retried_once(site, fresh_state):
    answers = iter([(500, {}, "oops"), (200, {}, LISTINGS)])
    site.routes["/flaky"] = lambda: next(answers)
    assert fetch.get(site.url + "/flaky") == LISTINGS
    assert site.paths().count("/flaky") == 2
    assert 10 in fresh_state                                       # backed off before retrying


def test_repeated_server_errors_fail(site):
    site.routes["/down"] = (502, {}, "bad gateway")
    with pytest.raises(fetch.FetchFailed, match="HTTP 502"):
        fetch.get(site.url + "/down")
    assert site.paths().count("/down") == 2


@pytest.mark.parametrize("page", [
    "<html><head><title>Just a moment...</title></head><body>cf-chl-bypass</body></html>",
    "<html><body><h1>Attention Required! | Cloudflare</h1></body></html>",
    "<html><body>Please verify you are human</body></html>",
    "<html><body><div id='px-captcha'></div></body></html>",
    "<html><body>Csak egy gyors ellenőrzés</body></html>",
])
def test_bot_checks_are_detected(site, page):
    site.routes["/page"] = (200, {}, page)
    with pytest.raises(fetch.Blocked, match="bot check"):
        fetch.get(site.url + "/page")


def test_captcha_mention_deep_in_a_large_page_is_fine():
    body = "<html><body>" + "<p>Eladó ház</p>" * 5000 + "<script>loadCaptcha()</script></body></html>"
    fetch.check_not_challenge(body, "example.hu")                # does not raise


def test_render_respects_robots(site):
    site.routes["/robots.txt"] = (200, {}, "User-agent: *\nDisallow: /\n")
    with pytest.raises(fetch.Blocked):
        fetch.render(site.url + "/page")
