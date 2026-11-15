"""Card copy is translated completely, ages use shared units, and finished work folds away."""
import json
import subprocess

import pytest

from ctb import cards, panel, panel_page
from ctb.config import Config
from test_panel_cards_by_session import DOM
from test_panel_old_group import live, topics
from test_panel_page import func


class Hebrew:
    def he(self, text):
        return "תרגום: " + text if isinstance(text, str) and text else ""


def string_fields(row, prefix=""):
    """List every copy-bearing leaf in the actual wire schema, not just a handpicked subset."""
    for key, value in row.items():
        if key.endswith("_he") or key in ("id", "kind", "session", "url", "topic_url"):
            continue
        path = prefix + key
        if isinstance(value, str):
            yield path, row, key, value
        elif isinstance(value, dict):
            yield from string_fields(value, path + ".")
        elif isinstance(value, list):
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    yield from string_fields(item, path + "[].")
                elif isinstance(item, str):
                    yield path + "[]", row, (key, i), item


def test_every_string_field_of_every_card_kind_is_translated(monkeypatch):
    now = 1_000_000
    topics("s1-session")
    live(monkeypatch, {"s1-session": "working"})
    bodies = {"progress": {"steps": ["Review fixes", "Back gestures"], "at": 1},
              "choice": {"options": ["Continue work", "Wait please"]}, "ask": {},
              "link": {"url": "https://example.com/", "label": "Open preview"},
              "numbers": {"figures": ["Open issues=Two"]}}
    assert set(bodies) == set(cards.KINDS)
    for kind, body in bodies.items():
        cards.publish("s1-session", -1001234567, 10, kind, "Agent title", "Agent detail", now=now, **body)
    cards.update_live("s1-session", "Running the browser checks", now=now)
    monkeypatch.setattr(panel, "_card_activity", lambda *a: {"task": "Check the UI", "activity": "Inspecting layout"})
    out = panel.build_state(Config(), tr=Hebrew(), now=now, light=True)
    seen = set()
    for card in out["cards"]:
        for path, row, key, value in string_fields(card):
            seen.add(path)
            if not value:
                continue
            translated = row[key[0] + "_he"][key[1]] if isinstance(key, tuple) else row[key + "_he"]
            assert translated == "תרגום: " + value, path
        assert card.get("url_he") is None and card.get("topic_url_he") is None
    assert seen == {"title", "detail", "topic", "live", "task", "activity", "steps[]", "options[]",
                    "label", "figures[].label", "figures[].value"}
    assert out["pending"] == 0


def test_new_card_copy_fields_are_translated_by_default_including_nested_names_and_now():
    row = {"now": "Reviewing fixes", "future_copy": "A new field",
           "steps": [{"name": "Review fixes", "detail": "Check the button"}],
           "future_list": ["One more field"], "id": "protocol-id", "url": "https://example.com/"}
    panel._hebrew_cards(Hebrew(), [row], [])
    for _, parent, key, value in string_fields(row):
        translated = parent[key[0] + "_he"][key[1]] if isinstance(key, tuple) else parent[key + "_he"]
        assert translated == "תרגום: " + value
    assert "id_he" not in row and "url_he" not in row


def test_pending_list_translations_and_disabled_translation_preserve_the_originals():
    class Pending:
        def he(self, text):
            return None if text == "Review fixes" else ""
    row = {"steps": ["Review fixes", "בדיקה"]}
    waiting = []
    panel._hebrew_cards(Pending(), [row], waiting)
    assert row == {"steps": ["Review fixes", "בדיקה"], "steps_he": [None, None]}
    assert len(waiting) == 1
    untranslated = {"steps": ["Review fixes"]}
    panel._hebrew_cards(None, [untranslated], [])
    assert untranslated == {"steps": ["Review fixes"]}


def test_the_sessions_now_text_gets_hebrew_and_renders_it_everywhere(monkeypatch):
    topics("s1-session")
    live(monkeypatch, {"s1-session": "working"})
    monkeypatch.setattr(panel, "_auto_cards", lambda *a: None)
    monkeypatch.setattr(panel.mac_resources, "snapshot", lambda: {})
    monkeypatch.setattr(panel.status, "transcript_of", lambda *a: None)
    monkeypatch.setattr(panel.panel_view, "current_step", lambda *a: "Review fixes")
    session = panel.build_state(Config(), tr=Hebrew(), now=1_000_000)["sessions"][0]
    assert session["now_he"] == "תרגום: Review fixes"
    out = render_js("function details(){};var b=sessionCard(" + json.dumps(session) + ",{});"
                    "console.log(JSON.stringify(texts(b,'act')));", ("line", "sessionCard"))
    assert out == [session["now_he"]]
    for name in ("sessionRow", "sessionCard", "renderAll"):
        assert '"bolt",shown(s,"now")' in func(name)


@pytest.mark.parametrize("age,old", [(1799, False), (1800, True), (1801, True)])
@pytest.mark.parametrize("finish", ["percent", "steps", "unfinished"])
def test_finished_cards_fold_after_half_an_hour_from_the_authored_update(monkeypatch, age, old, finish):
    now = 1_000_000
    rec = cards.publish("s1-session", -1001234567, 10, "progress", "Progress", steps=["Check"],
                        at=1 if finish == "steps" else 0, percent=100 if finish == "percent" else 20, now=now-age)
    # Even a fresh live line must not keep an already-finished plan in the main stack.
    rec.update(live="Recent summary", live_at=now)
    monkeypatch.setattr(cards, "reconcile_progress", lambda *a: [rec])
    live(monkeypatch, {"s1-session": "working"})
    result = panel.build_state(Config(), now=now, light=True)["cards"][0]
    assert cards.PROGRESS_FOLD_AFTER == 1800
    assert result["finished"] is (finish != "unfinished")
    assert result["old"] is (old and finish != "unfinished")


