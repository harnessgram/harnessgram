"""The per-project work map: ledger reading (impact notes, corrupt files), ordering, the pinned message and its edit
throttle, the owner's /what, and the add-on flag. Everything is invented; no real ledger or config is read."""
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from conftest import GOOD_TOKEN, FakeBot, signed_in
from ctb import addons, board, cockpit_v2, flood, i18n, work_lines, work_map
from ctb import inbox as I
from ctb.config import Config, ProjectConfig, state_dir, write_private
from ctb.sessions_menu import Entry
from ctb.telegram import TelegramError

OWNER = 7
GROUP = -1001234567890
TZ = ZoneInfo("Asia/Jerusalem")
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=TZ).timestamp()
MIDNIGHT = datetime(2026, 11, 15, 0, 0, tzinfo=TZ).timestamp()


@pytest.fixture(autouse=True)
def roomy_budget(monkeypatch):
    monkeypatch.setattr(flood, "OPTIONAL_PER_MIN", 1000)


def make_cfg(tmp_path, **kw):
    base = dict(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, chat_id=GROUP, target_language="en", ui_language="en",
                projects=[ProjectConfig(name="Studio", chat_id=GROUP)],
                work_map={"ledger_dir": str(tmp_path / "ledger")})
    base.update(kw)
    return Config(**base)


def item(ident, text, status="open", wait="", updated=NOW - 600, notes=(), **extra):
    return dict({"id": ident, "text": text, "status": status, "wait": wait, "notes": list(notes),
                 "created": updated - 1000, "updated": updated}, **extra)


def ledger(tmp_path, items, project="studio"):
    folder = tmp_path / "ledger"
    folder.mkdir(exist_ok=True)
    (folder / f"{project}.json").write_text(json.dumps({"next": 9, "items": items}))
    return folder


def working(thread=11, name="Studio: the filters board", session="sess-aaaaaaaa", age=5):
    return Entry(session=session, name=name, thread=thread, age=age, state="working", closed=False, chat=GROUP)


# --- the ledger ---------------------------------------------------------------------------------

def test_impact_comes_from_the_field_then_from_an_impact_note_and_is_not_the_latest_note():
    rows = work_lines.rows_of("studio", {"items": [
        item(1, "Field", impact="Faster checkout", notes=["impact: ignored", "plain note"]),
        item(2, "Note", notes=["started", "impact: Fewer wrong orders", "later plain note"]),
        item(3, "Only impact", notes=["Impact:   The banner is clearer"]),
        item(4, "None", notes=["just a note"])]})
    assert [r["impact"] for r in rows] == ["Faster checkout", "Fewer wrong orders", "The banner is clearer", ""]
    assert [r["note"] for r in rows] == ["plain note", "later plain note", "", "just a note"]


def test_a_corrupt_missing_or_odd_ledger_is_an_empty_map(tmp_path):
    assert work_map.ledger_items(tmp_path / "nothing") == {}
    folder = tmp_path / "ledger"
    folder.mkdir()
    (folder / "broken.json").write_text("{not json")
    (folder / "odd.json").write_text(json.dumps({"items": "no"}))
    (folder / "list.json").write_text("[1, 2]")
    (folder / "mixed.json").write_text(json.dumps({"items": [1, None, {"text": "x"}, item(1, "Real")]}))
    found = work_map.ledger_items(folder)
    assert [i["text"] for i in found["mixed"]["items"]] == ["Real"]
    assert all(not found[k]["items"] for k in ("odd", "list") if k in found) and "broken" not in found
    assert work_map.build_all(make_cfg(tmp_path), [], NOW, folder=tmp_path / "nothing") == {}


def test_the_ledger_folder_is_configurable_and_falls_back_to_the_default(tmp_path):
    assert work_map.ledger_dir(make_cfg(tmp_path)) == tmp_path / "ledger"
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER)
    assert str(work_map.ledger_dir(cfg)).endswith(".local/state/work-ledger")
    assert work_map.ledger_dir(Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, brief={"ledger_dir": "/x/y"})).as_posix() == "/x/y"


# --- the lines ----------------------------------------------------------------------------------

