"""The morning briefing: content assembly, the wake time, the quiet days, delivery to the owner only, the private
radio and the child-suitability check. Everything is invented; nothing is sent anywhere."""
import json
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from conftest import GOOD_TOKEN, FakeBot
from ctb import addons, briefing, i18n, radio, schedule
from ctb import inbox as I
from ctb.config import Config, ProjectConfig, from_dict, ConfigError

OWNER = 7
GROUP = -1001234567890
TZ = ZoneInfo("Asia/Jerusalem")


def at(day, hour=7, minute=30):
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ).timestamp()


SUNDAY, SATURDAY = at(15), at(14)  # 2026-11-15 is a Sunday


def item(ident, text, status="open", wait="", updated=None, **extra):
    updated = SUNDAY - 600 if updated is None else updated
    return dict({"id": ident, "text": text, "status": status, "wait": wait, "notes": [], "created": updated - 1000,
                 "updated": updated}, **extra)


def make_cfg(tmp_path, **kw):
    base = dict(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, chat_id=GROUP, target_language="en", ui_language="en",
                projects=[ProjectConfig(name="Studio", chat_id=GROUP)], work_map={"ledger_dir": str(tmp_path / "ledger")})
    base.update(kw)
    return Config(**base)


def ledger(tmp_path, items, project="studio"):
    folder = tmp_path / "ledger"
    folder.mkdir(exist_ok=True)
    (folder / f"{project}.json").write_text(json.dumps({"next": 99, "items": items}))
    return folder


@pytest.fixture
def spoken(monkeypatch):
    """The radio is on; everything it is asked to say is collected."""
    said = []
    monkeypatch.setattr(radio, "job_pid", lambda: 4242)
    monkeypatch.setattr(radio, "say", lambda text: said.append(text) or "radio_said")
    return said


@pytest.fixture
def no_radio(monkeypatch):
    said = []
    monkeypatch.setattr(radio, "job_pid", lambda: None)
    monkeypatch.setattr(radio, "say", lambda text: said.append(text) or "radio_said")
    return said


# --- content ------------------------------------------------------------------------------------

def test_content_has_schedule_overnight_and_the_top_five_waiting_with_the_rest_counted(tmp_path):
    waiting = [item(i, f"Decision number {i}", wait="owner", updated=SUNDAY - 1000 * i, impact=f"Effect {i}") for i in range(1, 8)]
    done = [item(20, "Landed overnight", status="done", updated=SUNDAY - 5 * 3600),
            item(21, "Landed last week", status="done", updated=SUNDAY - 9 * 86400)]
    folder = ledger(tmp_path, waiting + done)
    data = briefing.build(make_cfg(tmp_path), [], SUNDAY, folder=folder,
                          schedule_rows=[{"time": "10:00", "title": "Team sync"}, {"time": "", "title": "Buy milk"}], chores=[])
    assert [r["what"] for r in data["waiting"]] == [f"Decision number {i}" for i in (7, 6, 5, 4, 3)]  # longest-waiting first
    assert data["waiting_more"] == 2 and data["waiting"][0]["impact"] == "Effect 7" and data["waiting"][0]["project"] == "studio"
    assert [r["what"] for r in data["overnight"]] == ["Landed overnight"]
    assert [r["title"] for r in data["schedule"]] == ["Team sync", "Buy milk"] and data["chores"] == []
    assert data["date"] == "2026-11-15" and data["lang"] == "en"
    text = briefing.render(make_cfg(tmp_path), data)
    for part in ("Team sync", "10:00", "Landed overnight", "Decision number 7", "Effect 7"):
        assert part in text
    assert "Decision number 2" not in text and i18n.t("br_more", lang="en", n=2) in text
    assert "Chores" not in text


def test_the_schedule_comes_from_the_owners_own_items(tmp_path):
    cfg = make_cfg(tmp_path)
    schedule.add(cfg, "call the dentist")
    assert "call the dentist" in [r["title"] for r in briefing._schedule_rows(cfg, SUNDAY)]


