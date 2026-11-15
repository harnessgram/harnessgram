"""The staff board's progress (radio_staff, radio_board.staff_slide/card_projects, radio_paint.staff,
radio_youtube.StaffStation and start).

Structured data only: for each listed project staff see and hear its owner-given label (or a neutral "Project A"),
its live progress cards as numbers and the day's counts, from fixed templates. No free text from a card (title,
step names) or from a model reaches the picture, the voice or YouTube. The guards at the end reproduce every leak
the two privacy reviews of #268 found. All data is sample data; Google, the Keychain and launchd are fakes."""
import json
import time
import urllib.parse

import pytest

from conftest import GOOD_TOKEN
from ctb import cards as C, radio_board as B, radio_paint as P, radio_staff as S, radio_youtube as Y
from ctb.config import Config, ProjectConfig

NOW = time.mktime((2026, 3, 4, 15, 30, 0, 0, 0, -1))
# free text a card can carry (synthetic owner and personal text, sample names): none of it may ever go out
FREE_TEXT = ["סודכרטיס עבודה פנימית", "יונתן מטפל במשכנתה הפרטית", "בודקים את המסך החדש", "contracts review",
             "Meeting Marie"]
FREE_WORDS = ["סודכרטיס", "יונתן", "משכנתה", "בודקים", "contracts", "Marie"]


def card(project="alpha", total=3, at=1, percent=None, updated=NOW - 60):
    """A card as the station keeps it: numbers only."""
    return {"project": project, "total": total, "at": at, "percent": percent, "updated": updated}


def snap(*cards, done=()):
    return {"at": NOW, "done": list(done), "team_open_by": {"alpha": 2}, "staff_cards": list(cards)}


@pytest.fixture
def lists(tmp_path):
    """A sample deny-list (plain terms, one per line) and a child-suitability list."""
    deny = tmp_path / "terms.txt"
    deny.write_text("# sample terms\nסודי ביותר\nwidget\nאקמה\n", encoding="utf-8")
    suit = tmp_path / "suit.json"
    suit.write_text(json.dumps({"pattern": "אלימות|נשק"}), encoding="utf-8")
    return {"deny_terms_file": str(deny), "suitability_file": str(suit)}


def filters(lists, names=("דנה לוי",)):
    c = Config(bot_token=GOOD_TOKEN, owner_chat_id=1, target_language="he")
    c.team.members = {i + 10: n for i, n in enumerate(names)}
    return Y.staff_filters(c, lists, {})


def prof(labels=None, flt=None, projects=("alpha",), title=None, lang="he"):
    p = S.profile({"enabled": True}, lang)
    p["projects"] = {k: (labels or {}).get(k) or k for k in projects}
    p["labels"] = {k: v for k, v in (labels or {}).items() if k in projects}
    p["filters"] = flt or {}
    if title is not None:
        p["title"] = title
    return p


def everything(view):
    """Every output a staff viewer can get: the spoken script, the card, the board slide and the painter's ops."""
    title, sections = S.render_inputs(view)
    sl = B.staff_slide(view)
    ops = P.layout(sl, "/nowhere/staff.png").data()
    return (S.build_script(view), json.dumps([title, sections], ensure_ascii=False), sl,
            json.dumps(ops, ensure_ascii=False))


def blob(view):
    script, card_, sl, ops = everything(view)
    return script + card_ + json.dumps(sl, ensure_ascii=False) + ops


# --- progress as numbers ----------------------------------------------------------------------------------------

def test_progress_rows_come_from_the_cards_as_numbers(lists):
    view = S.staff_view(snap(card(), card(total=2, at=2, updated=NOW - 10)),
                        prof(labels={"alpha": "אלפא"}, flt=filters(lists)))
    p = view["projects"][0]
    assert [(c["done"], c["total"], c["pct"]) for c in p["cards"]] == [(2, 2, 100), (1, 3, 33)]  # newest first
    assert (p["steps_done"], p["steps_total"], p["pct"]) == (3, 5, 66)  # the mean of each card's own percent
    script, card_, sl, ops = everything(view)
    assert "בפרויקט אלפא: שישים ושישה אחוז, שלושה מתוך חמישה שלבים." in script
    assert "אלפא: שישים ושישה אחוז, שלושה מתוך חמישה שלבים" in card_
    row = sl["rows"][0]
    assert row["pct"] == 66 and [(c["done"], c["total"]) for c in row["cards"]] == [(2, 2), (1, 3)]
    assert '"s": "6"' in ops  # the percent is drawn as tiles