def test_lines_are_ordered_waiting_first_then_working_then_done_today(tmp_path):
    folder = ledger(tmp_path, [
        item(1, "Finished today", status="done", updated=MIDNIGHT + 3600),
        item(2, "Finished yesterday", status="done", updated=MIDNIGHT - 3600),
        item(3, "Newer work", updated=NOW - 60),
        item(4, "Older work", updated=NOW - 900),
        item(5, "Needs your pick, recent", wait="owner", updated=NOW - 100),
        item(6, "Needs your pick, long ago", wait="owner", updated=NOW - 5000)])
    m = work_map.build_all(make_cfg(tmp_path), [], NOW, folder=folder)["studio"]
    assert [(l["status"], l["what"]) for l in m["lines"]] == [
        ("waiting", "Needs your pick, long ago"), ("waiting", "Needs your pick, recent"),
        ("working", "Newer work"), ("working", "Older work"), ("done", "Finished today")]
    assert m["counts"] == {"waiting": 2, "working": 2, "done": 1}


def test_every_line_says_what_it_changes_for_the_owner_in_plain_words(tmp_path):
    folder = ledger(tmp_path, [
        item(1, "Fix the crash on the order page"), item(2, "Add a new catalog view"), item(3, "Review the release notes"),
        item(4, "Rename things", wait="owner"), item(5, "Tidy", impact="You stop getting duplicate emails"),
        item(6, "Reindex", status="done", updated=NOW - 60)])
    lines = {l["what"]: l for l in work_map.build_all(make_cfg(tmp_path), [], NOW, folder=folder)["studio"]["lines"]}
    assert lines["Fix the crash on the order page"]["impact"] == i18n.t("wm_i_fix", lang="en")
    assert lines["Add a new catalog view"]["impact"] == i18n.t("wm_i_new", lang="en")
    assert lines["Review the release notes"]["impact"] == i18n.t("wm_i_check", lang="en")
    assert lines["Rename things"]["impact"] == i18n.t("wm_i_wait", lang="en")
    assert lines["Tidy"]["impact"] == "You stop getting duplicate emails"
    assert lines["Reindex"]["impact"] == i18n.t("wm_i_done", lang="en")
    he = work_map.build_all(make_cfg(tmp_path, ui_language="he"), [], NOW, folder=folder)["studio"]["lines"]
    assert all(l["impact"] for l in he) and any(re.search("[א-ת]", l["impact"]) for l in he)


def test_running_agents_are_lines_in_progress_named_by_their_progress_card(tmp_path):
    cfg = make_cfg(tmp_path)
    ended = Entry("sess-bbbbbbbb", "Studio: old thing", 12, 9999, "ended", True, GROUP)
    idle = Entry("sess-cccccccc", "Studio: resting", 13, 50, "idle", False, GROUP)
    cards = [{"kind": "progress", "state": "open", "session": "sess-aaaaaaaa", "title": "Building the filters board", "updated": NOW - 30}]
    maps = work_map.build_all(cfg, [working(), ended, idle], NOW, folder=tmp_path / "none", card_rows=cards)
    assert [(l["status"], l["what"], l["source"]) for l in maps["studio"]["lines"]] == [
        ("working", "Building the filters board", "agent")]
    plain = work_map.build_all(cfg, [working()], NOW, folder=tmp_path / "none", card_rows=[])
    assert plain["studio"]["lines"][0]["what"] == "the filters board"


# --- the message --------------------------------------------------------------------------------

def test_the_message_caps_lines_and_keeps_hebrew_headings_free_of_latin(tmp_path):
    folder = ledger(tmp_path, [item(i, f"Item number {i}", updated=NOW - i) for i in range(1, 16)])
    cfg = make_cfg(tmp_path, ui_language="he")
    text = work_map.render(cfg, work_map.build_all(cfg, [], NOW, folder=folder)["studio"])
    assert text.count("• ") == work_map.CHAT_LINES
    assert i18n.t("wm_more", lang="he", n=3) in text
    headings = re.findall(r"<b>(.*?)</b>", text)
    assert all(not re.search("[A-Za-z]", h.replace("Studio", "")) for h in headings[:1] + headings[2:])
    # every English item sits in a bidi isolate on its own line
    assert all("⁨" in l for l in text.splitlines() if "Item number" in l and l.startswith("‏•"))
    assert len(text) < 4000