def test_chores_are_todays_open_chores_and_routine_from_the_family_board(tmp_path):
    from ctb import family_board
    cfg = make_cfg(tmp_path)
    view = family_board.view(cfg, now=datetime.fromtimestamp(SUNDAY, TZ))
    expected = [r["text"] for r in view["days"][view["day"]]["chores"] + view["daily"] if not r["done"]]
    got = briefing._chores(cfg, SUNDAY)
    assert got == [briefing.work_lines._text(t) for t in expected] and got  # the starting lists are not empty
    assert briefing._chores(cfg, SATURDAY) == []  # the board offers nothing on Shabbat


def test_a_failing_family_board_gives_no_chores_not_an_error(tmp_path, monkeypatch):
    from ctb import family_board
    monkeypatch.setattr(family_board, "view", lambda *a, **k: 1 / 0)
    assert briefing._chores(make_cfg(tmp_path), SUNDAY) == []


def test_a_quiet_morning_says_so_and_hebrew_headings_stay_hebrew(tmp_path):
    cfg = make_cfg(tmp_path, ui_language="he")
    data = briefing.build(cfg, [], SUNDAY, folder=tmp_path / "none", schedule_rows=[], chores=[])
    assert briefing.empty(data)
    text = briefing.render(cfg, data)
    assert i18n.t("br_quiet", lang="he") in text and i18n.t("br_none_today", lang="he") in text
    assert not re.search("[A-Za-z]", re.sub(r"<[^>]+>", "", text))


def test_the_spoken_line_is_short_and_counts_what_matters(tmp_path):
    folder = ledger(tmp_path, [item(1, "Pick a caption style", wait="owner"), item(2, "Landed", status="done", updated=SUNDAY - 3600)])
    cfg = make_cfg(tmp_path)
    data = briefing.build(cfg, [], SUNDAY, folder=folder, schedule_rows=[{"time": "09:00", "title": "Gym"}], chores=[])
    line = briefing.spoken(cfg, data)
    assert line.startswith("Good morning") and len(line) <= radio.MAX_SAY
    assert "Pick a caption style" in line and "<" not in line


# --- child suitability --------------------------------------------------------------------------

def test_an_unsuitable_item_is_shown_by_its_project_name_only(tmp_path):
    folder = ledger(tmp_path, [item(1, "Plan the murder mystery episode", wait="owner", impact="A story about a massacre"),
                               item(2, "Pick a caption style", wait="owner")])
    data = briefing.build(make_cfg(tmp_path), [], SUNDAY, folder=folder, schedule_rows=[{"time": "", "title": "Cooking show"}], chores=[])
    text = briefing.render(make_cfg(tmp_path), data) + briefing.spoken(make_cfg(tmp_path), data)
    assert "murder" not in text.casefold() and "massacre" not in text.casefold()
    assert "Pick a caption style" in text and "Cooking show" in text


# --- the wake time and quiet days --------------------------------------------------------------

def test_the_wake_time_is_configurable_and_validated(tmp_path):
    assert briefing.wake_time(make_cfg(tmp_path)) == (7, 0)
    assert briefing.wake_time(make_cfg(tmp_path, briefing={"time": "06:45"})) == (6, 45)
    assert briefing.wake_time(make_cfg(tmp_path, briefing={"time": "nonsense"})) == (7, 0)
    for bad in ({"time": "7am"}, {"time": "25:00"}, {"time": 7}, []):
        assert not briefing.valid_section(bad)
        if isinstance(bad, dict):
            with pytest.raises(ConfigError):
                from_dict({"briefing": bad})
    assert briefing.valid_section({}) and briefing.valid_section({"time": "07:00"})


def test_nothing_is_made_before_the_wake_time_and_it_comes_once_a_day(tmp_path, no_radio):
    cfg, bot = make_cfg(tmp_path), FakeBot()
    assert briefing.tick(cfg, bot, at(15, 6, 59), entries=[], folder=tmp_path / "none") is False and bot.calls == []
    assert briefing.tick(cfg, bot, at(15, 7, 0), entries=[], folder=tmp_path / "none") is True
    assert briefing.tick(cfg, bot, at(15, 7, 1), entries=[], folder=tmp_path / "none") is False
    assert briefing.tick(cfg, bot, at(15, 12, 0), entries=[], folder=tmp_path / "none") is False
    assert len(bot.methods("sendMessage")) == 1
    assert briefing.tick(cfg, bot, at(16, 7, 5), entries=[], folder=tmp_path / "none") is True  # the next day
    assert len(bot.methods("sendMessage")) == 2