def test_spoken_lines_say_only_label_percent_and_counts(lists):
    view = S.staff_view(snap(card(), done=[{"key": "x", "project": "alpha", "updated": NOW - 60}]),
                        prof(labels={"alpha": "אלפא"}, flt=filters(lists)))
    body = S.build_script(view).split("שלום לכל הצוות.")[1]
    assert body.strip().split(". ") == ["בפרויקט אלפא: שלושים ושלושה אחוז, אחד מתוך שלושה שלבים",
                                        "בפרויקט אלפא הושלמה משימה חדשה",
                                        "בפרויקט אלפא יש עכשיו שתי משימות בעבודה."]


def test_a_card_of_a_project_not_listed_is_never_shown(lists):
    view = S.staff_view(snap(card(project="beta", total=7)), prof(flt=filters(lists)))
    assert view["projects"][0]["cards"] == [] and "שבעה" not in everything(view)[0]


def test_labels_come_only_from_the_map_else_project_a_b(lists):
    view = S.staff_view(snap(card()), prof(labels={"beta": "בטא"}, projects=("alpha", "beta", "gamma"),
                                           flt=filters(lists)))
    assert [x["label"] for x in view["projects"]] == ["פרויקט א׳", "בטא", "פרויקט ג׳"]
    script, card_, sl, ops = everything(view)
    assert "alpha" not in script + card_ + ops and "gamma" not in ops
    assert "בפרויקט א׳: שלושים ושלושה אחוז" in script and "פרויקט פרויקט" not in script


def test_settings_take_labels_only_from_the_staff_map(monkeypatch, lists):
    c = Config(bot_token=GOOD_TOKEN, owner_chat_id=1, target_language="he",
               radio={"project_names": {"beta": "שם פרטי של פרויקט"},
                      "youtube": dict({"enabled": True, "projects": {"alpha": "אלפא"}}, **lists)})
    monkeypatch.setattr(Y, "cockpit_prefs", lambda: {"projects": ["alpha", "beta"]})
    monkeypatch.setattr(Y, "catalog_keys", lambda s, names: ["alpha", "beta"])
    s = Y.settings(c)
    assert s["labels"] == {"alpha": "אלפא"}
    assert [x["label"] for x in S.staff_view(snap(), s)["projects"]] == ["אלפא", "פרויקט ב׳"]


# --- no free text from a card ever goes out ---------------------------------------------------------------------

def _station(monkeypatch, lists, projects=None, **extra):
    c = Config(bot_token=GOOD_TOKEN, owner_chat_id=1, target_language="he",
               radio=dict({"youtube": dict({"enabled": True, "projects": projects or {"alpha": "אלפא"}}, **lists)},
                          **extra))
    monkeypatch.setattr(Y, "cockpit_prefs", lambda: {})
    return c, Y.StaffStation(c, service=None, reload=lambda: Y.settings(c))


def raw_card(title, steps, at=0, chat=-1001, visibility="public"):
    """A card that may reach the staff station at all is a public one (cards.is_public); the private ones are
    covered by test_card_visibility_leaks.py."""
    return {"id": "c1", "kind": "progress", "session": "s" * 8, "title": title, "detail": title, "steps": steps,
            "at": at, "percent": None, "updated": NOW, "created": NOW, "state": "open", "chat": chat, "thread": 7,
            "visibility": visibility}


@pytest.mark.parametrize("field", ["title", "step", "detail"])
def test_no_kind_of_card_free_text_reaches_any_output(monkeypatch, lists, field):
    """A card's title, its step names and its detail are never even copied: the station keeps its numbers only,
    and the script, card, board, painter ops (and so the TTS input and the frame) carry none of it."""
    c, st = _station(monkeypatch, lists)
    text = " ".join(FREE_TEXT)
    rc = raw_card(text if field == "title" else "כותרת", [text, "סיום"] if field == "step" else ["א", "ב"])
    if field == "detail":
        rc["detail"] = text
    monkeypatch.setattr(B, "card_projects", lambda cfg: [("alpha", rc)])
    kept = st.staff_cards()
    assert kept == [{"project": "alpha", "total": 2, "at": 0, "percent": None, "updated": NOW}]
    out = blob(S.staff_view(snap(*kept), st.s))
    for word in FREE_WORDS + ["כותרת"]:
        assert word not in out, word