def test_an_empty_map_says_so(tmp_path):
    cfg = make_cfg(tmp_path)
    assert i18n.t("wm_empty", lang="en") in work_map.render(cfg, {"key": "studio", "name": "Studio", "lines": []})


def test_text_is_html_escaped(tmp_path):
    folder = ledger(tmp_path, [item(1, "Handle <b>bold</b> & more")])
    text = work_map.render(make_cfg(tmp_path), work_map.build_all(make_cfg(tmp_path), [], NOW, folder=folder)["studio"])
    assert "&lt;b&gt;bold&lt;/b&gt; &amp; more" in text


# --- the pinned message -------------------------------------------------------------------------

def pins(bot):
    return [p for m, p in bot.calls if m == "pinChatMessage"]


def test_the_map_is_created_unpinned_in_the_lead_topic_and_then_only_edited(tmp_path):
    cfg, bot = make_cfg(tmp_path), FakeBot()
    folder = ledger(tmp_path, [item(1, "Needs you", wait="owner")])
    entries = [working(thread=11, session="sess-aaaaaaaa", age=100), working(thread=12, name="Studio: other", session="sess-bbbbbbbb", age=5)]
    maps = lambda: work_map.build_all(cfg, entries, NOW, folder=folder)
    assert work_map.sync_pins(cfg, bot, entries, NOW, maps()) == 1
    sent = bot.methods("sendMessage")
    assert len(sent) == 1 and sent[0]["message_thread_id"] == 12  # the most recently active session's topic
    assert sent[0]["chat_id"] == GROUP and sent[0]["parse_mode"] == "HTML" and sent[0]["disable_notification"]
    assert pins(bot) == [] and "Needs you" in sent[0]["text"]  # never pinned: the bar stays the Cockpit button
    # nothing changed: no call at all
    n = len(bot.calls)
    assert work_map.sync_pins(cfg, bot, entries, NOW + 500, maps()) == 0 and len(bot.calls) == n


def test_changes_edit_the_same_message_at_most_once_every_two_minutes(tmp_path):
    signed_in("main")
    cfg, bot = make_cfg(tmp_path), FakeBot()
    entries = [working()]
    folder = ledger(tmp_path, [item(1, "First")])
    work_map.sync_pins(cfg, bot, entries, NOW, work_map.build_all(cfg, entries, NOW, folder=folder))
    mid = bot.methods("sendMessage")[0]["message_id"]
    for step, (text, at, edits) in enumerate([("Second", NOW + 30, 0), ("Third", NOW + 119, 0), ("Fourth", NOW + 121, 1),
                                               ("Fifth", NOW + 200, 1), ("Sixth", NOW + 241, 2)]):
        folder = ledger(tmp_path, [item(1, text)])
        work_map.sync_pins(cfg, bot, entries, at, work_map.build_all(cfg, entries, at, folder=folder))
        assert len(bot.methods("editMessageText")) == edits, step
    assert len(bot.methods("sendMessage")) == 1  # never a new message for a change
    assert all(p["message_id"] == mid for p in bot.methods("editMessageText"))
    assert "Sixth" in bot.methods("editMessageText")[-1]["text"]


def test_a_deleted_message_is_replaced_by_exactly_one_new_message(tmp_path):
    cfg, bot = make_cfg(tmp_path), FakeBot()
    entries = [working()]
    folder = ledger(tmp_path, [item(1, "First")])
    work_map.sync_pins(cfg, bot, entries, NOW, work_map.build_all(cfg, entries, NOW, folder=folder))
    folder = ledger(tmp_path, [item(1, "Changed")])
    bot.fail["editMessageText"] = TelegramError("Bad Request: message to edit not found", 400)
    work_map.sync_pins(cfg, bot, entries, NOW + 300, work_map.build_all(cfg, entries, NOW + 300, folder=folder))
    assert len(bot.methods("sendMessage")) == 2 and pins(bot) == []


