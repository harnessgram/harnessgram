# Cockpit browser tests

Install the Chromium headless shell with the same Playwright release used by
the tests. Playwright selects a specific browser revision for each release.

```sh
uv run --with-requirements tests/requirements-browser.txt python -m playwright install --only-shell chromium
```

Run the complete offline suite with browser coverage required:

```sh
CTB_REQUIRE_BROWSER=1 uv run -q --with pytest --with pytest-timeout --with pytest-xdist \
  --with-requirements tests/requirements-browser.txt --with pillow --with-editable . \
  python -m pytest -q --timeout 60 -n 3 tests --ignore=tests/test_live.py
```

With `CTB_REQUIRE_BROWSER=1`, a missing Playwright package or a Chromium launch
error fails the run. Without it, the browser tests can be skipped on machines
set up only for unit tests. Use `-rs` to show skip reasons.
