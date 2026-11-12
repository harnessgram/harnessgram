"""Where the Studio's data comes from: two functions, `desk()` and `project(id)`.

With `Config.studio_adapter` set, a trusted private Python module supplies these shapes. With no adapter configured
they return the invented sample. Adapter failures return a clearly labelled sample with a bilingual `notice`; its
existing title slot also shows the reason, without changing the page. Exception text and private paths never leave
the loader. Both are read-only: the only thing the Studio writes is the owner's draft marks (marks.py).

The shape (every text is `{"he": ..., "en": ...}` or a plain string; `scene` names one of the kit's placeholder
pictures until a real poster frame exists):

    {"sample": bool,
     "series":    [{"id", "name"}],
     "waiting":   [{"id", "series", "title", "episode", "draft", "length_s", "suggestions", "est_s", "scene"}],
     "working":   [{"id", "series", "title", "stage", "pct", "eta_s", "scene"}],
     "shelf":     [{"id", "series", "title", "status", "ago_s", "scene"}],   # status: ready|writing|producing|published
     "templates": [{"id", "name", "length_s", "format", "scene"}]}          # format: wide|tall

`stage` is one of script, voice, art, captions, checks; `pct` is 0..1; `ago_s` only for published videos.

One video, as the project page ("the desk") shows it (`project(id)`, None when there is no such video):

    {"sample": bool, "id", "series", "title", "episode", "draft", "length_s",
     "scenes":    [{"i", "title", "start", "end", "scene", "frame"}],
     "sentences": [{"i", "scene_i", "start", "end", "said", "scene", "frame", "audio",
                    "suggest", "short", "short_s", "why", "flag", "note"}]}

Scenes and sentences are in order; `i` starts at 1. Sentence IDs retain their original source index when malformed
rows are dropped, so gaps are valid. `start`/`end` are seconds; `scene_i` is the index of the scene a
sentence belongs to (zero-based, unlike `i`). `suggest` is the team's suggestion (`tighten`, `remove` or None) with `why` (a short reason) and,
for `tighten`, `short` (the shorter wording) and `short_s` (its estimated length). `flag` marks a sentence the team
already changed or found a problem in (`visual`: a new picture, `take`: a new recording, `fix`: needs a fix; `note`
says what). Stills: `scene` names a placeholder picture of the kit; `frame` is the engine's real still (`--frame`),
a path under the app's media root, or None until it exists. `audio` is the sentence's own clip under the media root,
or None. The page draws `frame` over the placeholder and plays `audio` when they are there, so wiring the engine
changes only this file.
"""
from __future__ import annotations

from . import strings

DAY = 86400

STAGES = ("script", "voice", "art", "captions", "checks")
STATUSES = ("ready", "writing", "producing", "published")
FORMATS = ("wide", "tall")



SERIES = [
    {"id": "weekly", "name": dict(strings.SAMPLE_TEXT['sample_text_1'])},
    {"id": "stories", "name": dict(strings.SAMPLE_TEXT['sample_text_2'])},
    {"id": "guides", "name": dict(strings.SAMPLE_TEXT['sample_text_3'])},
    {"id": "recaps", "name": dict(strings.SAMPLE_TEXT['sample_text_4'])},
]


def sample() -> dict:
    return {
        "sample": True,
        "series": [dict(s) for s in SERIES],
        "waiting": [
            {"id": "weekly-12", "series": "weekly", "title": dict(strings.SAMPLE_TEXT['sample_text_5']),
             "episode": 12, "draft": 2, "length_s": 68, "suggestions": 3, "est_s": 60, "scene": "q"},
            {"id": "weekly-13", "series": "weekly", "title": dict(strings.SAMPLE_TEXT['sample_text_6']),
             "episode": 13, "draft": 1, "length_s": 74, "suggestions": 1, "est_s": 30, "scene": "bee"},
        ],
        "working": [
            {"id": "stories-3", "series": "stories", "title": dict(strings.SAMPLE_TEXT['sample_text_7']),
             "stage": "captions", "pct": 0.64, "eta_s": 120, "scene": "story"},
        ],
        "shelf": [
            {"id": "recaps-1", "series": "recaps", "title": dict(strings.SAMPLE_TEXT['sample_text_8']), "status": "ready", "scene": "recap"},
            {"id": "guides-1", "series": "guides", "title": dict(strings.SAMPLE_TEXT['sample_text_9']),
             "status": "writing", "scene": "seed"},
            {"id": "weekly-11", "series": "weekly", "title": dict(strings.SAMPLE_TEXT['sample_text_10']),
             "status": "published", "ago_s": 7 * DAY, "scene": "moon"},
            {"id": "weekly-14", "series": "weekly", "title": dict(strings.SAMPLE_TEXT['sample_text_6']),
             "status": "producing", "scene": "bee"},
            {"id": "stories-2", "series": "stories", "title": dict(strings.SAMPLE_TEXT['sample_text_7']),
             "status": "producing", "scene": "story"},
            {"id": "weekly-10", "series": "weekly", "title": dict(strings.SAMPLE_TEXT['sample_text_11']),
             "status": "published", "ago_s": 14 * DAY, "scene": "sun"},
        ],
        "templates": [
            {"id": "weekly", "name": dict(strings.SAMPLE_TEXT['sample_text_1']), "length_s": 60, "format": "wide", "scene": "title"},
            {"id": "story", "name": dict(strings.SAMPLE_TEXT['sample_text_12']), "length_s": 180, "format": "wide", "scene": "story"},
            {"id": "guide", "name": dict(strings.SAMPLE_TEXT['sample_text_13']), "length_s": 30, "format": "tall", "scene": "seed"},
            {"id": "recap", "name": dict(strings.SAMPLE_TEXT['sample_text_14']), "length_s": 90, "format": "wide", "scene": "recap"},
        ],
    }


SUGGESTIONS = ("tighten", "remove")
FLAGS = ("visual", "take", "fix")


from .sample_projects import _bees, _leaves  # noqa: E402


PROJECTS = {"weekly-12": _leaves, "weekly-13": _bees}


def desk() -> dict:
    """The desk as the page shows it (see the module docstring for the shape). Read-only."""
    from . import adapter
    configured, value, reason = adapter.read("desk")
    if configured and reason is None:
        return value
    out = sample()
    if reason:
        out["notice"] = dict(adapter.REASONS[reason])
        out["waiting"][0]["title"] = dict(out["notice"])
    return out


def project(pid: str):
    """One video as the desk page shows it (see the module docstring), or None when there is no such video.
    Read-only."""
    from . import adapter
    if not adapter.ident(pid):
        return None
    configured, value, reason = adapter.read("project", pid)
    if configured and reason is None:
        return value
    make = PROJECTS.get(pid)
    if reason:
        # The requested desk stays open with a labelled sample; marks refuse this fallback.
        out = (make or _leaves)()
        out.update(id=pid, sample=True, notice=dict(adapter.REASONS[reason]))
        out["title"] = dict(out["notice"])
        return out
    if make is None:
        return None
    out = make()
    out["sample"] = True
    return out