def test_a_failed_edit_is_retried_later_not_every_round(tmp_path):
    cfg, bot = make_cfg(tmp_path), FakeBot()
    entries = [working()]
    work_map.sync_pins(cfg, bot, entries, NOW, work_map.build_all(cfg, entries, NOW, folder=ledger(tmp_path, [item(1, "First")])))
    folder = ledger(tmp_path, [item(1, "Changed")])
    bot.fail["editMessageText"] = TelegramError("Too Many Requests", 429)
    maps = lambda at: work_map.build_all(cfg, entries, at, folder=folder)
    assert work_map.sync_pins(cfg, bot, entries, NOW + 300, maps(NOW + 300)) == 0
    calls = len(bot.methods("editMessageText"))
    assert work_map.sync_pins(cfg, bot, entries, NOW + 310, maps(NOW + 310)) == 0 and len(bot.methods("editMessageText")) == calls
    assert work_map.sync_pins(cfg, bot, entries, NOW + 400, maps(NOW + 400)) == 1


def test_no_message_without_a_live_session_a_project_group_or_the_flag(tmp_path):
    folder = ledger(tmp_path, [item(1, "Needs you", wait="owner")])
    cfg, bot = make_cfg(tmp_path), FakeBot()
    assert work_map.sync_pins(cfg, bot, [], NOW, work_map.build_all(cfg, [], NOW, folder=folder)) == 0  # no lead topic
    off = make_cfg(tmp_path)
    addons.set_on(off, "work_map", False)
    assert work_map.sync_pins(off, bot, [working()], NOW) == 0 and work_map.tick(off, bot, NOW, entries=[working()]) == 0
    assert bot.calls == []


def test_a_configured_lead_wins_over_the_most_recent_session(tmp_path):
    cfg = make_cfg(tmp_path, work_map={"leads": {"Studio": "sess-bbbbbbbb"}})
    entries = [working(thread=11, session="sess-aaaaaaaa", age=1), working(thread=12, name="Studio: lead", session="sess-bbbbbbbb", age=900)]
    assert work_map.lead_place(cfg, "studio", entries) == (GROUP, 12)
    assert work_map.lead_place(make_cfg(tmp_path), "studio", entries) == (GROUP, 11)


# --- /what --------------------------------------------------------------------------------------

def say(rig, text, uid=OWNER, chat=OWNER, chat_type="private"):
    m = {"message_id": 9, "date": 2_000_000_000, "text": text, "chat": {"id": chat, "type": chat_type}, "from": {"id": uid}}
    return I.handle({"update_id": 3, "message": m}, rig.cfg, rig.bot, rig.topics, rig.box, None, 1000, rig.team_topics)


def test_what_answers_with_the_projects_map_in_its_topic(tmp_path, monkeypatch):
    cfg, bot = make_cfg(tmp_path), FakeBot()
    folder = ledger(tmp_path, [item(1, "Pick a caption style", wait="owner", impact="The video can ship")])
    replies = []
    assert work_map.telegram_command(cfg, GROUP, 11, replies.append, now=NOW, entries=[working()], folder=folder) == "what"
    text = replies[0]
    assert "Pick a caption style" in text and "The video can ship" in text and "the filters board" in text
    assert text.index("Pick a caption style") < text.index("the filters board")
    other = []
    work_map.telegram_command(cfg, -1009999999999, 5, other.append, now=NOW, entries=[working()], folder=folder)
    assert other == [i18n.t("wm_no_project", lang="en")]
    off = []
    addons.set_on(cfg, "work_map", False)
    work_map.telegram_command(cfg, GROUP, 11, off.append, now=NOW, entries=[])
    assert off == [i18n.t("wm_off", lang="en")]


def test_what_command_is_owner_only_and_never_reaches_a_session(tmp_path, monkeypatch):
    from test_team import Rig
    rig = Rig(tmp_path)
    monkeypatch.setattr(work_map, "ledger_dir", lambda cfg: ledger(tmp_path, [item(1, "Pick a caption style", wait="owner")]))
    rig.cfg.projects = [ProjectConfig(name="Studio", chat_id=OWNER)]  # the owner's own chat stands for the project here
    monkeypatch.setattr("ctb.sessions_menu.collect", lambda *a, **k: [])
    assert say(rig, "/what") == "what"
    sent = rig.bot.methods("sendMessage")
    assert sent and sent[-1]["parse_mode"] == "HTML" and "Pick a caption style" in sent[-1]["text"]
    assert say(rig, "/what@somebot") == "what"
    n = len(rig.bot.methods("sendMessage"))
    assert say(rig, "/what", uid=99) != "what" and say(rig, "/what", chat=-777, chat_type="supergroup") != "what"
    assert len(rig.bot.methods("sendMessage")) == n


