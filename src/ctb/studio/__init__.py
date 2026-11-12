"""The Studio: a Cockpit app for directing videos from the phone (see docs/studio.md).

It is an ordinary Cockpit app (cockpit_apps.py): a page in the panel's sandboxed frame plus a command the panel runs
for each API call. Nothing in the panel knows about it; `harnessgram studio install` writes the app folder.

- `strings.py`  the words, one table per screen (Hebrew and English)
- `kit.css`, `sprite.svg`  the brand kit: the Cockpit's tokens plus the Studio layer, icons and scene placeholders
- `app.js`, `page.py`  the page: shell, home screen and its states
- `provider.py`  where the desk's data comes from (a small sample today, one function to replace)
- `api.py`  the command the panel runs: read-only, one JSON object in, one out
- `install.py`  writes the app folder under the Cockpit apps directory
"""