def test_a_full_update_says_and_draws_no_card_text(monkeypatch, lists):
    c, st = _station(monkeypatch, lists)
    Y._set_state(on=True, broadcast_id="bc-1", owner_start="n", since=int(time.time()), start_generation=Y.generation())
    rc = raw_card(FREE_TEXT[1], [FREE_TEXT[2], FREE_TEXT[0]])
    monkeypatch.setattr(B, "card_projects", lambda cfg: [("alpha", rc)])
    monkeypatch.setattr(st, "gather", lambda now: dict(snap(), staff_cards=st.staff_cards()))
    drawn, said = [], []
    monkeypatch.setattr(Y.radio, "board_on", lambda: False)
    monkeypatch.setattr(Y.radio_card, "render", lambda folder, out, title, when, sections, foot, rtl: drawn.append(
        (title, sections)) or True)
    monkeypatch.setattr(st, "speak", lambda text: said.append(text) or True)
    st.update()
    out = json.dumps([drawn, said], ensure_ascii=False)
    assert said and "אפס אחוז, אפס מתוך שני שלבים" in out
    for word in FREE_WORDS:
        assert word not in out, word


def test_there_is_no_free_text_path_left():
    """The summary rewrite and the step-names option are gone from the staff edition."""
    for name in ("safe_line", "summary_sources", "rewrite_prompt", "REWRITE_PROMPT", "LEVELS", "step_of"):
        assert not hasattr(S, name), name
    assert not hasattr(Y.StaffStation, "summaries")
    assert "step_names" not in S.profile({"step_names": True, "content": "summaries"}, "he")


# --- guards: every leak the privacy reviews of #268 reproduced --------------------------------------------------

def test_r1_1_a_display_name_alias_never_relabels_another_projects_card(monkeypatch, lists):
    """Only alpha is listed; the radio's display name for alpha is "beta": a card of beta's group is not alpha's."""
    c, st = _station(monkeypatch, lists, project_names={"alpha": "beta"})
    monkeypatch.setattr(B, "card_projects", lambda cfg: [("beta", raw_card("סודבטא", ["א"])),
                                                         ("alpha", raw_card("כותרת", ["א", "ב", "ג"]))])
    assert [x["total"] for x in st.staff_cards()] == [3]
    # another project's display name folding to a listed key makes that key ambiguous: no card at all
    c2, st2 = _station(monkeypatch, lists, project_names={"gamma": "Alpha"})
    assert st2.staff_cards() == []


def _home_and_group_cfg():
    c = Config(bot_token=GOOD_TOKEN, owner_chat_id=1, target_language="he", chat_id=-1009)
    c.projects = [ProjectConfig(name="alpha", chat_id=-1001, title_prefixes=["alpha"])]
    return c


def test_r2_2_a_home_chat_card_never_enters_a_project_through_its_topic_title(monkeypatch):
    """A card in the owner's home chat under a topic "alpha: private finance" is not alpha's; only a card in alpha's
    own group chat is."""
    c = _home_and_group_cfg()
    home = raw_card("משכנתה פרטית", ["א"], chat=-1009)
    other = raw_card("משהו", ["א"], chat=-1005)
    group = raw_card("כותרת", ["א", "ב"], chat=-1001)
    monkeypatch.setattr(C, "open_cards", lambda: [home, other, group])
    assert B.card_projects(c) == [("alpha", group)]


def test_r2_2_a_project_group_that_is_the_home_chat_admits_nothing(monkeypatch):
    c = _home_and_group_cfg()
    c.projects = [ProjectConfig(name="alpha", chat_id=-1009)]
    monkeypatch.setattr(C, "open_cards", lambda: [raw_card("משכנתה פרטית", ["א"], chat=-1009)])
    assert B.card_projects(c) == []


@pytest.mark.parametrize("rule", [r"\bסודי\s+ביותר\b", r"\bסודי\x20ביותר\b", r"סודי\Sביותר", r"(?:סודי)\W+ביותר",
                                  "סודי[ -]ביותר", "סודי.ביותר", r"\s*"])
