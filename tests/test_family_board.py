"""The family home board: the store, the day and week resets, Shabbat, the panel routes and /shop."""
import http.client
import json
import os
import stat
import threading
import time
from datetime import datetime, timezone

import pytest

from conftest import GOOD_TOKEN
from ctb import family_board as fb
from ctb import panel
from ctb.config import Config, state_dir
from test_panel import OWNER, signed
from test_schedule import say


def cfg(**kw):
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, **kw)


def at(y, m, d, h=10, mi=0):
    """A UTC instant; Jerusalem is UTC+2 in November, so 10:00 UTC is 12:00 there."""
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)


SUN = at(2026, 11, 15)   # Sunday 15 November 2026
MON, WED, FRI = at(2026, 11, 16), at(2026, 11, 18), at(2026, 11, 20)
SAT = at(2026, 11, 21)


def chore(view, day, text_part):
    return next(r for r in view["days"][day]["chores"] if text_part in r["text"])


# --- the starting lists -------------------------------------------------------------------------------------------------

def test_a_new_board_shows_the_hebrew_starting_lists_and_writes_nothing():
    v = fb.view(cfg(), SUN)
    assert not (state_dir() / fb.FILE).exists()
    assert [len(d["chores"]) for d in v["days"]] == [3, 1, 1, 1, 1, 1, 1]
    assert [r["text"] for r in v["days"][0]["chores"]] == ["לימודים בערב", "כביסות (ילדים/בגדים)", "הכנת סל קניות לשבוע"]
    assert v["days"][6]["chores"][0]["text"].startswith("כביסה קלה מהשבת")
    assert len(v["daily"]) == 5 and len(v["shop"]) == 6 and v["meals"] == []
    assert "ירקות ופירות" in [r["text"] for r in v["shop"]] and "כלים" in [r["text"] for r in v["daily"]]
    # the same ids before and after the first save
    before = [r["id"] for r in v["daily"]]
    fb.add("shop", "חלב")
    assert [r["id"] for r in fb.view(cfg(), SUN)["daily"]] == before


def test_every_starting_item_fits_the_text_cap():
    lists = fb.defaults()
    assert all(len(r["text"]) <= fb.MAX_TEXT for rows in lists.values() for r in rows)
    assert all(len(rows) <= fb.CAPS[k] for k, rows in lists.items())


# --- the resets ---------------------------------------------------------------------------------------------------------

def test_a_daily_task_resets_the_next_day_and_a_weekly_chore_the_next_week():
    v = fb.view(cfg(), WED)
    daily, weekly = v["daily"][0]["id"], chore(v, 3, "כביסות")["id"]
    fb.check(cfg(), daily, True, WED)
    fb.check(cfg(), weekly, True, WED)
    v = fb.view(cfg(), WED)
    assert v["daily"][0]["done"] and chore(v, 3, "כביסות")["done"] and v["open"] == {"chores": 0, "daily": 4}
    thu = fb.view(cfg(), at(2026, 11, 19))
    assert not thu["daily"][0]["done"]                       # a new day
    assert chore(thu, 3, "כביסות")["done"]                    # the same week
    assert chore(fb.view(cfg(), at(2026, 11, 20)), 3, "כביסות")["done"]
    nxt = fb.view(cfg(), at(2026, 11, 22))                    # Sunday: a new week
    assert not chore(nxt, 3, "כביסות")["done"] and nxt["week_start"] == "2026-11-22"
    fb.check(cfg(), daily, False, WED)
    assert not fb.view(cfg(), WED)["daily"][0]["done"]