def test_saturday_and_configured_holiday_windows_are_skipped(tmp_path, no_radio):
    cfg, bot = make_cfg(tmp_path), FakeBot()
    assert briefing.rest_day(cfg, SATURDAY) and not briefing.rest_day(cfg, SUNDAY) and not briefing.rest_day(cfg, at(13, 7, 30))
    assert briefing.tick(cfg, bot, SATURDAY, entries=[]) is False and bot.calls == [] and briefing.card(cfg, SATURDAY) is None
    holiday = make_cfg(tmp_path, voice_digest_quiet_windows=[{"start": "2026-11-16T00:00", "end": "2026-11-17T00:00"}])
    assert briefing.rest_day(holiday, at(16, 8, 0)) and not briefing.rest_day(holiday, at(17, 8, 0))
    assert briefing.tick(holiday, bot, at(16, 7, 30), entries=[]) is False
    broken = make_cfg(tmp_path, voice_digest_quiet_windows=[{"start": "nope", "end": "x"}])
    assert briefing.rest_day(broken, SUNDAY)  # an unreadable window list never lets it through


def test_a_briefing_hours_late_is_skipped_for_the_day(tmp_path, no_radio):
    cfg, bot = make_cfg(tmp_path), FakeBot()
    assert briefing.tick(cfg, bot, at(15, 14, 0), entries=[]) is False and bot.calls == []
    assert briefing.tick(cfg, bot, at(15, 14, 5), entries=[]) is False  # and it does not catch up later


def test_the_flag_switches_everything_off(tmp_path, spoken):
    cfg, bot = make_cfg(tmp_path), FakeBot()
    addons.set_on(cfg, "morning_briefing", False)
    assert briefing.tick(cfg, bot, SUNDAY, entries=[]) is False and bot.calls == [] and spoken == []
    replies = []
    briefing.telegram_command(cfg, bot, GROUP, None, replies.append, now=SUNDAY, entries=[])
    assert replies == [i18n.t("br_off", lang="en")] and briefing.card(cfg, SUNDAY) is None


# --- delivery -----------------------------------------------------------------------------------

def test_delivery_goes_to_the_home_chat_silently_and_puts_the_card_up_until_dismissed(tmp_path, no_radio):
    folder = ledger(tmp_path, [item(1, "Pick a caption style", wait="owner")])
    cfg, bot = make_cfg(tmp_path), FakeBot()
    assert briefing.tick(cfg, bot, SUNDAY, entries=[], folder=folder) is True
    sent = bot.methods("sendMessage")
    assert len(sent) == 1 and sent[0]["chat_id"] == GROUP and "message_thread_id" not in sent[0]
    assert sent[0]["disable_notification"] is True and "Pick a caption style" in sent[0]["text"]
    card = briefing.card(cfg, SUNDAY + 3600)
    assert card and card["waiting"][0]["what"] == "Pick a caption style"
    briefing.dismiss(cfg, SUNDAY + 3600)
    assert briefing.card(cfg, SUNDAY + 3700) is None
    assert briefing.card(cfg, SUNDAY + 86400) is None  # yesterday's is never shown today
    briefing.tick(cfg, bot, at(16, 7, 10), entries=[], folder=folder)
    assert briefing.card(cfg, at(16, 8, 0)) is not None  # a new day, a new card


def test_it_is_only_ever_sent_to_the_owners_chats(tmp_path, no_radio):
    cfg, bot = make_cfg(tmp_path), FakeBot()
    briefing.tick(cfg, bot, SUNDAY, entries=[], folder=tmp_path / "none")
    assert {p["chat_id"] for p in bot.methods("sendMessage")} == {GROUP}
    private = make_cfg(tmp_path, chat_id=0)
    bot2 = FakeBot()
    briefing.deliver(private, bot2, briefing.build(private, [], SUNDAY, folder=tmp_path / "none", schedule_rows=[], chores=[]), SUNDAY)
    assert {p["chat_id"] for p in bot2.methods("sendMessage")} == {OWNER}


