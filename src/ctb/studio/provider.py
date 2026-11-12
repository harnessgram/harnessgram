"""Where the Studio's data comes from: two functions, `desk()` and `project(id)`.

Today they return a small invented sample (a few generic videos, so the screens and their states can be built and
checked end to end). A later step replaces their bodies with what the video studio keeps on disk and keeps the shapes
below, so nothing in the page changes. The page shows a notice whenever `sample` is true, so a sample is never mistaken
for the real desk. Both are read-only: the only thing the Studio writes is the owner's draft marks (marks.py).

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

Scenes and sentences are in order; `i` counts from 1; `start`/`end` are seconds; `scene_i` is the index of the scene a
sentence belongs to. `suggest` is the team's suggestion (`tighten`, `remove` or None) with `why` (a short reason) and,
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
    return sample()


def project(pid: str):
    """One video as the desk page shows it (see the module docstring), or None when there is no such video.
    Read-only."""
    make = PROJECTS.get(pid) if isinstance(pid, str) else None
    if make is None:
        return None
    out = make()
    out["sample"] = True
    return out