def test_r1_2_r2_1_a_deny_rule_that_is_not_a_plain_term_is_refused_and_stops_the_edition(monkeypatch, tmp_path,
                                                                                         lists, rule):
    deny = tmp_path / "rx.txt"
    deny.write_text("אקמה\n" + rule + "\n", encoding="utf-8")
    flt = Y.staff_filters(Config(bot_token=GOOD_TOKEN, owner_chat_id=1), dict(lists, deny_terms_file=str(deny)), {})
    assert flt["broken"] and flt["reason"] == Y.RULES_INVALID and not S.filters_ready({"filters": flt})
    # start creates nothing on YouTube
    c, st = _station(monkeypatch, dict(lists, deny_terms_file=str(deny)))
    with pytest.raises(Y.YouTubeError) as e:
        Y.start(c, service=object(), load_job=lambda s: pytest.fail("no job"))
    assert e.value.key == "youtube_rules_invalid"
    # a running job ends at its next round
    Y._set_state(on=True, broadcast_id="bc-1", owner_start="n", since=int(time.time()), start_generation=Y.generation())
    assert st.refresh() is False and st.ending == "rules"


@pytest.mark.parametrize("label", ["ס ו ד י ב י ו ת ר", "סודי-ביותר", "ס.ו.ד.י ביותר", "שיחה עם א.ק.מ.ה"])
def test_r1_2_plain_terms_hold_against_spacing_and_punctuation_in_labels(lists, label):
    view = S.staff_view(snap(card()), prof(labels={"alpha": label}, flt=filters(lists)))
    assert view["projects"][0]["label"] == "פרויקט א׳" and label not in blob(view)


@pytest.mark.parametrize("name,text,lang", [("דן", "רדיו ד ן", "he"), ("דן", "רדיו לדן", "he"),
                                            ("דן", "רדיו דן", "he"), ("Anne-Marie Smith", "Meeting Marie", "en"),
                                            ("שירה-לי כהן", "רדיו לי", "he")])
def test_r1_3_r2_5_r2_6_member_names_block_titles_and_labels(lists, name, text, lang):
    """Every part of a member's name (split on spaces, hyphens and other marks, two letters or more) is matched on
    the normal form: in the station title and in a staff label."""
    flt = filters(lists, names=(name,))
    view = S.staff_view(snap(card()), prof(labels={"alpha": text}, flt=flt, title=text, lang=lang))
    out = blob(view)
    assert text not in out and view["projects"][0]["label"] in ("פרויקט א׳", "Project A")


def test_r1_4_r2_4_a_denied_station_title_is_never_said_drawn_painted_or_sent_to_youtube(monkeypatch, lists):
    p = prof(labels={"alpha": "אלפא"}, flt=filters(lists), title="רדיו סודי ביותר")
    view = S.staff_view(snap(card()), p)
    view["title"] = "רדיו סודי ביותר"   # even a title slipped into the view after it was built
    script, card_, sl, ops = everything(view)
    assert "ביותר" not in script + card_ + ops and sl["title"] == S.TITLE_HE
    assert S.render_inputs(view)[0] == "📻 " + S.TITLE_HE
    # the YouTube broadcast metadata uses the same gated title
    import test_radio_youtube as T
    c = Config(bot_token=GOOD_TOKEN, owner_chat_id=1, target_language="he",
               radio={"youtube": dict({"enabled": True, "projects": {"alpha": "אלפא"}, "title": "רדיו סודי ביותר"},
                                      **lists)})
    g = T.FakeGoogle()
    with Y.connection_lock():
        Y._set_connection(connected=True, channel_id="UCsample", channel_title="Sample channel", live_enabled=True)
    monkeypatch.setattr(Y, "_job_on", lambda: False)
    assert Y.start(c, T.service(c, g, T.Store(**{Y.REFRESH: T.REFRESH_TOKEN})), load_job=lambda s: None) == \
        "youtube_started"
    sent = json.dumps(g.did("POST", "/liveBroadcasts")[0][3], ensure_ascii=False)
    assert "ביותר" not in sent and S.TITLE_HE in sent