def test_the_week_starts_on_sunday_in_the_jerusalem_zone():
    # Saturday 22:30 UTC is already Sunday 00:30 in Jerusalem: a new week, and Shabbat is over.
    v = fb.view(cfg(), at(2026, 11, 14, 22, 30))
    assert (v["today"], v["day"], v["week_start"], v["shabbat"]) == ("2026-11-15", 0, "2026-11-15", False)
    # Friday 22:30 UTC is Saturday 00:30 there, in the previous week and inside Shabbat
    v = fb.view(cfg(), at(2026, 11, 13, 22, 30))
    assert (v["today"], v["day"], v["week_start"], v["shabbat"]) == ("2026-11-14", 6, "2026-11-08", True)


def test_a_zone_change_moves_the_day_boundary():
    instant = at(2026, 11, 14, 22, 30)
    assert fb.view(cfg(timezone="UTC"), instant)["day"] == 6
    assert fb.view(cfg(timezone="Asia/Jerusalem"), instant)["day"] == 0


def test_sunday_through_saturday_numbering():
    assert [fb.sunday_index(d.date()) for d in (SUN, MON, WED, FRI, SAT)] == [0, 1, 3, 5, 6]


def test_a_shopping_tick_never_resets():
    ident = fb.view(cfg(), SUN)["shop"][0]["id"]
    fb.check(cfg(), ident, True, SUN)
    assert fb.view(cfg(), at(2027, 2, 3))["shop"][0]["done"]


# --- Shabbat ------------------------------------------------------------------------------------------------------------

def test_on_shabbat_no_chores_are_offered_and_ticks_are_refused():
    ident = fb.view(cfg(), SAT)["days"][6]["chores"][0]["id"]
    v = fb.view(cfg(), SAT)
    assert v["shabbat"] and v["open"] == {"chores": 0, "daily": 0} and v["shabbat_until"]
    assert v["days"][6]["chores"]                                        # the column is still readable
    with pytest.raises(fb.BoardError) as exc:
        fb.check(cfg(), ident, True, SAT)
    assert exc.value.code == "shabbat"
    with pytest.raises(fb.BoardError):
        fb.check(cfg(), v["daily"][0]["id"], True, SAT)
    fb.check(cfg(), v["shop"][0]["id"], True, SAT)                       # the shopping list is not a chore
    fb.add("daily", "x"); fb.edit(ident, "כביסה בערב")                    # editing is not ticking
    assert not chore(fb.view(cfg(), SAT), 6, "כביסה בערב")["done"]


def local(y, m, d, h, mi=0):
    from zoneinfo import ZoneInfo
    return datetime(y, m, d, h, mi, tzinfo=ZoneInfo("Asia/Jerusalem"))


@pytest.mark.parametrize("label,friday,margins", [("winter", (2026, 12, 18), (16, 17)), ("summer", (2027, 6, 18), (19, 20))])
def test_friday_before_candle_lighting_is_ordinary_and_after_it_is_shabbat(label, friday, margins):
    y, m, d = friday
    cl_hour = margins[0]
    before, after = local(y, m, d, cl_hour - 2), local(y, m, d, 23)
    assert not fb.view(cfg(), before)["shabbat"]
    fb.check(cfg(), fb.view(cfg(), before)["days"][5]["chores"][0]["id"], True, before)       # Friday afternoon works
    v = fb.view(cfg(), after)
    assert v["shabbat"] and v["open"] == {"chores": 0, "daily": 0}
    with pytest.raises(fb.BoardError) as exc:
        fb.check(cfg(), v["daily"][0]["id"], True, after)
    assert exc.value.code == "shabbat"