# --- the radio ----------------------------------------------------------------------------------

def test_it_is_spoken_on_the_private_home_stream_only_while_the_radio_is_on(tmp_path, spoken):
    folder = ledger(tmp_path, [item(1, "Pick a caption style", wait="owner")])
    cfg, bot = make_cfg(tmp_path), FakeBot()
    briefing.tick(cfg, bot, SUNDAY, entries=[], folder=folder)
    assert len(spoken) == 1 and "Pick a caption style" in spoken[0]


def test_nothing_is_spoken_when_the_radio_is_off(tmp_path, no_radio):
    cfg, bot = make_cfg(tmp_path), FakeBot()
    briefing.tick(cfg, bot, SUNDAY, entries=[], folder=tmp_path / "none")
    assert no_radio == [] and len(bot.methods("sendMessage")) == 1


def test_the_speech_path_is_the_home_radios_spool_never_the_staff_edition():
    source = Path(briefing.__file__).read_text()
    assert "radio.say(" in source
    assert "radio_youtube" not in source and "radio_staff" not in source and "radio_board" not in source


def test_spoken_text_is_scrubbed_of_emails_and_identifiers(tmp_path, spoken):
    folder = ledger(tmp_path, [item(1, "Ask jane.doe@example.com about account 9876543210", wait="owner")])
    cfg = make_cfg(tmp_path)
    briefing.deliver(cfg, FakeBot(), briefing.build(cfg, [], SUNDAY, folder=folder, schedule_rows=[], chores=[]), SUNDAY)
    assert spoken and "example.com" not in spoken[0] and "9876543210" not in spoken[0] and "@" not in spoken[0]


def test_a_radio_failure_never_breaks_the_delivery(tmp_path, monkeypatch):
    monkeypatch.setattr(radio, "job_pid", lambda: 1)
    monkeypatch.setattr(radio, "say", lambda text: (_ for _ in ()).throw(radio.RadioError("radio_is_off")))
    cfg, bot = make_cfg(tmp_path), FakeBot()
    assert briefing.tick(cfg, bot, SUNDAY, entries=[], folder=tmp_path / "none") is True
    assert len(bot.methods("sendMessage")) == 1


# --- /briefing ----------------------------------------------------------------------------------

def test_briefing_command_sends_it_now_in_the_topic_it_was_typed_in(tmp_path, spoken):
    folder = ledger(tmp_path, [item(1, "Pick a caption style", wait="owner")])
    cfg, bot, replies = make_cfg(tmp_path), FakeBot(), []
    assert briefing.telegram_command(cfg, bot, GROUP, 55, replies.append, now=SUNDAY + 3600 * 5, entries=[], folder=folder) == "briefing"
    sent = bot.methods("sendMessage")
    assert len(sent) == 1 and sent[0]["message_thread_id"] == 55 and "Pick a caption style" in sent[0]["text"] and replies == []
    assert len(spoken) == 1 and briefing.card(cfg, SUNDAY + 3600 * 5) is not None


def test_briefing_command_on_a_quiet_day_only_says_it_rests(tmp_path, spoken):
    cfg, bot, replies = make_cfg(tmp_path, ui_language="he"), FakeBot(), []
    briefing.telegram_command(cfg, bot, GROUP, None, replies.append, now=SATURDAY, entries=[])
    assert replies == [i18n.t("br_shabbat", lang="he")] and bot.calls == [] and spoken == []


def say(rig, text, uid=OWNER, chat=OWNER, chat_type="private"):
    m = {"message_id": 9, "date": 2_000_000_000, "text": text, "chat": {"id": chat, "type": chat_type}, "from": {"id": uid}}
    return I.handle({"update_id": 3, "message": m}, rig.cfg, rig.bot, rig.topics, rig.box, None, 1000, rig.team_topics)