def test_r2_5_without_the_lists_the_title_is_the_built_in_one(lists):
    for flt in ({}, filters(dict(lists, deny_terms_file="/nowhere/terms.txt"))):
        view = S.staff_view(snap(), prof(flt=flt, title="רדיו שלנו"))
        assert view["title"] == S.TITLE_HE and "שלנו" not in blob(view)
    assert S.staff_view(snap(), prof(flt=filters(lists), title="רדיו שלנו"))["title"] == "רדיו שלנו"


@pytest.mark.parametrize("field,failure", [
    ("deny_terms_file", "missing"), ("deny_terms_file", "unreadable"),
    ("deny_terms_file", "invalid_utf8"), ("deny_terms_file", "invalid_rule"),
    ("suitability_file", "missing"), ("suitability_file", "unreadable"),
    ("suitability_file", "invalid_utf8"), ("suitability_file", "invalid_rule"),
])
def test_failed_filters_neutralize_labels_in_a_running_station(monkeypatch, tmp_path, lists, field, failure):
    from ctb import policy

    c, st = _station(monkeypatch, lists, projects={"alpha": "סודי ביותר"})
    seen = []
    monkeypatch.setattr(st, "gather", lambda now: snap(card()))
    monkeypatch.setattr(st, "draw", lambda view, generation: seen.append(view) or True)
    monkeypatch.setattr(st, "speak", lambda text: True)
    monkeypatch.setattr(policy, "on", lambda: True)
    Y._set_state(on=True, broadcast_id="bc-1", owner_start="n", since=int(time.time()), start_generation=Y.generation())
    st.update()
    assert seen[-1]["projects"][0]["label"] == "פרויקט א׳"  # valid deny list blocks this label

    # A harmless-looking label can contain an unlisted name or unsuitable text if the lists fail.
    c.radio["youtube"]["projects"]["alpha"] = "שם פרטי"
    path = tmp_path / (field + failure)
    if failure == "unreadable":
        path.mkdir()
    elif failure == "invalid_utf8":
        path.write_bytes(b"\xff")
    elif failure == "invalid_rule":
        path.write_text("bad.*" if field == "deny_terms_file" else '{"pattern": "["}', encoding="utf-8")
    c.radio["youtube"][field] = str(path)
    st.update()
    if failure == "invalid_rule" and field == "deny_terms_file":
        assert st.ending == "rules"
    else:
        assert seen[-1]["projects"][0]["label"] == "פרויקט א׳"
        assert "שם פרטי" not in blob(seen[-1])


@pytest.mark.parametrize("total,words", [(13, "thirteen"), (14, "fourteen"), (15, "fifteen"),
                                        (16, "sixteen"), (17, "seventeen"), (18, "eighteen")])