@pytest.mark.parametrize("label,saturday,hours", [("winter", (2026, 12, 19), (17, 18)), ("summer", (2027, 6, 19), (20, 21))])
def test_saturday_chores_open_after_nightfall_in_the_same_week(label, saturday, hours):
    y, m, d = saturday
    during, after = local(y, m, d, hours[0] - 3), local(y, m, d, hours[1] + 1)
    assert fb.view(cfg(), during)["shabbat"]
    v = fb.view(cfg(), after)
    assert not v["shabbat"] and v["day"] == 6 and v["open"]["chores"] == 1 and v["open"]["daily"] == 5
    motzaei = v["days"][6]["chores"][0]
    fb.check(cfg(), motzaei["id"], True, after)
    now = fb.view(cfg(), after)
    assert now["days"][6]["chores"][0]["done"] and now["week_start"] == v["week_start"]
    # the next morning is a new week: the tick no longer shows, and Sunday's chores are theirs to tick
    sunday = fb.view(cfg(), local(y, m, d + 1, 9))
    assert sunday["week_start"] != v["week_start"] and not sunday["days"][6]["chores"][0]["done"] and not sunday["shabbat"]
    fb.check(cfg(), sunday["days"][0]["chores"][0]["id"], True, local(y, m, d + 1, 9))
    assert fb.view(cfg(), local(y, m, d + 1, 9))["days"][0]["chores"][0]["done"]


def test_the_shabbat_margins_come_from_the_config():
    from ctb import shabbat_times as st
    d = (2026, 12, 18)
    default = st.window(__import__("datetime").date(*d), cfg())
    short = st.window(__import__("datetime").date(*d), cfg(family_shabbat={"candle_minutes": 18, "nightfall_minutes": 72}))
    assert (default[0] - short[0]).total_seconds() == -22 * 60 and (short[1] - default[1]).total_seconds() == 30 * 60


def test_friday_before_candle_lighting_is_an_ordinary_day():
    v = fb.view(cfg(), FRI)
    assert not v["shabbat"] and v["open"]["chores"] == 1
    fb.check(cfg(), v["days"][5]["chores"][0]["id"], True, FRI)


# --- the store ----------------------------------------------------------------------------------------------------------

def test_the_file_is_private_and_written_atomically():
    fb.add("shop", "חלב")
    path = state_dir() / fb.FILE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1
    assert not [p for p in state_dir().iterdir() if p.name.startswith(fb.FILE + ".")]    # no temp file left behind


def test_a_failed_write_keeps_the_old_file(monkeypatch):
    fb.add("shop", "חלב")
    before = (state_dir() / fb.FILE).read_text(encoding="utf-8")
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(fb.BoardError) as exc:
        fb.add("shop", "ביצים")
    assert exc.value.code == "unavailable"
    monkeypatch.setattr(os, "replace", real)
    assert (state_dir() / fb.FILE).read_text(encoding="utf-8") == before


@pytest.mark.parametrize("kind", ["weekly", "daily", "shop", "meals"])
def test_every_list_is_capped(kind):
    day = 2 if kind == "weekly" else None
    start = len(fb.defaults()[kind]) if kind != "weekly" else len([r for r in fb.defaults()["weekly"]])
    for n in range(fb.CAPS[kind] - start):
        fb.add(kind, "item %d" % n, day)
    with pytest.raises(fb.BoardError) as exc:
        fb.add(kind, "one too many", day)
    assert exc.value.code == "full"
    assert len(fb._read()[kind]) == fb.CAPS[kind]


def test_a_hand_edited_file_is_cut_to_the_caps_and_bad_rows_are_dropped():
    rows = [{"id": "%012x" % n, "text": "t%d" % n, "day": None, "done": None} for n in range(500)]
    bad = [{"id": "zz", "text": "x"}, {"id": "a" * 12, "text": "   "}, 5, {"id": "b" * 12, "text": "y", "day": 9}]
    (state_dir()).mkdir(parents=True, exist_ok=True)
    (state_dir() / fb.FILE).write_text(json.dumps({"version": 1, "lists": {"shop": rows, "weekly": bad}}))
    lists = fb._read()
    assert len(lists["shop"]) == fb.CAPS["shop"] and lists["weekly"] == [] and lists["daily"] == []


def test_a_damaged_file_falls_back_to_the_starting_lists():
    (state_dir()).mkdir(parents=True, exist_ok=True)
    (state_dir() / fb.FILE).write_text("{not json")
    assert len(fb.view(cfg(), SUN)["daily"]) == 5