def test_briefing_command_through_the_inbox_is_owner_only(tmp_path, no_radio, monkeypatch):
    from test_team import Rig
    rig = Rig(tmp_path)
    monkeypatch.setattr("ctb.sessions_menu.collect", lambda *a, **k: [])
    monkeypatch.setattr(briefing, "rest_day", lambda cfg, now: False)
    assert say(rig, "/briefing") == "briefing"
    sent = rig.bot.methods("sendMessage")
    assert sent and sent[-1]["parse_mode"] == "HTML" and i18n.t("br_title", lang=rig.cfg.ui_language) in sent[-1]["text"]
    n = len(rig.bot.methods("sendMessage"))
    assert say(rig, "/briefing", uid=99) != "briefing" and say(rig, "/briefing", chat=-777, chat_type="supergroup") != "briefing"
    assert len(rig.bot.methods("sendMessage")) == n


# --- the Cockpit route and state ------------------------------------------------------------------

def _server(cfg):
    import threading
    from ctb import panel
    server = panel.PanelServer(("127.0.0.1", 0), cfg)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _post(server, body, path="/api/briefing-dismiss", init="ok"):
    import http.client
    from ctb import panel
    from test_panel import signed
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=20)
    headers = {panel.INIT_HEADER: signed()} if init == "ok" else {}
    conn.request("POST", path, body=json.dumps(body).encode(), headers=headers)
    r = conn.getresponse()
    out = json.loads(r.read())
    conn.close()
    return r.status, out


def test_the_route_is_declared_small_and_dismisses_only_for_the_owner(tmp_path, no_radio):
    from ctb import panel
    assert panel.ROUTES["/api/briefing-dismiss"] <= 1024 and panel.ACTIONS["/api/briefing-dismiss"] == "_briefing_dismiss"
    import time as _time
    cfg = make_cfg(tmp_path)
    briefing.deliver(cfg, FakeBot(), briefing.build(cfg, [], _time.time(), folder=tmp_path / "none", schedule_rows=[], chores=[]), _time.time())
    server = _server(cfg)
    try:
        assert _post(server, {}, init=None)[0] in (401, 403) and briefing.card(cfg, _time.time()) is not None
        assert _post(server, {"x": 1})[0] == 400 and briefing.card(cfg, _time.time()) is not None
        assert _post(server, {}) == (200, {"ok": True}) and briefing.card(cfg, _time.time()) is None
    finally:
        server.shutdown()
        server.server_close()


def test_the_state_carries_the_card_only_while_a_briefing_stands(tmp_path, no_radio):
    import time as _time
    from ctb import cockpit_v2, panel
    cockpit_v2.set_mode("on")
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, work_map={"ledger_dir": str(tmp_path / "none")})
    assert "morning" not in panel.build_state(cfg, light=True)
    now = _time.time()
    briefing.deliver(cfg, FakeBot(), briefing.build(cfg, [], now, folder=tmp_path / "none", schedule_rows=[{"time": "", "title": "Gym"}], chores=[]), now)
    assert panel.build_state(cfg, light=True)["morning"]["schedule"] == [{"time": "", "title": "Gym"}]
    briefing.dismiss(cfg, now)
    assert "morning" not in panel.build_state(cfg, light=True)
    addons.set_on(cfg, "morning_briefing", False)
    assert "morning" not in panel.build_state(cfg, light=True)


def test_briefing_command_only_in_the_home_chat(tmp_path, no_radio, monkeypatch):
    """It holds his schedule, chores and every project's waiting items: a project group's topic never gets it."""
    from test_team import Rig
    rig = Rig(tmp_path)
    monkeypatch.setattr("ctb.sessions_menu.collect", lambda *a, **k: [])
    monkeypatch.setattr(briefing, "rest_day", lambda cfg, now: False)
    rig.cfg.projects = [ProjectConfig(name="Studio", chat_id=-1005555555555)]
    n = len(rig.bot.methods("sendMessage"))
    assert say(rig, "/briefing", chat=-1005555555555, chat_type="supergroup") != "briefing"
    assert len(rig.bot.methods("sendMessage")) == n and briefing.card(rig.cfg, 2_000_000_000) is None
    assert say(rig, "/briefing") == "briefing"
