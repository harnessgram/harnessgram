"""The Studio page's own script, run in node against a small fake DOM and a fake panel host (tests/studio_fake_dom.js),
so its guards are checked on every machine that has node, with or without Playwright: the back bar, the back
confirmation, the must-keep elements of every state, and one language per line. Skipped only when node is missing."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from ctb.studio import page as studio_page
from ctb.studio import provider
import studio_browser as sb

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(not NODE, reason="node is not installed")
HERE = Path(__file__).resolve().parent
PRELUDE = (HERE / "studio_fake_dom.js").read_text(encoding="utf-8")
KEEP = json.loads((HERE / "studio_must_keep.json").read_text(encoding="utf-8"))
HEBREW = re.compile(r"[֐-׿]")
LATIN = re.compile(r"[A-Za-z]")
ISOLATE = re.compile("⁨[^⁩]*⁩")


def script():
    html = studio_page.render_page()
    return html.split("<script>", 1)[1].rsplit("</script>", 1)[0]


def run(body, lang="he"):
    src = PRELUDE + "\nstart(%s);\n(async function () {\n%s\n})().catch(function (e) { console.error(e); process.exit(1); });" % (
        json.dumps(script()), body)
    done = subprocess.run([NODE, "-"], input=src, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def desk_js(**kw):
    return json.dumps({"ok": True, "desk": sb.sample_desk(**kw)})


def boot(lang="he", status=200, data="null"):
    return "hello(%s); await answer(%d, %s);" % (json.dumps(lang), status, data)


# --- every state renders its must-keep elements ---------------------------------------------------------------------------

STATES = {
    "home": ("200", desk_js()),
    "empty-home": ("200", desk_js(waiting=[], working=[], shelf=[])),
    "error-offline": ("0", "null"),
    "error-expired": ("403", "null"),
    "bridge-off": ("503", json.dumps({"ok": False, "error": "mirroring is off"})),
    "error-desk": ("502", json.dumps({"error": "app failed"})),
    "no-permission": ("200", json.dumps({"ok": False, "error": "no_permission"})),
}


@needs_node
@pytest.mark.parametrize("state", sorted(STATES))
@pytest.mark.parametrize("lang", ["he", "en"])
def test_each_state_draws_its_must_keep_elements(state, lang):
    status, data = STATES[state]
    keys = KEEP[state]
    got = run(boot(lang, int(status), data) + "emit(%s.map(function (k) { return [k, byKeep(k).length]; }));" % json.dumps(keys))
    assert all(n >= 1 for _, n in got), got


@needs_node
def test_the_loading_state_draws_its_skeleton_before_any_answer():
    got = run('hello("he"); await flush(); emit(%s.map(function (k) { return [k, byKeep(k).length]; }));' % json.dumps(KEEP["loading-home"]))
    assert all(n >= 1 for _, n in got), got


@needs_node
def test_an_unreachable_studio_keeps_the_loaded_desk_under_the_offline_notice():
    body = boot(data=desk_js()) + "docHandlers.visibilitychange(); await answer(0, null); " \
        "emit([byKeep('offline-banner').length, byKeep('waiting-stack').length]);"
    assert run(body) == [1, 1]


@needs_node
def test_without_a_panel_answer_the_page_says_so():
    got = run('for (var i = 0; i < 4; i++) { timers.forEach(function (t) { t.f(); }); await flush(); } '
              'emit([byKeep("error-card").length, byKeep("error-actions").length]);')
    assert got == [1, 1]


# --- the back bar and the back confirmation -------------------------------------------------------------------------------

OPENERS = {  # the entry points whose page a later step builds ("open" and the preview open the desk: see below)
    "new": "byKeep('new-video')[0].click();",
    "approve": "byKeep('slate-actions')[0].childNodes[1].click();",
    "item": "byClass('shelf')[0].childNodes[0].click();",
    "more": "byClass('link-btn')[0].click();",
}


@needs_node
@pytest.mark.parametrize("name", sorted(OPENERS))
@pytest.mark.parametrize("lang", ["he", "en"])
def test_every_entry_point_has_a_wired_back_bar_that_confirms_and_goes_home(name, lang):
    body = boot(lang, 200, desk_js()) + OPENERS[name] + """
    var bars = byClass('bar'), back = byKeep('back');
    var shown = {bars: bars.length, back: back.length, wired: back[0] && (back[0].listeners.click || []).length,
      label: back[0] && back[0].attrs['aria-label'], card: byKeep('coming-next').length, dock: byKeep('primary-dock').length,
      focus: sent('focus').pop(), backOn: sent('back').pop()};
    back[0].click();
    shown.home = byKeep('waiting-stack').length; shown.confirmed = sent('back_done').length;
    shown.backOff = sent('back').pop(); shown.focusOff = sent('focus').pop();
    emit(shown);"""
    got = run(body, lang)
    assert got["bars"] == 1 and got["back"] == 1 and got["wired"] >= 1 and got["label"]
    assert got["card"] == 1 and got["dock"] == 1
    assert got["focus"]["on"] is True and got["backOn"]["on"] is True
    assert got["home"] == 1 and got["confirmed"] == 1
    assert got["backOff"]["on"] is False and got["focusOff"]["on"] is False


@needs_node
def test_the_dock_also_goes_home_and_the_panels_back_is_always_confirmed():
    body = boot(data=desk_js()) + """
    send({cockpit: 'back'});                           // on the home screen: nothing to undo, still confirmed
    var a = sent('back_done').length;
    byKeep('new-video')[0].click();
    send({cockpit: 'back'});                           // on a page: goes home, confirmed
    var b = sent('back_done').length, home = byKeep('waiting-stack').length;
    byKeep('new-video')[0].click();
    byKeep('primary-dock')[0].childNodes[0].childNodes[0].click();
    var c = sent('back_done').length, home2 = byKeep('waiting-stack').length;
    handler({source: {}, data: {cockpit: 'back'}});   // from another window: ignored
    emit([a, b, home, c, home2, sent('back_done').length]);"""
    a, b, home, c, home2, last = run(body)
    assert (a, b, home, c, home2, last) == (1, 2, 1, 3, 1, 3)


@needs_node
def test_the_page_asks_for_no_takeover_before_it_has_a_page_to_go_back_from():
    got = run(boot(data=desk_js()) + "emit([sent('back').length, sent('focus').length]);")
    assert got == [0, 0]


# --- one language per line -------------------------------------------------------------------------------------------------

def text(he=None, en=None):
    return {k: v for k, v in (("he", he), ("en", en)) if v}


def shelf(series, title, **kw):
    d = sb.sample_desk(waiting=[], working=[], series=[{"id": "s", "name": series}],
                       shelf=[{"id": "x-1", "series": "s", "title": title, "status": "ready", "scene": "q", **kw}],
                       templates=[{"id": "t", "name": title, "length_s": 60, "format": "wide", "scene": "q"}])
    return {"ok": True, "desk": d}


def dump(lang, data):
    return run(boot(lang, 200, json.dumps(data)) + "emit(allEls().map(function (e) { return {tag: e.tagName, cls: e.className, "
               "own: ownText(e), lang: e.attrs.lang || null, dir: e.attrs.dir || null, text: textOf(e)}; }));", lang)


def own_lines(els):
    return [e for e in els if e["own"].strip()]


def assert_no_mixed_line(els, ui):
    for e in own_lines(els):
        bare = ISOLATE.sub("", e["own"])
        assert not (HEBREW.search(bare) and LATIN.search(bare)), e
        foreign = "en" if ui == "he" else "he"
        if e["lang"] == foreign:  # a line in the other language has its own direction and only that language
            assert e["dir"] == ("ltr" if foreign == "en" else "rtl"), e
            assert not (HEBREW.search(e["own"]) if foreign == "en" else LATIN.search(e["own"])), e


CASES = {
    "hebrew only": (text(he="שאלה של שבוע"), text(he="למה השמיים כחולים?")),
    "english title only": (text(he="שאלה של שבוע"), text(en="Why is the sky blue and the grass green?")),
    "english series only": (text(en="Question of the week and more"), text(he="למה השמיים כחולים?")),
    "english both": (text(en="Question of the week"), text(en="Why is the sky blue and the grass green?")),
    "short english name": (text(en="Weekly"), text(he="למה השמיים כחולים?")),
    "plain strings": ("שאלה של שבוע", "Why is the sky blue and the grass green?"),
    "plain mixed": ("Question of the week", "למה השמיים כחולים?"),
}


@needs_node
@pytest.mark.parametrize("case", sorted(CASES))
def test_a_hebrew_line_never_holds_english_words_and_the_other_way_round(case):
    series, title = CASES[case]
    for ui in ("he", "en"):
        assert_no_mixed_line(dump(ui, shelf(series, title)), ui)


@needs_node
def test_a_single_language_item_shows_in_its_own_language_and_direction():
    els = dump("he", shelf(text(he="שאלה של שבוע"), text(en="Why is the sky blue and the grass green?")))
    row = [e for e in els if e["cls"] == "nm" and "Why is the sky" in e["text"]]
    assert row, "the shelf row's name"
    english = [e for e in els if e["lang"] == "en" and "Why is the sky" in e["own"]]
    assert english and english[0]["dir"] == "ltr"
    # the Hebrew part stays on its own line, in the page's direction
    assert any(e["own"].strip() == "שאלה של שבוע" and e["lang"] is None for e in els)


@needs_node
def test_a_short_english_name_stays_inside_the_hebrew_line_bidi_isolated():
    els = dump("he", shelf(text(en="Weekly"), text(he="למה השמיים כחולים?")))
    assert any("⁨Weekly⁩" in e["own"] and HEBREW.search(e["own"]) for e in els)


@needs_node
def test_english_ui_with_hebrew_only_data_mirrors_the_rule():
    els = dump("en", shelf(text(en="Weekly question"), text(he="למה השמיים כחולים והעשב ירוק?")))
    hebrew = [e for e in els if e["lang"] == "he" and "השמיים" in e["own"]]
    assert hebrew and hebrew[0]["dir"] == "rtl"
    assert_no_mixed_line(els, "en")


# --- SVG geometry: no plus or cross shapes ---------------------------------------------------------------------------------

def tokens(d):
    return re.findall(r"[MmLlHhVvZzCcSsQqTtAa]|-?\d*\.?\d+(?:e-?\d+)?", d)


def segments(d):
    """The straight segments of a path (curves count by their chord: a cross drawn with arcs is not a thing here)."""
    out, cur, start, cmd, toks, i = [], (0.0, 0.0), (0.0, 0.0), "", tokens(d), 0
    n = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "T": 2, "A": 7, "Z": 0}
    while i < len(toks):
        if re.match(r"[A-Za-z]", toks[i]):
            cmd = toks[i]
            i += 1
            if cmd in "Zz":
                if cur != start:
                    out.append((cur, start))
                cur = start
                continue
        k = n[cmd.upper()]
        a = [float(x) for x in toks[i:i + k]]
        i += k
        rel = cmd.islower()
        x, y = cur
        c = cmd.upper()
        if c in "ML":
            nx, ny = (x + a[0], y + a[1]) if rel else (a[0], a[1])
        elif c == "H":
            nx, ny = (x + a[0] if rel else a[0]), y
        elif c == "V":
            nx, ny = x, (y + a[0] if rel else a[0])
        else:
            nx, ny = (x + a[-2], y + a[-1]) if rel else (a[-2], a[-1])
        if c == "M":
            start = (nx, ny)
            cmd = "l" if rel else "L"  # further pairs after a move are lines
        elif c in "LHV":
            out.append(((x, y), (nx, ny)))
        cur = (nx, ny)
    return out


def crossing(s, t, margin=0.12):
    """True when two segments cross each other inside both (a plus, a cross or an X), not at an end or a T joint."""
    (x1, y1), (x2, y2), (x3, y3), (x4, y4) = s[0], s[1], t[0], t[1]
    den = (x2 - x1) * (y4 - y3) - (y2 - y1) * (x4 - x3)
    if abs(den) < 1e-9:
        return False
    u = ((x3 - x1) * (y4 - y3) - (y3 - y1) * (x4 - x3)) / den
    v = ((x3 - x1) * (y2 - y1) - (y3 - y1) * (x2 - x1)) / den
    return margin < u < 1 - margin and margin < v < 1 - margin


def crosses(shape_d):
    segs = [s for s in segments(shape_d) if abs(s[0][0] - s[1][0]) + abs(s[0][1] - s[1][1]) > 3]
    return any(crossing(a, b) for i, a in enumerate(segs) for b in segs[i + 1:])


def test_the_geometry_check_itself_sees_a_plus_a_cross_and_an_x_but_not_a_check_or_a_tick():
    assert crosses("M12 6v12M6 12h12")  # plus
    assert crosses("M6 6l12 12M18 6L6 18")  # x
    assert crosses("M12 4v16M4 12h16M7 7l10 10")
    assert not crosses("M5 12l5 5L20 7")  # a check
    assert not crosses("M4 5h16v14H4zM8 5v14M16 5v14M4 9h4M4 14h4")  # the film strip (T joints only)
    assert not crosses("M6 12h12")  # a minus


def test_no_icon_or_drawing_of_the_studio_has_a_plus_or_cross_shape():
    sprite = (Path(studio_page.__file__).parent / "sprite.svg").read_text(encoding="utf-8")
    drawings = {}
    for m in re.finditer(r'<symbol id="(i-[a-z-]+)"[^>]*>(.*?)</symbol>', sprite, re.S):
        drawings[m.group(1)] = " ".join(re.findall(r'\bd="([^"]+)"', m.group(2)))
        lines = re.findall(r'<line [^>]*x1="([\d.]+)"[^>]*y1="([\d.]+)"[^>]*x2="([\d.]+)"[^>]*y2="([\d.]+)"', m.group(2))
        drawings[m.group(1)] += " " + " ".join("M%s %sL%s %s" % l for l in lines)
    art = " ".join(re.findall(r'\bd="([^"]+)"', studio_page.ART_CLAPPER))
    drawings["art-clapper"] = art
    assert len(drawings) > 30
    flagged = [name for name, d in drawings.items() if crosses(d)]
    assert not flagged, flagged



# --- the desk of one video ---------------------------------------------------------------------------------------------------

DESK_KEEP = KEEP


def project_js(pid="weekly-12", marks=None, **override):
    p = provider.project(pid)
    p.update(override)
    return json.dumps({"ok": True, "project": p, "marks": marks or {}})


OPEN = "byKeep('slate-actions')[0].childNodes[0].click();"


def desk_boot(lang="he", status=200, data=None, then=""):
    return boot(lang, 200, desk_js()) + OPEN + "await answer(%d, %s);" % (status, data or project_js()) + then


def keys_js(keys):
    return "emit(%s.map(function (k) { return [k, byKeep(k).length]; }));" % json.dumps(keys)


ALL_DONE = {"2": {"mark": "remove"}, "5": {"mark": "tighten"}, "10": {"mark": "keep"}}
DESK_STATES = {
    "desk": desk_boot(),
    "desk-scenes": desk_boot(then="byKeep('desk-modes')[0].childNodes[1].click();"),
    "desk-done": desk_boot(data=project_js(marks=ALL_DONE)),
    "words": desk_boot(then="byKeep('to-map')[0].click();"),
    "loading-desk": boot("he", 200, desk_js()) + OPEN + "await flush();",
    "offline-desk": desk_boot(then="send({cockpit: 'back'}); " + OPEN + "await answer(0, null);"),
    "error-project": desk_boot(status=502, data=json.dumps({"error": "app failed"})),
}


@needs_node
@pytest.mark.parametrize("state", sorted(DESK_STATES))
@pytest.mark.parametrize("lang", ["he", "en"])
def test_each_desk_state_draws_its_must_keep_elements(state, lang):
    body = DESK_STATES[state].replace('hello("he")', "hello(%s)" % json.dumps(lang)).replace(boot("he", 200, desk_js()), boot(lang, 200, desk_js()))
    got = run(body + keys_js(KEEP[state]), lang)
    assert all(n >= 1 for _, n in got), got


@needs_node
def test_a_video_without_suggestions_and_one_not_found_have_their_own_states():
    quiet = provider.project("weekly-12")
    for s in quiet["sentences"]:
        s.update(suggest=None, short=None, short_s=None, why=None)
    got = run(desk_boot(data=json.dumps({"ok": True, "project": quiet, "marks": {}})) +
              "emit([byKeep('no-suggestions').length, textOf(byKeep('no-suggestions')[0]), byKeep('cue-stack').length]);")
    assert got[0] == 1 and "אין הצעות פתוחות" in got[1] and got[2] == 0
    got = run(desk_boot(data=json.dumps({"ok": False, "error": "not_found"})) + "emit(textOf(byKeep('error-card')[0]));")
    assert "הסרטון לא נמצא" in got


@needs_node
@pytest.mark.parametrize("opener", ["byKeep('slate-actions')[0].childNodes[0].click();", "byClass('play')[0].click();"])
def test_open_and_the_preview_open_the_desk_of_that_video(opener):
    body = boot("he", 200, desk_js()) + opener + """
    var asked = sent('api').pop().body, loading = byKeep('poster-first').length;
    await answer(200, %s);
    emit({asked: asked, loading: loading, cue: byKeep('cue-stack').length, focus: sent('focus').pop(), back: sent('back').pop(),
      title: textOf(byClass('bar')[0])});""" % project_js()
    got = run(body)
    assert got["asked"] == {"op": "project.get", "project": "weekly-12"} and got["loading"] == 1 and got["cue"] == 1
    assert got["focus"]["on"] is True and got["back"]["on"] is True
    assert "למה העלים מחליפים צבע?" in got["title"] and "טיוטה שנייה" in got["title"]


@needs_node
def test_back_walks_the_pages_one_by_one_and_is_always_confirmed():
    body = desk_boot(then="""
    byKeep('to-map')[0].click();
    var words = byKeep('sentence-list').length;
    send({cockpit: 'back'});
    var desk = byKeep('cue-stack').length, a = sent('back_done').length, stillFocused = sent('focus').pop().on;
    byKeep('primary-dock')[0].childNodes[0].childNodes[1].childNodes[1].click();   // "prepare a new draft"
    var next = byKeep('coming-next').length, nextText = textOf(root);
    byKeep('back')[0].click();
    var desk2 = byKeep('cue-stack').length;
    byKeep('back')[0].click();
    emit({words: words, desk: desk, a: a, stillFocused: stillFocused, next: next, nextText: nextText, desk2: desk2,
      home: byKeep('waiting-stack').length, done: sent('back_done').length, focusOff: sent('focus').pop(), backOff: sent('back').pop(),
      calls: sent('api').length});""")
    got = run(body)
    assert got["words"] == 1 and got["desk"] == 1 and got["a"] == 1 and got["stillFocused"] is True
    assert got["next"] == 1 and "הסימונים שמורים כטיוטה בלבד" in got["nextText"] and "לחזור לסרטון" in got["nextText"]
    assert got["desk2"] == 1 and got["home"] == 1 and got["done"] == 3
    assert got["focusOff"]["on"] is False and got["backOff"]["on"] is False
    assert got["calls"] == 2  # desk.get and project.get: walking the pages asks the Studio for nothing more


def map_classes():
    return "byKeep('map')[0].childNodes.map(function (c) { return c.className; })"


@needs_node
def test_a_mark_shows_at_once_is_stored_and_moves_to_the_next_suggestion():
    body = desk_boot(then="""
    var before = %s;
    byKeep('cue-answers')[0].childNodes[2].click();          // the team suggests removing sentence two: "remove"
    var asked = sent('api').pop().body, optimistic = %s;
    await answer(200, {ok: true, marks: {"2": {mark: "remove"}}});
    emit({before: before, asked: asked, optimistic: optimistic, after: %s, dock: textOf(byKeep('new-length')[0]),
      cue: textOf(byKeep('cue-stack')[0]), player: textOf(byKeep('player')[0])});""" % (map_classes(), map_classes(), map_classes()))
    got = run(body)
    assert got["before"][1] == "s cur" and got["asked"] == {"op": "marks.set", "project": "weekly-12", "i": 2, "mark": "remove"}
    assert got["optimistic"][1] == "x" and got["after"][1] == "x"
    assert "שינוי אחד" in got["dock"] and "1:05" in got["dock"]
    assert "הצוות מציע לקצר" in got["cue"] and "0:20–0:27" in got["cue"]  # the next open suggestion
    assert "שבסתיו" in got["player"] and got["after"][4] == "s cur"


@needs_node
def test_a_mark_the_studio_did_not_keep_rolls_back_and_says_why():
    offline = run(desk_boot(then="""
    byKeep('cue-answers')[0].childNodes[2].click();
    await answer(0, null);
    emit({map: %s, banner: byKeep('offline-banner').length, off: byKeep('cue-answers')[0].childNodes.map(function (b) {
      return b.attrs.disabled != null; }), toast: byClass('toast').map(textOf)});""" % map_classes()))
    assert offline["map"][1] == "s cur" and offline["banner"] == 1 and offline["off"] == [True, True, True]
    assert "לא נשמר" in offline["toast"][0]
    refused = run(desk_boot(then="""
    byKeep('cue-answers')[0].childNodes[1].click();
    await answer(200, {ok: false, error: "unavailable"});
    emit({map: %s, banner: byKeep('offline-banner').length, toast: byClass('toast').map(textOf)});""" % map_classes()))
    assert refused["map"][1] == "s cur" and refused["banner"] == 0 and "הסימון חזר" in refused["toast"][0]


@needs_node
@pytest.mark.parametrize("lang,page_dir", [("he", "rtl"), ("en", "ltr")])
def test_the_shorter_wording_field_takes_the_page_direction_while_empty_and_saves_the_owners_words(lang, page_dir):
    body = desk_boot(lang, then="""
    byKeep('cue-answers')[0].childNodes[0].click();           // keep sentence two: the next suggestion is "shorten"
    await answer(200, {ok: true, marks: {"2": {mark: "keep"}}});
    byClass('fix')[0].click();
    var f = allEls().filter(function (e) { return e.tagName === "textarea"; })[0];
    var filled = f.attrs.dir, wasFocused = focused === f;
    f.value = ""; f.fire("input");
    var empty = f.attrs.dir;
    f.value = "   "; f.fire("input");
    var blank = f.attrs.dir;
    f.value = "Short words"; f.fire("input");
    var typed = f.attrs.dir;
    byKeep('cue-answers')[0].childNodes[1].click();
    emit({filled: filled, focused: wasFocused, empty: empty, blank: blank, typed: typed, asked: sent('api').pop().body,
      ph: f.attrs.placeholder, edit: byKeep('short-edit').length});""")
    got = run(body, lang)
    assert got["filled"] == "auto" and got["focused"] and got["empty"] == page_dir and got["blank"] == page_dir
    assert got["typed"] == "auto" and got["ph"] and got["edit"] == 0
    assert got["asked"] == {"op": "marks.set", "project": "weekly-12", "i": 5, "mark": "tighten", "text": "Short words"}


@needs_node
def test_the_teams_own_wording_is_saved_without_a_text():
    got = run(desk_boot(then="""
    byKeep('cue-answers')[0].childNodes[0].click();
    await answer(200, {ok: true, marks: {"2": {mark: "keep"}}});
    byClass('fix')[0].click();
    byKeep('cue-answers')[0].childNodes[1].click();
    emit(sent('api').pop().body);"""))
    assert got == {"op": "marks.set", "project": "weekly-12", "i": 5, "mark": "tighten"}


@needs_node
def test_the_runtime_and_the_change_count_follow_the_marks():
    got = run(desk_boot(data=project_js(marks={"2": {"mark": "remove"}, "5": {"mark": "tighten"}}),
                        then="byKeep('to-map')[0].click(); emit([textOf(byKeep('runtime')[0]), textOf(byKeep('new-length')[0])]);"))
    assert "1:08" in got[0] and "1:02" in got[0] and "חוסך שש שניות" in got[0]
    assert "שני שינויים" in got[1] and "1:02" in got[1]
    en = run(desk_boot("en", data=project_js(marks={"2": {"mark": "remove"}}), then="emit(textOf(byKeep('new-length')[0]));"), "en")
    assert "One change" in en and "1:05" in en


@needs_node
def test_the_words_view_groups_sentences_by_scene_and_a_scene_card_opens_its_group():
    got = run(desk_boot(then="""
    byKeep('desk-modes')[0].childNodes[1].click();
    var cards = byKeep('scene-card').length;
    byKeep('scene-card')[2].click();
    emit({cards: cards, groups: byKeep('scene-group').length, rows: byKeep('sentence').length,
      scrolled: scrolledTo && scrolledTo.attrs['data-keep'], third: scrolledTo && textOf(scrolledTo)});"""))
    assert got["cards"] == 7 and got["groups"] == 7 and got["rows"] == 12
    assert got["scrolled"] == "scene-group" and "הסתיו מגיע" in got["third"] and "סצנה שלישית" in got["third"]


@needs_node
def test_a_removed_sentence_can_be_restored_from_the_words_view():
    got = run(desk_boot(data=project_js(marks={"2": {"mark": "remove"}}), then="""
    byKeep('to-map')[0].click();
    var cut = byClass('cut').length;
    byClass('cut')[0].childNodes[1].childNodes[1].click();
    emit({cut: cut, asked: sent('api').pop().body});"""))
    assert got["cut"] == 1 and got["asked"] == {"op": "marks.set", "project": "weekly-12", "i": 2, "mark": "keep"}


@needs_node
def test_a_tap_on_the_map_moves_the_player_and_play_skips_removed_sentences():
    got = run(desk_boot(data=project_js(marks={"2": {"mark": "remove"}}), then="""
    var cells = byKeep('map')[0].childNodes;
    byKeep('map')[0].fire('click', {target: cells[0]});
    var first = textOf(byKeep('player')[0]);
    byClass('play')[0].click();
    var label = byClass('play')[0].attrs['aria-label'];
    var live = timers.filter(function (t) { return !t.cleared; }).pop(); live.f();
    emit({first: first, label: label, next: textOf(byKeep('player')[0]), cur: %s});""" % map_classes()))
    assert "שאלתם אותנו השבוע" in got["first"] and got["label"] == "לעצור"
    assert "בכל עלה ירוק" in got["next"] and got["cur"][2] == "cur"  # sentence two is removed: three comes next


@needs_node
def test_real_stills_and_sound_come_through_the_media_route_and_bad_paths_never_do():
    p = provider.project("weekly-12")
    p["scenes"][0]["frame"] = "weekly-12/scene_1.jpg"
    p["sentences"][0]["frame"] = "../secret.jpg"
    p["sentences"][1]["frame"] = "https://example.com/x.jpg"
    p["sentences"][2]["frame"] = "/etc/x.jpg"
    p["sentences"][1]["audio"] = "weekly-12/s2.m4a"
    data = json.dumps({"ok": True, "project": p, "marks": {}})
    got = run(desk_boot(data=data, then="""
    var imgs = function () { return allEls().filter(function (e) { return e.tagName === "img"; }).map(function (e) { return e.attrs.src; }); };
    var desk = imgs();
    byKeep('hear')[0].click();
    byKeep('desk-modes')[0].childNodes[1].click();
    emit({desk: desk, scenes: imgs(), audios: audios, toast: byClass('toast').length});"""))
    good = {"media/weekly-12/scene_1.jpg?auth=init"}  # the player shows sentence two, whose path is not a media path
    assert set(got["desk"]) <= good and got["scenes"] and set(got["scenes"]) == good
    assert got["audios"] == ["media/weekly-12/s2.m4a?auth=init"] and got["toast"] == 0
    quiet = run(desk_boot(then="byKeep('hear')[0].click(); emit([audios.length, byClass('toast').map(textOf)]);"))
    assert quiet[0] == 0 and "אין קול" in quiet[1][0]


@needs_node
def test_an_answer_for_a_video_no_longer_open_is_dropped():
    body = boot("he", 200, desk_js()) + OPEN + """
    send({cockpit: 'back'});
    byClass('dots')[0].childNodes[1].click();
    byKeep('slate-actions')[0].childNodes[0].click();
    await answer(200, %s);
    var still = byKeep('poster-first').length;
    await answer(200, %s);
    emit({still: still, title: textOf(byClass('bar')[0]), asked: sent('api').filter(function (m) { return m.body.op === 'project.get'; })
      .map(function (m) { return m.body.project; })});""" % (project_js(), project_js("weekly-13"))
    got = run(body)
    assert got["still"] == 1 and "איך הדבורים מכינות דבש?" in got["title"] and got["asked"] == ["weekly-12", "weekly-13"]


@needs_node
@pytest.mark.parametrize("data", [json.dumps({"ok": True, "project": "nope"}), json.dumps({"ok": True, "project": {"id": "other"}}),
                                  json.dumps({"ok": True, "project": {"id": "weekly-12", "sentences": []}})])
def test_a_wrongly_shaped_video_is_an_error_not_a_broken_page(data):
    got = run(desk_boot(data=data) + keys_js(KEEP["error-project"]))
    assert all(n >= 1 for _, n in got), got


@needs_node
@pytest.mark.parametrize("view", ["desk", "scenes", "words"])
@pytest.mark.parametrize("ui", ["he", "en"])
def test_the_desk_never_mixes_two_languages_on_a_line(view, ui):
    then = {"desk": "", "scenes": "byKeep('desk-modes')[0].childNodes[1].click();", "words": "byKeep('to-map')[0].click();"}[view]
    p = provider.project("weekly-12")
    p["sentences"][0]["said"] = {"en": "You asked us this week: why do leaves change colour in autumn?"}  # one language only
    p["scenes"][1]["title"] = {"en": "Leaf"}  # a short name of the other language
    data = json.dumps({"ok": True, "project": p, "marks": {"5": {"mark": "tighten", "text": "בסתיו הימים מתקצרים."}}})
    els = run(desk_boot(ui, data=data, then=then + "emit(allEls().map(function (e) { return {tag: e.tagName, cls: e.className, "
                        "own: ownText(e), lang: e.attrs.lang || null, dir: e.attrs.dir || null, text: textOf(e)}; }));"), ui)
    assert_no_mixed_line(els, ui)



# --- review of #249: answers in any order, per-video state, a known offline state, mixed wording ------------------------------

@needs_node
def test_an_older_save_answer_never_undoes_a_newer_mark():
    got = run(desk_boot(then="""
    byKeep('cue-answers')[0].childNodes[2].click();           // remove sentence two
    byKeep('cue-answers')[0].childNodes[1].click();           // shorten sentence five (the next suggestion)
    // the second answer comes first and holds both; the first one, older, holds only sentence two
    await answerWhere(function (b) { return b.op === 'marks.set' && b.i === 5; }, 200,
      {ok: true, marks: {"2": {mark: "remove"}, "5": {mark: "tighten"}}});
    await answerWhere(function (b) { return b.op === 'marks.set' && b.i === 2; }, 200, {ok: true, marks: {"2": {mark: "remove"}}});
    emit({map: %s, dock: textOf(byKeep('new-length')[0])});""" % map_classes()))
    assert got["map"][1] == "x" and got["map"][4].split()[0] == "t", got["map"]
    assert "שני שינויים" in got["dock"]


@needs_node
def test_a_failed_save_of_another_video_leaves_the_open_video_writable():
    got = run(boot("he", 200, desk_js()) + OPEN + "await answer(200, %s);" % project_js() + """
    byKeep('cue-answers')[0].childNodes[2].click();           // a save for the leaves video is on its way
    send({cockpit: 'back'});
    byClass('dots')[0].childNodes[1].click();
    byKeep('slate-actions')[0].childNodes[0].click();          // open the bees video
    await answerWhere(function (b) { return b.op === 'project.get'; }, 200, %s);
    await answerWhere(function (b) { return b.op === 'marks.set'; }, 0, null);   // the old save fails: no network
    var here = {banner: byKeep('offline-banner').length, toast: byClass('toast').length,
      off: byKeep('cue-answers')[0].childNodes.map(function (b) { return b.attrs.disabled != null; })};
    byKeep('cue-answers')[0].childNodes[1].click();
    here.asked = sent('api').pop().body;
    send({cockpit: 'back'});
    byClass('dots')[0].childNodes[0].click();
    byKeep('slate-actions')[0].childNodes[0].click();          // back to the leaves video: it remembers it failed
    here.leaves = {banner: byKeep('offline-banner').length, map: %s};
    emit(here);""" % (project_js("weekly-13"), map_classes()))
    assert got["banner"] == 0 and got["toast"] == 0 and got["off"] == [False, False, False]
    assert got["asked"] == {"op": "marks.set", "project": "weekly-13", "i": 4, "mark": "tighten"}
    assert got["leaves"]["banner"] == 1 and got["leaves"]["map"][1].split()[0] == "s"  # rolled back, read-only


@needs_node
def test_reopening_a_video_known_offline_stays_read_only_until_a_request_succeeds():
    got = run(desk_boot(then="""
    byKeep('cue-answers')[0].childNodes[2].click();
    await answer(0, null);                                     // the save found no Studio
    send({cockpit: 'back'});
    byKeep('slate-actions')[0].childNodes[0].click();          // reopen while the check is still on its way
    var during = {banner: byKeep('offline-banner').length,
      off: byKeep('cue-answers')[0].childNodes.map(function (b) { return b.attrs.disabled != null; })};
    var before = sent('api').filter(function (m) { return m.body.op === 'marks.set'; }).length;
    byKeep('cue-answers')[0].childNodes[2].click();
    during.sentWhileOffline = sent('api').filter(function (m) { return m.body.op === 'marks.set'; }).length - before;
    await answerWhere(function (b) { return b.op === 'project.get'; }, 200, %s);
    emit({during: during, after: {banner: byKeep('offline-banner').length,
      off: byKeep('cue-answers')[0].childNodes.map(function (b) { return b.attrs.disabled != null; })}});""" % project_js()))
    assert got["during"]["banner"] == 1 and got["during"]["off"] == [True, True, True] and got["during"]["sentWhileOffline"] == 0
    assert got["after"]["banner"] == 0 and got["after"]["off"] == [False, False, False]


@needs_node
@pytest.mark.parametrize("ui", ["he", "en"])
@pytest.mark.parametrize("view", ["desk", "words"])
def test_a_wording_that_mixes_both_languages_puts_each_on_its_own_line(ui, view):
    p = provider.project("weekly-12")
    p["sentences"][4]["why"] = {ui: "הפתיחה חוזרת. The opening says the same thing twice over again. הקול נשאר."}
    p["sentences"][4]["said"] = {ui: "אז בעצם, so what I really wanted to say here is, בסתיו הימים מתקצרים."}
    marks = {"2": {"mark": "keep"}, "5": {"mark": "tighten", "text": "המשפט AI קצר with a longer English tail here"}}
    then = "byKeep('to-map')[0].click();" if view == "words" else "byClass('dots')[0].childNodes[1].click();"
    els = run(desk_boot(ui, data=json.dumps({"ok": True, "project": p, "marks": marks}), then=then +
                        "emit(allEls().map(function (e) { return {tag: e.tagName, cls: e.className, own: ownText(e), "
                        "lang: e.attrs.lang || null, dir: e.attrs.dir || null, text: textOf(e)}; }));"), ui)
    assert_no_mixed_line(els, ui)
    if ui == "he":  # a short English name stays inside the Hebrew line, isolated; the long English part has its own line
        assert any("⁨AI⁩" in e["own"] and HEBREW.search(e["own"]) for e in els)
        assert any(e["lang"] == "en" and e["dir"] == "ltr" and "with a longer English tail here" in e["own"] for e in els)