@pytest.mark.parametrize("text", ["", "   ", "x" * 101, 5, None, "\x00\x01"])
def test_bad_text_is_refused(text):
    with pytest.raises(fb.BoardError) as exc:
        fb.add("shop", text)
    assert exc.value.code == "text"


def test_text_is_tidied_and_duplicates_are_not_added_twice():
    first = fb.add("shop", "  חלב \n  3%\t")
    assert first["text"] == "חלב 3%"
    assert fb.add("shop", "חלב 3%")["id"] == first["id"]
    assert [r["text"] for r in fb._read()["shop"]].count("חלב 3%") == 1


def test_weekly_needs_a_valid_weekday_and_others_take_none():
    for bad in (None, -1, 7, True, "1"):
        with pytest.raises(fb.BoardError):
            fb.add("weekly", "x", bad)
    with pytest.raises(fb.BoardError):
        fb.add("shop", "x", 2)
    assert fb.add("meals", "שניצל", 4)["day"] == 4 and fb.add("meals", "פסטה")["day"] is None
    with pytest.raises(fb.BoardError):
        fb.add("nope", "x")


def test_rename_move_delete_and_the_undo_shape():
    row = fb.add("weekly", "לנקות את המרפסת", 1)
    fb.edit(row["id"], "לשטוף את המרפסת")
    assert fb.edit(row["id"], day=3)["day"] == 3
    assert fb.edit(row["id"], "ללא יום")["day"] == 3                    # day left alone unless given
    gone = fb.remove(row["id"])
    assert gone["list"] == "weekly" and gone["day"] == 3
    again = fb.add(gone["list"], gone["text"], gone["day"])
    assert again["text"] == "ללא יום"
    with pytest.raises(fb.BoardError) as exc:
        fb.remove(row["id"])
    assert exc.value.code == "unknown"
    for bad in ("../x", "zz", 5, None):
        with pytest.raises(fb.BoardError):
            fb.edit(bad, "x")


def test_clear_checked_removes_only_the_checked_shopping_items():
    shop = fb.view(cfg(), SUN)["shop"]
    fb.check(cfg(), shop[0]["id"], True, SUN)
    fb.check(cfg(), shop[1]["id"], True, SUN)
    assert fb.clear_checked() == 2
    assert [r["text"] for r in fb.view(cfg(), SUN)["shop"]] == [r["text"] for r in shop[2:]]
    assert fb.clear_checked() == 0


def test_check_needs_a_real_boolean():
    ident = fb.view(cfg(), SUN)["daily"][0]["id"]
    for bad in ("yes", 1, None):
        with pytest.raises(fb.BoardError):
            fb.check(cfg(), ident, bad, SUN)
    with pytest.raises(fb.BoardError) as exc:
        fb.check(cfg(), "a" * 12, True, SUN)
    assert exc.value.code == "unknown"


def test_the_view_carries_no_other_data_than_the_board():
    v = fb.view(cfg(), SUN)
    assert set(v) == {"today", "week_start", "day", "shabbat", "shabbat_until", "days", "daily", "shop", "meals", "open", "caps"}


# --- the panel routes ---------------------------------------------------------------------------------------------------

@pytest.fixture
def srv():
    server = panel.PanelServer(("127.0.0.1", 0), cfg())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def post(server, body, path="/api/family", init="ok"):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=20)
    headers = {}
    if init is not None:
        headers[panel.INIT_HEADER] = signed() if init == "ok" else init
    payload = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
    conn.request("POST", path, body=payload, headers=headers)
    r = conn.getresponse()
    data = r.read()
    conn.close()
    return r.status, json.loads(data)


def test_the_routes_are_declared_with_small_bodies():
    assert panel.ROUTES["/api/family"] <= 4096 and panel.ACTIONS["/api/family"] == "_family"
    assert panel.ROUTES["/api/family-view"] <= 1024 and panel.ACTIONS["/api/family-view"] == "_family_view"


