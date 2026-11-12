# Contributing

Thanks for helping. Bug reports, small fixes and translations of the bot's menus are all welcome.

## Before you start

- For anything larger than a small fix, open an issue first and describe the problem and the change you have in
  mind, so the design can be agreed before you spend time on it.
- Security problems go through private reporting, not issues: see [SECURITY.md](SECURITY.md).

## Development setup

The core uses only the Python standard library (3.9+). Tests need `pytest`, and the tmux tests need `tmux`.

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/python -m pytest -q --ignore=tests/test_live.py
```

or with uv:

```bash
uv run --with pytest --with-editable . python -m pytest -q --ignore=tests/test_live.py
```

The tests use no network (a local HTTP server stands in for Telegram), no real home folder and never your live
tmux server or Claude Code. `tests/test_live.py` is an older set that CI leaves out; run it on its own when you
change live progress, receipts or the footer.

## Rules for changes

- **Standard library only in the core.** Optional features may use optional extras (`pyproject.toml`), and must
  stay off and silent until configured.
- **Never log or print a secret.** Tokens and keys come from the config or from a command, stay in memory and
  never reach a command line, a log, Telegram or an exception message.
- **No personal data in the tree.** Use made-up names, `example.com` emails and placeholder group ids such as
  `-1001234567890`. `tests/test_public_hygiene.py` checks this.
- **User-facing text goes through `src/ctb/i18n.py`**, in English and Hebrew. Keep Hebrew lines free of long
  English runs: put code, links and long English on their own line.
- **One topic per pull request**, with a test that fails without the change. Run the full suite before you push.
- Write commit messages and pull requests in plain English: what changed and why.

## Licence

By contributing you agree that your contribution is released under the project's [MIT licence](LICENSE).