# --- the Cockpit --------------------------------------------------------------------------------

def test_the_project_page_data_carries_the_map_and_the_flag_removes_it(tmp_path):
    folder = ledger(tmp_path, [item(1, "Pick a caption style", wait="owner")])
    state = {"sessions": [{"id": "sess-aaa", "name": "Studio: the filters board", "age": 5, "state": "working"}],
             "requests": [], "decisions": [], "cards": [], "topic_cards": [], "reminders": []}
    cfg = make_cfg(tmp_path)
    out = cockpit_v2.presentation(cfg, dict(state), [working(session="sess-aaaaaaaa")], NOW)
    studio = next(p for p in out["projects"] if p["key"] == "studio")
    assert [l["status"] for l in studio["work_map"]] == ["waiting", "working"] and "work_map_off" not in out
    addons.set_on(cfg, "work_map", False)
    off = cockpit_v2.presentation(cfg, dict(state), [working(session="sess-aaaaaaaa")], NOW)
    assert off["work_map_off"] is True and all("work_map" not in p for p in off["projects"])


# --- the heartbeat ------------------------------------------------------------------------------

def test_the_heartbeat_keeps_the_pinned_map_current_without_hammering(tmp_path, monkeypatch):
    from ctb import heartbeat
    heartbeat._owner_views["at"] = 0.0
    ledger(tmp_path, [item(1, "Needs you", wait="owner")])
    cfg, bot = make_cfg(tmp_path), FakeBot()
    monkeypatch.setattr("ctb.sessions_menu.collect", lambda *a, **k: [working()])
    heartbeat.run_once(cfg, bot=bot, now=NOW)
    assert len(bot.methods("sendMessage")) >= 1 and any("Needs you" in p["text"] for p in bot.methods("sendMessage"))
    calls = len(bot.calls)
    heartbeat.run_once(cfg, bot=bot, now=NOW + 30)  # inside the round gap: the owner views are not even looked at
    assert not [p for p in bot.calls[calls:] if p[0] in ("sendMessage", "editMessageText", "pinChatMessage")]


def test_the_switch_is_a_registered_add_on_shown_in_the_add_ons_screen(tmp_path):
    cfg = make_cfg(tmp_path)
    keys = [i["key"] for g in addons.view(cfg)["groups"] for i in g["items"]]
    assert "work_map" in keys and "morning_briefing" in keys
    assert addons.enabled(cfg, "work_map") and addons.enabled(cfg, "morning_briefing")
    addons.set_on(cfg, "work_map", False)
    assert not work_map.enabled(cfg) and not addons.enabled(cfg, "work_map") and addons.enabled(cfg, "morning_briefing")


# --- a project group whose ledger has its own key (15.11: the pinned map showed one line of 60) -----


def _joined(tmp_path):
    return make_cfg(tmp_path, projects=[ProjectConfig(name="גשר", chat_id=GROUP)],
                    radio={"project_names": {"harnessgram": "גשר"}})


def test_the_ledger_and_the_group_sessions_are_one_map_under_the_ledger_key(tmp_path):
    signed_in("main")
    cfg = _joined(tmp_path)
    folder = ledger(tmp_path, [item(1, "Needs you", wait="owner"), item(2, "Build 11")], project="harnessgram")
    entries = [working(name="גשר: the restart fix")]
    maps = work_map.build_all(cfg, entries, NOW, folder=folder)
    assert list(maps) == ["harnessgram"] and maps["harnessgram"]["name"] == "גשר"
    assert maps["harnessgram"]["counts"] == {"waiting": 1, "working": 2, "done": 0}
    assert work_map.lead_place(cfg, "harnessgram", entries) == (GROUP, 11)
    assert work_map.map_key(cfg, "גשר") == "harnessgram" and work_map.map_key(cfg, "harnessgram") == "harnessgram"
    assert work_map.map_key(cfg, "other") == "other"
    explicit = make_cfg(tmp_path, brief={"ledger_projects": {"Studio": "studio-ledger"}})
    assert work_map.map_key(explicit, "studio") == "studio-ledger"