@pytest.mark.parametrize("path,body", [("/api/family", {"action": "add", "list": "shop", "text": "x"}), ("/api/family-view", {})])
def test_the_owner_login_is_required(srv, path, body):
    for init in (None, "", "garbage", signed(tamper=True), signed(user_id=OWNER + 1),
                 signed(auth_date=int(time.time()) - panel.MAX_AGE - 30)):
        code, out = post(srv, body, path=path, init=init)
        assert code == 403 and out == {"error": "forbidden"}, init
    assert not (state_dir() / fb.FILE).exists()


def test_changes_need_a_fresh_login_but_the_read_accepts_an_hour_old_one(srv):
    old = signed(auth_date=int(time.time()) - panel.DECIDE_MAX_AGE - 60)
    assert post(srv, {"action": "add", "list": "shop", "text": "x"}, init=old)[0] == 403
    assert not (state_dir() / fb.FILE).exists()
    assert post(srv, {}, path="/api/family-view", init=old)[0] == 200


def test_check_add_edit_remove_and_clear_through_the_server(srv):
    code, out = post(srv, {}, path="/api/family-view")
    assert code == 200 and len(out["view"]["shop"]) == 6
    code, out = post(srv, {"action": "add", "list": "shop", "text": "חלב"})
    assert code == 200 and out["item"]["text"] == "חלב" and out["item"]["list"] == "shop"
    ident = out["item"]["id"]
    assert post(srv, {"action": "check", "id": ident, "done": True})[1]["item"]["done"] == "-"
    view = post(srv, {"action": "edit", "id": ident, "text": "חלב 3%"})[1]["view"]
    assert [r for r in view["shop"] if r["id"] == ident][0]["text"] == "חלב 3%"
    code, out = post(srv, {"action": "clear"})
    assert code == 200 and out["cleared"] == 1 and ident not in [r["id"] for r in out["view"]["shop"]]
    code, out = post(srv, {"action": "add", "list": "weekly", "text": "גינה", "day": 2})
    assert code == 200 and out["item"]["day"] == 2
    code, out = post(srv, {"action": "remove", "id": out["item"]["id"]})
    assert code == 200 and out["removed"]["text"] == "גינה" and out["removed"]["list"] == "weekly" and out["removed"]["day"] == 2


def test_a_tick_is_a_real_boolean_in_the_view(srv):
    daily = post(srv, {}, path="/api/family-view")[1]["view"]["daily"][0]["id"]
    now = datetime.now(timezone.utc)
    if fb.is_shabbat(cfg(), now):
        assert post(srv, {"action": "check", "id": daily, "done": True}) == (409, {"error": "shabbat"})
        return
    view = post(srv, {"action": "check", "id": daily, "done": True})[1]["view"]
    assert view["daily"][0]["done"] is True
    assert post(srv, {"action": "check", "id": daily, "done": False})[1]["view"]["daily"][0]["done"] is False


@pytest.mark.parametrize("body", [
    {}, {"action": "nope"}, {"action": 5}, {"action": "add"}, {"action": "add", "list": "shop"}, {"action": "add", "text": "x"},
    {"action": "add", "list": "shop", "text": ""}, {"action": "add", "list": "shop", "text": "x" * 101},
    {"action": "add", "list": "shop", "text": "x", "extra": 1}, {"action": "add", "list": "weekly", "text": "x"},
    {"action": "add", "list": "weekly", "text": "x", "day": 7}, {"action": "add", "list": "bogus", "text": "x"},
    {"action": "check", "id": "a" * 12}, {"action": "check", "id": "a" * 12, "done": "yes"},
    {"action": "check", "id": "../x", "done": True}, {"action": "edit", "id": "a" * 12}, {"action": "edit", "text": "x"},
    {"action": "remove"}, {"action": "remove", "id": "a" * 12, "x": 1}, {"action": "clear", "id": "a" * 12}, b"not json", b"[1]"])