def test_english_voice_says_exact_total_for_three_cards(lists, total, words):
    cards = [card(total=total // 3 + (i < total % 3), at=1, updated=NOW - i) for i in range(3)]
    view = S.staff_view(snap(*cards), prof(labels={"alpha": "Alpha"}, flt=filters(lists), lang="en"))
    script = S.build_script(view)
    assert f"three of {words} steps" in script
    assert "many steps" not in script


def test_eight_project_board_displays_both_counts_without_overlapping_cards(lists):
    keys = [f"p{i}" for i in range(8)]
    sn = {"at": NOW, "done": [{"key": k, "project": k, "updated": NOW} for k in keys],
          "team_open_by": {k: 2 for k in keys},
          "staff_cards": [card(project=k, total=4, at=1, updated=NOW - j) for k in keys for j in range(3)]}
    sl = B.staff_slide(S.staff_view(sn, prof(flt=filters(lists), projects=tuple(keys))))
    ops = P.layout(sl, "/nowhere/staff.png").data()["ops"]
    t, _ = P._lang(sl)
    for label in (t["done_today"], t["in_work"]):
        assert len([op for op in ops if op["op"] == "text" and op.get("s") == label]) == 8
    for x, y, w, h in P._grid(8):
        counts = [op for op in ops if op["op"] == "text" and op.get("s") in (t["done_today"], t["in_work"])
                  and x <= op["x"] < x + w and y <= op["y"] < y + h]
        assert len(counts) == 2 and min(op["y"] for op in counts) > y + 116 + 2 * 22 + 9
        assert max(op["y"] + op["h"] for op in counts) <= y + h


def test_r1_5_the_cards_own_percent_wins_over_its_shown_steps(lists):
    view = S.staff_view(snap(card(total=4, at=2, percent=80)), prof(labels={"alpha": "אלפא"}, flt=filters(lists)))
    p = view["projects"][0]
    assert p["cards"][0]["pct"] == 80 and p["pct"] == 80
    script, card_, sl, ops = everything(view)
    assert "שמונים אחוז" in script and "חמישים" not in script
    assert sl["rows"][0]["pct"] == 80 and sl["rows"][0]["cards"][0]["pct"] == 80


def test_automatic_card_does_not_report_truncated_counts(monkeypatch, lists):
    _, st = _station(monkeypatch, lists)
    todos = [{"content": f"Task {i}", "status": "completed" if i < 5 else "pending"} for i in range(11)]
    _, _, body = C._todo_body(todos)
    auto = dict(raw_card("title", body["steps"], at=body["at"]), automatic=True, percent=body["percent"])
    monkeypatch.setattr(B, "card_projects", lambda cfg: [("alpha", auto)])
    kept = st.staff_cards()
    view = S.staff_view(snap(*kept, card(total=2, at=1)), st.s)
    project = view["projects"][0]
    assert kept[0]["percent"] == 45 and kept[0]["total"] == 0
    assert project["steps_total"] == 0
    assert "מתוך" not in S.progress_line(project, True)
    assert B.staff_slide(view)["rows"][0]["cards"][0]["total"] == 0


def test_r1_6_the_newest_cards_are_shown_and_counted(lists):
    cards = [card(total=2, at=i % 2, updated=NOW - 600 + i * 60) for i in range(6)]
    view = S.staff_view(snap(*cards), prof(flt=filters(lists)))
    assert len(view["projects"][0]["cards"]) == S.MAX_CARDS
    assert view["projects"][0]["steps_total"] == 2 * S.MAX_CARDS


@pytest.mark.parametrize("n", [1, 5, 6, 8])
def test_r2_7_every_card_that_is_counted_is_drawn(lists, n):
    """With up to eight projects of three cards each, the painter draws a bar and a done/total for every card the
    spoken line counts (the compact layout too)."""
    keys = [f"p{i}" for i in range(n)]
    sn = {"at": NOW, "done": [], "team_open_by": {},
          "staff_cards": [card(project=k, total=4, at=1, updated=NOW - j) for k in keys for j in range(5)]}
    view = S.staff_view(sn, prof(flt=filters(lists), projects=tuple(keys)))
    counted = sum(len(p["cards"]) for p in view["projects"])
    assert counted == n * S.MAX_CARDS
    sl = B.staff_slide(view)
    assert sum(len(r["cards"]) for r in sl["rows"]) == counted
    ops = P.layout(sl, "/nowhere/staff.png").data()["ops"]
    bars = [op for op in ops if op["op"] == "fill" and op.get("c") == P.LINE and op.get("h") in (14, 9)]
    assert len(bars) == counted
    assert all(op["y"] + op["h"] <= P.FOOT_Y for op in ops if op["op"] == "fill" and op.get("c") == P.MINT)


def test_r2_3_owner_and_personal_text_has_no_way_out(monkeypatch, lists):
    """The reproduction of round 2: synthetic personal text that no list names. It used to survive as a summary;
    there is no summary any more, and a card's text is never kept."""
    c, st = _station(monkeypatch, lists)
    monkeypatch.setattr(B, "card_projects", lambda cfg: [("alpha", raw_card(FREE_TEXT[1], [FREE_TEXT[1]]))])
    out = blob(S.staff_view(snap(*st.staff_cards()), st.s))
    assert "יונתן" not in out and "משכנתה" not in out


def test_word_bounded_and_optional_space_lines_count_as_plain_terms():
    # The shared private list (also read by the hygiene test as regular expressions) has always used these two forms.
    from ctb import radio_staff as R
    assert R.literal_term(r"\bsecretword\b") == "secretword"
    assert R.literal_term("two ?words") == "twowords"
    for refused in (r"a.*b", r"\ba\b\bb\b", r"x|y", r"(ab)", r"\d+"):
        assert R.literal_term(refused) is None
