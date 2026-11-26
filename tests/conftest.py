"""Shared test setup. Runs before any app module is imported, so the app never sees local sources,
plugins or API keys, and no test touches the network."""
import os
import pathlib
import tempfile

_EMPTY = tempfile.mkdtemp(prefix="househunt-tests-")
os.environ["PLUGINS_DIR"] = _EMPTY
os.environ["SOURCES_FILE"] = os.path.join(_EMPTY, "sources.toml")
os.environ["SOURCE_URLS"] = ""
os.environ["CONTACT_EMAIL"] = ""
os.environ.pop("USER_AGENT", None)
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

import pytest  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture_html():
    def load(name):
        return (FIXTURES / name).read_text(encoding="utf-8")
    return load