def test_bad_bodies_are_400_and_change_nothing(srv, body):
    code, out = post(srv, body)
    assert code == 400, (body, code, out)
    assert not (state_dir() / fb.FILE).exists()


def test_unknown_ids_are_404_a_full_list_is_409_and_the_view_body_is_empty(srv):
    assert post(srv, {"action": "check", "id": "a" * 12, "done": True})[0] == 404
    assert post(srv, {"action": "edit", "id": "a" * 12, "text": "x"})[0] == 404
    assert post(srv, {"action": "remove", "id": "a" * 12})[0] == 404
    assert post(srv, {"x": 1}, path="/api/family-view")[0] == 400
    for n in range(fb.CAPS["daily"] - 5):
        fb.add("daily", "d%d" % n)
    assert post(srv, {"action": "add", "list": "daily", "text": "more"}) == (409, {"error": "full"})


def test_a_chore_tick_on_shabbat_is_409(srv, monkeypatch):
    real = fb._local
    monkeypatch.setattr(fb, "_local", lambda c, n=None: real(c, SAT))
    monkeypatch.setattr(fb, "_instant", lambda n=None: SAT)
    ident = fb.view(cfg(), SAT)["days"][6]["chores"][0]["id"]
    assert post(srv, {"action": "check", "id": ident, "done": True}) == (409, {"error": "shabbat"})


def test_the_state_poll_carries_no_family_board(srv):
    fb.add("shop", "פריט פרטי מאוד")
    code, out = post(srv, {}, path="/api/state")
    assert code == 200 and "פריט פרטי מאוד" not in json.dumps(out, ensure_ascii=False) and "family" not in out


# --- /shop --------------------------------------------------------------------------------------------------------------

def test_shop_adds_and_answers():
    replies = []
    assert fb.shop_command(cfg(ui_language="he"), "חלב", replies.append) == "shop"
    assert replies[0].splitlines()[-1] == "חלב" and "חלב" in [r["text"] for r in fb._read()["shop"]]


@pytest.mark.parametrize("arg,key", [("", "shop_usage"), ("x" * 101, "shop_bad")])
def test_shop_without_a_usable_line_explains_and_stores_nothing(arg, key):
    from ctb.i18n import t
    replies = []
    fb.shop_command(cfg(ui_language="en"), arg, replies.append)
    assert replies == [t(key, lang="en")] and "/shop" in replies[0]
    assert not (state_dir() / fb.FILE).exists()


def test_shop_command_stores_for_the_owner_and_never_reaches_a_session(tmp_path):
    from test_team import Rig
    rig = Rig(tmp_path)
    assert say(rig, "/shop ביצים") == "shop"
    assert "ביצים" in [r["text"] for r in fb._read()["shop"]]
    assert rig.bot.methods("sendMessage") and "ביצים" in rig.bot.methods("sendMessage")[-1]["text"]
    assert say(rig, "/shop@somebot לחם") == "shop" and say(rig, "/shop") == "shop"
    assert len([r for r in fb._read()["shop"] if r["text"] in ("ביצים", "לחם")]) == 2


def test_shop_command_is_ignored_for_anyone_but_the_owner(tmp_path):
    from test_team import Rig
    rig = Rig(tmp_path)
    assert say(rig, "/shop secret", uid=99) != "shop"
    assert say(rig, "/shop secret", chat=-777, chat_type="supergroup") != "shop"
    assert not (state_dir() / fb.FILE).exists()


def test_shop_refuses_a_line_that_looks_like_a_secret(tmp_path):
    from test_team import Rig
    rig = Rig(tmp_path)
    assert say(rig, "/shop ghp_" + "a" * 36) == "refused"
    assert not (state_dir() / fb.FILE).exists()