def test_a_map_pinned_under_the_display_name_is_moved_and_edited_in_place(tmp_path):
    signed_in("main")
    cfg, bot = _joined(tmp_path), FakeBot()
    path = state_dir() / work_map.PINS_FILE
    write_private(path, json.dumps({"גשר": {"id": 777, "pinned": True, "pin_retry_at": 0.0, "digest": "x",
                                            "edited_at": NOW - 10, "retry_at": 0.0, "chat": GROUP, "thread": 11},
                                    "harnessgram": {"id": 0, "pinned": False}}))
    folder = ledger(tmp_path, [item(1, "Needs you", wait="owner")], project="harnessgram")
    entries = [working(name="גשר: the restart fix")]
    assert work_map.sync_pins(cfg, bot, entries, NOW, work_map.build_all(cfg, entries, NOW, folder=folder)) == 1
    assert bot.methods("sendMessage") == [] and pins(bot) == []  # no second message, no second pin
    [edit] = bot.methods("editMessageText")
    assert edit["message_id"] == 777 and "Needs you" in edit["text"]
    data = json.loads(path.read_text())
    assert "גשר" not in data and data["harnessgram"]["id"] == 777


def test_the_cockpit_project_page_gets_the_joined_work_map(tmp_path):
    cfg = _joined(tmp_path)
    folder = ledger(tmp_path, [item(1, "Needs you", wait="owner"), item(2, "Build 11")], project="harnessgram")
    assert folder.exists()
    state = {"sessions": [{"id": "sess-aaa", "name": "גשר: the restart fix", "age": 5, "state": "working"}],
             "requests": [], "decisions": [], "cards": [], "topic_cards": [], "reminders": []}
    out = cockpit_v2.presentation(cfg, state, [working(name="גשר: the restart fix")], NOW)
    page = next(p for p in out["projects"] if p["key"] == "גשר")  # the group is listed under the display name
    assert [l["status"] for l in page["work_map"]] == ["waiting", "working", "working"]
    assert page["work_map_counts"] == {"waiting": 1, "working": 2, "done": 0}


def test_a_display_name_shared_by_two_ledger_keys_joins_nothing(tmp_path):
    shared = make_cfg(tmp_path, radio={"project_names": {"a-ledger": "Shared", "b-ledger": "Shared"}})
    assert work_map.map_key(shared, "shared") == "shared"  # ambiguous: the key stays unchanged
    tangled = make_cfg(tmp_path, projects=[], radio={"project_names": {"a-ledger": "Shared", "b-ledger": "Shared",
                                                          "shared": "Other"}})
    assert work_map.map_key(tangled, "shared") == "shared"  # a real ledger key is never taken for an alias
    entries = [working(name="Shared: the restart fix")]
    maps = work_map.build_all(tangled, entries, NOW, folder=ledger(tmp_path, [item(1, "Build 11")], project="a-ledger"))
    assert sorted(maps) == ["a-ledger", "shared"] and maps["shared"]["counts"]["working"] == 1
    assert maps["a-ledger"]["counts"]["working"] == 1  # the session did not attach to a-ledger


def test_a_ledger_file_of_the_same_name_keeps_its_key_and_its_pin(tmp_path):
    cfg, bot = _joined(tmp_path), FakeBot()
    ledger(tmp_path, [item(1, "Own work")], project="גשר")  # the display name is also a real ledger file
    folder = ledger(tmp_path, [item(2, "Needs you", wait="owner")], project="harnessgram")
    assert work_map.map_key(cfg, "גשר", folder) == "גשר" and work_map.map_key(cfg, "גשר") == "גשר"
    path = state_dir() / work_map.PINS_FILE
    write_private(path, json.dumps({"גשר": {"id": 777, "pinned": True, "pin_retry_at": 0.0, "digest": "x",
                                            "edited_at": NOW - 10, "retry_at": 0.0, "chat": GROUP, "thread": 11}}))
    work_map.sync_pins(cfg, bot, [], NOW, {})
    data = json.loads(path.read_text())
    assert data["גשר"]["id"] == 777 and "harnessgram" not in data  # not migrated: it is a real ledger key


