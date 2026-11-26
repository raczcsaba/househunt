# Contributing

Issues and pull requests are welcome.

- Set up with `pip install -r requirements-dev.txt` (Python 3.11+).
- Run `pytest` and `ruff check .` before opening a pull request; CI runs both, plus a Docker build.
- Tests must not use the network. Use a small HTML fixture in `tests/fixtures/` or the local test server
  in `tests/test_fetch.py`.
- Don't add fixtures copied from real property portals, and never commit seller names, phone numbers or
  other personal data.
- Site-specific scrapers belong in your own `plugins/` folder, not in this repository. Changes to
  `app/fetch.py` must keep its rules: robots.txt, honest User-Agent, pacing, and stopping at bot checks.
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/) (`fix: ...`, `test: ...`).
