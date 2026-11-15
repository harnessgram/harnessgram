"""Ideas waiting on a Cockpit v2 project page: the presentation's rows and the page's exact action payload.

The node half runs the page's real functions on the fake DOM; layout stays with the browser tests.
"""
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from ctb import cockpit_v2, panel_page
from cockpit_v2_browser import sample_state
from test_panel_page import func

NODE = shutil.which("node")
HERE = Path(__file__).parent
PRELUDE = (HERE / "studio_fake_dom.js").read_text() + (HERE / "cockpit_v2_fake_dom.js").read_text()
ROOT = Path(panel_page.__file__).parent


def test_presentation_lists_open_items_older_than_a_day_with_the_lead():
    state = sample_state("en")
    projects = {p["key"]: p for p in state["cockpit_v2"]["projects"]}
    first = projects["planning"]
    assert [i["id"] for i in first["ideas"]] == [4] and first["ideas"][0]["title"] == "Older request"
    assert first["ideas"][0]["wait"] == "owner" and first["ideas"][0]["age"] == 400000
    assert first["lead"] == "sample00" and first["ideas_more"] == 0
    assert all(p["ideas"] == [] and p["lead"] == "" for k, p in projects.items() if k != "planning")


def test_configured_lead_wins_and_ended_sessions_are_never_asked():
    state = sample_state("en")
    now = state["now"]
    for n in range(10):
        state["requests"].append(dict(project="Planning", id=10 + n, title=f"Idea {n}", status="open", wait="team",
                                      age=90000 + n, updated=now, older=False, note="", steps=[]))
    state["sessions"].append(dict(id="sample09", name="Planning: lead", state="idle", age=9000, now="", summary="",
                                  url="", context=0, model="", account="", bg=0))
    entries = [SimpleNamespace(session=s["id"], chat=1, name=s["name"], state=s["state"]) for s in state["sessions"]]
    cfg = SimpleNamespace(projects=[], brief={}, idle_lines={"leads": {"planning": "sample09"}})
    p = {x["key"]: x for x in cockpit_v2.presentation(cfg, state, entries, now)["projects"]}["planning"]
    assert p["lead"] == "sample09" and len(p["ideas"]) == cockpit_v2.IDEAS_SHOWN and p["ideas_more"] == 5
    assert p["ideas"][0]["id"] == 4  # the oldest first
    for s in state["sessions"]:
        s["state"] = "ended"
    p = {x["key"]: x for x in cockpit_v2.presentation(cfg, state, entries, now)["projects"]}["planning"]
    assert p["lead"] == ""


def run(body, lang="en", mutate=None):
    script = (ROOT / "cockpit_v2.js").read_text().replace("__V2_STRINGS__", json.dumps(panel_page.V2_STRINGS, ensure_ascii=False))
    script = "\n".join(func(n) for n in ("el", "icon", "svgAdd", "he", "shown", "compactStorageKey", "compactBlock",
                                         "requestAction", "requestRow", "v2ModeRow", "settingsBox", "sendId")) + script
    script += "\nvar sends={};\n" + (ROOT / "cockpit_v2_screens.js").read_text()
    state = sample_state(lang)
    if mutate:
        mutate(state)
    src = PRELUDE + "\nbootV2(%s,%s,%s,%s);\n(async()=>{root.textContent='';%s})().catch(e=>{console.error(e);process.exit(1);});" % (
        json.dumps(script), json.dumps(state), json.dumps(panel_page.STRINGS[lang]), json.dumps(lang), body)
    result = subprocess.run([NODE, "-"], input=src, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


node = pytest.mark.skipif(not NODE, reason="node is not installed")
PAGE = "var p=sandbox.last.cockpit_v2.projects.filter(x=>x.key===%s)[0];var card=sandbox.v2Ideas(p);"


@node
@pytest.mark.parametrize("lang", ["he", "en"])
def test_a_tap_opens_the_idea_and_asks_the_lead_with_the_exact_payload(lang):
    key = json.dumps("תכנון" if lang == "he" else "planning")
    got = run(PAGE % key + "var row=card.querySelector('.v2-idea'),head=row.querySelector('.v2-idea-head'),body=row.querySelector('.v2-idea-body');"
              "var before=body.hidden;head.click();var opened=!body.hidden&&head.attrs['aria-expanded']==='true';"
              "row.querySelector('.v2-idea-ask').click();await flush();await flush();"
              "emit({section:card.attrs['data-v2-section'],idea:row.attrs['data-v2-idea'],before,opened,posted,"
              "done:!row.querySelector('.v2-idea-ask'),dir:row.querySelector('.v2-idea-title').dir});", lang)
    assert got["section"] == "project-ideas" and got["idea"] == "4" and got["before"] is True and got["opened"]
    [[path, body]] = got["posted"]
    assert path == "/api/say" and set(body) == {"session", "text", "send"} and body["session"] == "sample00"
    title = "בקשה ישנה" if lang == "he" else "Older request"
    lines = body["text"].split("\n")
    assert lines[0] == "Status question only, not an approval:"  # the panel message counts as the owner's word
    assert lines[2] == "%s #4" % ("תכנון" if lang == "he" else "Planning") and lines[3] == "«%s»" % title
    assert len(body["text"]) <= 600 and got["done"] and got["dir"] == "auto"


@node
def test_without_a_live_session_the_idea_only_shows_its_words():
    def mutate(state):
        for s in state["sessions"]:
            s["state"] = "ended"
        entries = [SimpleNamespace(session=s["id"], chat=1, name=s["name"]) for s in state["sessions"]]
        state["cockpit_v2"] = cockpit_v2.presentation(SimpleNamespace(projects=[], brief={}), state, entries, state["now"])
    got = run(PAGE % json.dumps("planning") + "var row=card.querySelector('.v2-idea');row.querySelector('.v2-idea-head').click();"
              "emit({ask:!!row.querySelector('.v2-idea-ask'),text:row.querySelector('.v2-idea-body').textContent,posted});",
              mutate=mutate)
    assert got == {"ask": False, "text": "Older request" + panel_page.V2_STRINGS["en"]["projects"]["ideaNoLead"],
                   "posted": []}


@node
def test_a_project_without_old_ideas_has_no_card():
    got = run(PAGE % json.dumps("learning") + "emit({card:card===null});")
    assert got == {"card": True}


def test_every_language_starts_the_status_question_with_the_no_approval_line():
    for lang, strings in panel_page.V2_STRINGS.items():
        assert strings["projects"]["ideaQuestion"].startswith("Status question only, not an approval:\n"), lang


def test_the_rtl_chevron_is_mirrored_like_the_knowledge_entry():
    css = (ROOT / "cockpit_v2.css").read_text()
    assert ".cockpit-v2:dir(rtl) .v2-idea .v2-chev svg{transform:scaleX(-1)}" in css