def test_a_pin_under_a_project_names_key_or_an_ambiguous_alias_is_not_moved(tmp_path):
    cfg = make_cfg(tmp_path, radio={"project_names": {"a-ledger": "Shared", "b-ledger": "Shared", "own": "Own"}})
    pin = {"id": 5, "pinned": True, "digest": "x", "edited_at": 0.0}
    data = {"own": dict(pin), "shared": dict(pin), "unrelated": dict(pin)}
    work_map._join_old_keys(cfg, data)
    assert sorted(data) == ["own", "shared", "unrelated"]


def test_a_lead_configured_under_the_former_display_name_is_still_found(tmp_path):
    entries = [working(session="sess-lead0001", thread=11, age=900, name="גשר: lead"),
               working(session="sess-newer001", thread=22, age=5, name="גשר: newer")]
    for conf in ({"work_map": {"ledger_dir": str(tmp_path / "ledger"), "leads": {"גשר": "sess-lead0001"}}},
                 {"idle_lines": {"leads": {"גשר": "sess-lead0001"}}}):
        cfg = make_cfg(tmp_path, projects=[ProjectConfig(name="גשר", chat_id=GROUP)],
                       radio={"project_names": {"harnessgram": "גשר"}}, **{**{"work_map": {}}, **conf})
        assert work_map.lead_place(cfg, "harnessgram", entries) == (GROUP, 11)  # not the newer session's topic


def _old_pin(**kw):
    return dict({"id": 777, "pinned": True, "pin_retry_at": 0.0, "digest": "x", "edited_at": NOW - 10,
                 "retry_at": 0.0, "chat": GROUP, "thread": 11}, **kw)


def test_a_map_pinned_by_an_earlier_version_is_unpinned_exactly_once(tmp_path):
    signed_in("main")
    cfg, bot = make_cfg(tmp_path), FakeBot()
    path = state_dir() / work_map.PINS_FILE
    write_private(path, json.dumps({"studio": _old_pin()}))
    folder = ledger(tmp_path, [item(1, "Needs you", wait="owner")])
    entries = [working()]
    for at in (NOW, NOW + 300, NOW + 600):
        work_map.sync_pins(cfg, bot, entries, at, work_map.build_all(cfg, entries, at, folder=folder))
    assert bot.methods("unpinChatMessage") == [{"chat_id": GROUP, "message_id": 777}]
    assert pins(bot) == [] and bot.methods("sendMessage") == []  # the same message is edited in place, never re-pinned
    assert json.loads(path.read_text())["studio"]["pinned"] is False


@pytest.mark.parametrize("description", ["Bad Request: message to unpin not found", "Bad Request: message not found", "Bad Request: message is not pinned"])
def test_a_message_already_gone_or_unpinned_counts_as_unpinned(tmp_path, description):
    cfg, bot = make_cfg(tmp_path), FakeBot()
    path = state_dir() / work_map.PINS_FILE
    write_private(path, json.dumps({"studio": _old_pin()}))
    bot.fail["unpinChatMessage"] = TelegramError(description, 400)
    entries = [working()]
    for at in (NOW, NOW + 300):
        work_map.sync_pins(cfg, bot, entries, at, work_map.build_all(cfg, entries, at, folder=ledger(tmp_path, [item(1, "x")])))
    assert len(bot.methods("unpinChatMessage")) == 1 and json.loads(path.read_text())["studio"]["pinned"] is False


def test_another_unpin_failure_is_retried_after_the_backoff(tmp_path):
    cfg, bot = make_cfg(tmp_path), FakeBot()
    path = state_dir() / work_map.PINS_FILE
    write_private(path, json.dumps({"studio": _old_pin()}))
    bot.fail["unpinChatMessage"] = TelegramError("Forbidden: not enough rights", 403)
    entries = [working()]
    maps = lambda at: work_map.build_all(cfg, entries, at, folder=ledger(tmp_path, [item(1, "x")]))
    work_map.sync_pins(cfg, bot, entries, NOW, maps(NOW))
    work_map.sync_pins(cfg, bot, entries, NOW + 60, maps(NOW + 60))
    assert len(bot.methods("unpinChatMessage")) == 1 and json.loads(path.read_text())["studio"]["pinned"] is True
    work_map.sync_pins(cfg, bot, entries, NOW + 60 + board.PIN_BACKOFF + 1, maps(NOW + 60 + board.PIN_BACKOFF + 1))
    assert len(bot.methods("unpinChatMessage")) == 2 and json.loads(path.read_text())["studio"]["pinned"] is False