def test_todo_is_unchanged(tmp_path):
    from ctb import schedule
    from test_team import Rig
    rig = Rig(tmp_path)
    assert say(rig, "/todo לקנות חלב") == "todo" and len(schedule.items()) == 1


# --- the print page -----------------------------------------------------------------------------------------------------

def get(server, path, init=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=20)
    conn.request("GET", path)
    r = conn.getresponse()
    data = r.read()
    conn.close()
    return r.status, dict(r.getheaders()), data


def link(srv, init="ok"):
    code, out = post(srv, {}, path="/api/family-print", init=init)
    return code, out


def test_the_print_link_needs_the_fresh_owner_login(srv):
    assert panel.ROUTES["/api/family-print"] <= 1024 and panel.ACTIONS["/api/family-print"] == "_family_print"
    for init in (None, "", "garbage", signed(tamper=True), signed(user_id=OWNER + 1),
                 signed(auth_date=int(time.time()) - panel.DECIDE_MAX_AGE - 60)):
        assert link(srv, init)[0] == 403, init
    assert post(srv, {"x": 1}, path="/api/family-print")[0] == 400
    code, out = link(srv)
    assert code == 200 and out["url"].startswith("/family/print?auth=fam1.")
    assert out["expires"] <= time.time() + 601


def test_the_print_page_is_owner_only_hebrew_no_images_and_escaped(srv):
    fb.add("shop", "<script>alert(1)</script> & חלב")
    url = link(srv)[1]["url"]
    code, headers, body = get(srv, url)
    html = body.decode()
    assert code == 200 and headers["Content-Type"].startswith("text/html") and headers["Cache-Control"] == "private, no-store"
    assert 'lang="he" dir="rtl"' in html and "הלוח המשפחתי שלנו" in html and "<img" not in html and "<svg" not in html
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;alert(1)&lt;/script&gt; &amp; חלב" in html
    csp = headers["Content-Security-Policy"]
    nonce = csp.split("'nonce-")[1].split("'")[0]
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp and html.count('nonce="%s"' % nonce) == 2
    assert html.index("רשימת קניות") > html.index('class="grid low"')          # the shopping list is on page 2


@pytest.mark.parametrize("make", [
    lambda srv: "/family/print", lambda srv: "/family/print?auth=",
    lambda srv: "/family/print?auth=fam1.%d.%d.%s" % (OWNER, int(time.time()) + 300, "0" * 64),
    lambda srv: "/family/print?auth=fam1.%d.%d.%s" % (OWNER + 1, int(time.time()) + 300, "0" * 64),
    lambda srv: link(srv)[1]["url"] + "&auth=x", lambda srv: "/family/print?auth=" + "a" * 5000])
def test_a_missing_forged_or_repeated_token_is_refused(srv, make):
    assert get(srv, make(srv))[0] == 403


def test_a_token_is_signed_for_the_owner_and_expires():
    from ctb import family_print as fp
    now = time.time()
    good = fp.token("secret", OWNER, int(now) + 300)
    assert fp.valid_token(good, "secret", OWNER)
    assert not fp.valid_token(good, "other", OWNER) and not fp.valid_token(good, "secret", OWNER + 1)
    assert not fp.valid_token(good, "secret", OWNER, now=now + 400)
    assert not fp.valid_token(fp.token("secret", OWNER, int(now) + 99999), "secret", OWNER)
    assert not fp.valid_token(good.replace("fam1", "app1"), "secret", OWNER) and not fp.valid_token(None, "secret", OWNER)


def test_a_card_media_or_app_token_does_not_open_the_print_page(srv):
    assert get(srv, "/family/print?auth=" + panel.card_media_token(srv.bot_token, srv.owner_id, "a" * 32, int(time.time()) + 300))[0] == 403
    assert get(srv, "/family/print?auth=" + panel.app_token(srv.bot_token, srv.owner_id, "sample"))[0] == 403
