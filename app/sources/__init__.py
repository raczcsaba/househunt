"""Where listings come from.

1. Config-driven sources: config/sources.toml (path in SOURCES_FILE), or simply a list of search URLs in
   SOURCE_URLS. No code needed; see config/sources.example.toml.
2. Plugins: any *.py file in PLUGINS_DIR (default ./plugins) defining NAME and scrape(town) or
   scrape_area(towns), and optionally detail(row) / scrape_budapest(districts). A plugin that instead
   defines JOB = True plus scrape_all(towns, data_dir, site_dir) and store(con, results, errors, day)
   runs as its own scheduled job (e.g. a browser-based source). See plugins/README.md.
Plugins are your own code for your own use: you are responsible for following each site's terms.
"""
import importlib.util
import os
import re
import sys
import tomllib
import urllib.parse

from ..common import log
from .configured import ConfiguredSource

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
SOURCES_FILE = os.environ.get("SOURCES_FILE") or os.path.join(ROOT, "config", "sources.toml")
PLUGINS_DIR = os.environ.get("PLUGINS_DIR") or os.path.join(ROOT, "plugins")


def _configured():
    out = []
    if os.path.exists(SOURCES_FILE):
        with open(SOURCES_FILE, "rb") as f:
            for cfg in tomllib.load(f).get("source", []):
                if cfg.get("enabled", True):
                    out.append(ConfiguredSource(cfg))
    for url in re.split(r"[\s;]+", os.environ.get("SOURCE_URLS") or ""):
        if url:
            host = urllib.parse.urlparse(url).netloc.removeprefix("www.")
            out.append(ConfiguredSource({"name": re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-"),
                                         "label": host, "url": url}))
    return out


def _plugins():
    mods = []
    if not os.path.isdir(PLUGINS_DIR):
        return mods
    if PLUGINS_DIR not in sys.path:
        sys.path.insert(0, PLUGINS_DIR)
    for fn in sorted(os.listdir(PLUGINS_DIR)):
        if not fn.endswith(".py") or fn.startswith(("_", "example")):
            continue
        name = fn[:-3]
        try:
            spec = importlib.util.spec_from_file_location(f"househunt_plugin_{name}", os.path.join(PLUGINS_DIR, fn))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mods.append(mod)
        except Exception as e:
            log.warning("plugin %s failed to load: %s", fn, e)
    return mods


ALL, JOB_PLUGINS = {}, {}
for _src in _configured() + _plugins():
    if getattr(_src, "JOB", False):
        JOB_PLUGINS[_src.NAME] = _src
    elif _src.NAME in ALL:
        log.warning("two sources are named %r; keeping the first", _src.NAME)
    else:
        ALL[_src.NAME] = _src
if not ALL and not JOB_PLUGINS:
    log.warning("no listing sources configured: copy config/sources.example.toml to config/sources.toml "
                "or set SOURCE_URLS")
