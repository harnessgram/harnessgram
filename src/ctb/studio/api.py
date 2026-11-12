"""The Studio's API command: the panel runs it once per call (never through a shell) with one JSON object on stdin
and reads one JSON object from stdout.

Ops:
- `desk.get` -> `{"ok", "desk"}`: the home screen (read-only).
- `project.get` `{project}` -> `{"ok", "project", "marks"}`: one video's scenes and sentences, with the owner's draft
  marks (read-only).
- `marks.set` `{project, i, mark: keep|tighten|remove|clear, text?}` -> `{"ok", "marks"}`: the one write. It stores a
  local draft mark in the app's own `state/` folder (marks.py); nothing is rendered, sent or published.

Anything else, or a body that is not an object with a string `op`, is refused with `{"ok": false, "error": ...}` (the
panel still answers 200: the page maps the error to a state). Errors are plain codes, never a traceback or a path:
`bad_request`, `unknown_op`, `not_found`, `too_large`, `unavailable`.

Run it with `python -m ctb.studio.api` (read-only then: without the app folder `marks.set` answers `unavailable`);
the installed app folder holds a one-line wrapper that hands its folder on.
"""
from __future__ import annotations

import json
import sys

MAX_IN = 16 * 1024  # the panel never sends more (cockpit_apps.API_BODY)


def handle(body: object, folder=None) -> dict:
    if not isinstance(body, dict) or not isinstance(body.get("op"), str):
        return {"ok": False, "error": "bad_request"}
    op = body["op"]
    if op == "desk.get":
        from . import provider
        try:
            return {"ok": True, "desk": provider.desk()}
        except Exception:
            return {"ok": False, "error": "unavailable"}
    if op == "project.get":
        from . import marks, provider
        pid = body.get("project")
        if not isinstance(pid, str) or not marks.PROJECT_ID.match(pid):
            return {"ok": False, "error": "bad_request"}
        try:
            proj = provider.project(pid)
        except Exception:
            return {"ok": False, "error": "unavailable"}
        if proj is None:
            return {"ok": False, "error": "not_found"}
        try:
            got = marks.get(folder, pid)
        except Exception:
            got = {}  # the draft marks are a convenience: the video still opens without them
        return {"ok": True, "project": proj, "marks": got}
    if op == "marks.set":
        from . import marks
        try:
            return {"ok": True, "marks": marks.set_mark(folder, body.get("project"), body.get("i"), body.get("mark"),
                                                       body.get("text"))}
        except marks.MarkError as e:
            return {"ok": False, "error": e.code}
        except Exception:
            return {"ok": False, "error": "unavailable"}
    return {"ok": False, "error": "unknown_op"}


def main(stdin=None, stdout=None, folder=None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    raw = stdin.read(MAX_IN + 1)
    try:
        body = json.loads(raw) if raw and len(raw) <= MAX_IN else None
    except ValueError:
        body = None
    stdout.write(json.dumps(handle(body, folder), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    from . import adapter
    adapter.limit_memory()
    sys.exit(main())