def render_js(script, functions=(), lang="he"):
    program = (DOM.replace("STRINGS", json.dumps(panel_page.STRINGS[lang])).replace("SESSIONS", "[]").replace("WA", "null")
               + "\nvar cardTextOpen={},pf={format:String},rtf=new Intl.RelativeTimeFormat(" + json.dumps(lang) + ",{numeric:'auto'});"
               + "function anim(k,v,f){f(v);}function plural(n,a,b){return n===1?a:b;}\n"
               + "\n".join(func(n).split("\nvar BODIES=", 1)[0] for n in ("el", "shown", "ago", "cardText", "progressBody") + functions)
               + "\n" + script)
    result = subprocess.run(["node", "-e", program], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("lang", ["he", "en"])
@pytest.mark.parametrize("age,unit", [(416 * 60, "hour"), (4 * 86400, "day")])
@pytest.mark.parametrize("field", ["live", "activity"])
def test_card_ages_use_hours_and_days_in_both_languages(lang, age, unit, field):
    card = {"id": "p", field: "Reviewing fixes", field + "_age": age}
    out = render_js("var b=new Element('div');progressBody(" + json.dumps(card) + ",b);"
                    "console.log(JSON.stringify({age:b.children[2].textContent,expected:S.cardUpdated+' '+rtf.format(-"
                    + str(7 if unit == "hour" else 4) + "," + json.dumps(unit) + ")}));", lang=lang)
    assert out["age"] == out["expected"]
    for strings in panel_page.STRINGS.values():
        assert "cardLiveUpdated" not in strings and "cardActivityUpdated" not in strings


def test_translated_steps_options_figures_and_link_labels_render_with_original_fallback():
    card = {"id": "p", "steps": ["Review fixes", "Back gestures"], "steps_he": ["בדיקת תיקונים", None], "at": 0,
            "options": ["Continue", "Wait"], "options_he": ["להמשיך", "לחכות"],
            "figures": [{"label": "Open issues", "label_he": "בעיות פתוחות", "value": "Two", "value_he": "שתיים"}],
            "url": "https://example.com/", "label": "Preview", "label_he": "תצוגה מקדימה"}
    out = render_js("var c=" + json.dumps(card) + ";var b=new Element('div');progressBody(c,b);choiceBody(c,b);numbersBody(c,b);linkBody(c,b);"
                    "console.log(JSON.stringify({steps:texts(b,'sl'),options:texts(b,'copt'),figures:texts(b,'fig'),links:texts(b,'lk-t')}));",
                    ("choiceBody", "numbersBody", "hostOf", "linkBody"))
    assert out == {"steps": ["בדיקת תיקונים", "Back gestures"], "options": ["להמשיך", "לחכות"],
                   "figures": ["שתייםבעיות פתוחות"], "links": ["תצוגה מקדימהexample.com"]}


def test_live_copy_keeps_the_full_text_and_tap_expansion_across_redraws():
    text = "Review fixes and back gestures once the browser checks finish. " * 8
    assert cards.subject_line(text) == text.strip()
    out = render_js("var c={id:'p',live:" + json.dumps(text) + "};var b=cardText(c,'live').children[0];"
                    "var before=b['aria-expanded'];b.listeners.click();var after=b['aria-expanded'];"
                    "var redrawn=cardText(c,'live').children[0];redrawn.listeners.click();"
                    "console.log(JSON.stringify({text:b.textContent,before,after,collapsed:redrawn['aria-expanded']}));")
    assert out == {"text": text, "before": "false", "after": "true", "collapsed": "false"}
    css = panel_page.PAGE.split("<style", 1)[1].split("</style>", 1)[0]
    assert "-webkit-line-clamp:3" in css and "word-break:normal" in css
    assert "text-overflow:ellipsis" in css and "overflow-wrap:break-word" in css


def test_oversized_live_copy_is_scrubbed_and_bounded_at_a_whole_word():
    text = "Review complete words " * 100
    result = cards.subject_line(text)
    assert len(result) <= cards.LIVE_CHARS and result.endswith("…")
    assert text.startswith(result[:-1]) and text[len(result)-1] == " "
    assert cards.subject_line("word" * cards.LIVE_CHARS) == "…"


def test_only_finished_old_progress_leaves_the_main_stack_and_all_cards_stay_reachable():
    out = render_js("""
var groups=[];
function sec(){return new Element('div');}
function fold(){var f=new Element('div');f.body=new Element('div');f.appendChild(f.body);return f;}
function cardGroups(list,host){groups.push({ids:list.map(c=>c.id),old:host!==app});}
var d={cards:[
 {id:'recent',kind:'progress',finished:true,old:false,here:true},
 {id:'finished',kind:'progress',finished:true,old:true,here:true},
 {id:'ended',kind:'progress',ended:true,here:true},
 {id:'closed',kind:'progress',auto_closed:true,here:true},
 {id:'unfinished',kind:'progress',finished:false,old:true,here:true},
 {id:'other-topic',kind:'progress',finished:true,old:true,here:false}]};
cardSections(d,true);console.log(JSON.stringify(groups));
""", ("sesKey", "allCards", "cardSections"))
    assert out == [{"ids": ["recent", "unfinished"], "old": False},
                   {"ids": ["finished", "ended", "closed"], "old": True}]