def test_explicit_aliases_follow_the_same_real_key_and_ambiguity_rules(tmp_path):
    ok = make_cfg(tmp_path, brief={"ledger_projects": {"גשר": "harnessgram"}})
    assert work_map.map_key(ok, "גשר") == "harnessgram"
    # a real ledger key (a project_names key, or a ledger file) never moves
    named = make_cfg(tmp_path, brief={"ledger_projects": {"Studio": "studio-ledger"}},
                     radio={"project_names": {"studio": "Studio"}})
    assert work_map.map_key(named, "studio") == "studio"
    filed = make_cfg(tmp_path, brief={"ledger_projects": {"Studio": "studio-ledger"}})
    folder = ledger(tmp_path, [item(1, "x")], project="studio")
    assert folder.exists() and work_map.map_key(filed, "studio") == "studio"
    # two spellings of one alias that lead to different targets are ambiguous
    clash = make_cfg(tmp_path, brief={"ledger_projects": {"Gesher": "a-ledger", "GESHER": "b-ledger"}})
    assert work_map.map_key(clash, "gesher") == "gesher"
    same = make_cfg(tmp_path, brief={"ledger_projects": {"Gesher": "a-ledger", "GESHER": "A-Ledger"}})
    assert work_map.map_key(same, "gesher") == "a-ledger"  # same target twice is not a clash


def test_old_pins_follow_the_explicit_alias_rules_too(tmp_path):
    pin = {"id": 5, "pinned": True, "digest": "x", "edited_at": 0.0}
    cfg = make_cfg(tmp_path, brief={"ledger_projects": {"Gesher": "a-ledger", "GESHER": "b-ledger",
                                                        "Own": "own-ledger", "Moves": "moved"}},
                   radio={"project_names": {"own": "Own"}})
    data = {"gesher": dict(pin), "own": dict(pin), "moves": dict(pin)}
    work_map._join_old_keys(cfg, data)
    assert sorted(data) == ["gesher", "moved", "own"]  # only the unambiguous, non-real alias moved


def test_chat_not_found_is_not_an_unpin_and_is_retried(tmp_path):
    cfg, bot = make_cfg(tmp_path), FakeBot()
    path = state_dir() / work_map.PINS_FILE
    write_private(path, json.dumps({"studio": _old_pin()}))
    bot.fail["unpinChatMessage"] = TelegramError("Bad Request: chat not found", 400)
    entries = [working()]
    maps = lambda at: work_map.build_all(cfg, entries, at, folder=ledger(tmp_path, [item(1, "x")]))
    work_map.sync_pins(cfg, bot, entries, NOW, maps(NOW))
    work_map.sync_pins(cfg, bot, entries, NOW + 60, maps(NOW + 60))
    assert len(bot.methods("unpinChatMessage")) == 1 and json.loads(path.read_text())["studio"]["pinned"] is True
    later = NOW + 60 + board.PIN_BACKOFF + 1
    work_map.sync_pins(cfg, bot, entries, later, maps(later))
    assert len(bot.methods("unpinChatMessage")) == 2 and json.loads(path.read_text())["studio"]["pinned"] is False


def test_a_map_whose_old_pin_waits_for_its_unpin_is_never_replaced_first(tmp_path):
    """Review of #384: an edit failing with "chat not found" was read as a deleted message, so the old pinned id
    was cleared and that pin could never be unpinned. While the unpin is pending nothing is written."""
    cfg, bot = make_cfg(tmp_path), FakeBot()
    path = state_dir() / work_map.PINS_FILE
    write_private(path, json.dumps({"studio": _old_pin()}))
    bot.fail["unpinChatMessage"] = TelegramError("Bad Request: chat not found", 400)
    bot.fail["editMessageText"] = TelegramError("Bad Request: chat not found", 400)
    entries = [working()]
    folder = ledger(tmp_path, [item(1, "changed text")])
    work_map.sync_pins(cfg, bot, entries, NOW + 500, work_map.build_all(cfg, entries, NOW + 500, folder=folder))
    rec = json.loads(path.read_text())["studio"]
    assert rec["pinned"] is True and rec["id"] == _old_pin()["id"]
    assert bot.methods("editMessageText") == [] and bot.methods("sendMessage") == []
