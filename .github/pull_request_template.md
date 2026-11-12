## What changed and why

<!-- Plain English: the problem, then the change. One topic per pull request. -->

## Checklist

- [ ] A test that fails without this change (or a note on why none is possible).
- [ ] `uv run --with pytest --with-editable . python -m pytest -q --ignore=tests/test_live.py` passes.
- [ ] No personal data, token, real chat id or private name in the files, screenshots or commit messages
      (`tests/test_public_hygiene.py` checks the files).
- [ ] User-facing text goes through `src/ctb/i18n.py` in English and Hebrew.
- [ ] If behaviour changed, `README.md` and `CHANGELOG.md` say so.
