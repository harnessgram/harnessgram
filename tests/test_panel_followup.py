"""The panel's follow-up: newest first, stale decisions folded away (and cleared on request), what each session
is doing now and said last, links to topics, and the bridge settings and radio words the owner can send."""
import json
import threading
import time

import pytest

from conftest import GOOD_TOKEN
from ctb import decisions, panel, panel_settings, panel_view, sessions_menu
from ctb.config import Config, state_dir, write_private
from test_panel import OWNER, call, signed

CHAT = -1001234567
E = sessions_menu.Entry
H = 3600
DAYS = 24 * H


def topics(*sessions):
    write_private(state_dir() / "topics.json", json.dumps(
        {s: {"thread": 10 + i, "name": f"T{i}", "chat": CHAT, "home": False} for i, s in enumerate(sessions)}))


def live(monkeypatch, states):
    """Sessions as collect() would report them: {session: state}."""
    monkeypatch.setattr(panel, "collect", lambda topics, now=None: [
        E(s, s[:4], 10 + i, 30.0, st, False, CHAT) for i, (s, st) in enumerate(states.items())])


@pytest.fixture
def server():
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he")
    srv = panel.PanelServer(("127.0.0.1", 0), cfg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


# --- stale decisions ------------------------------------------------------------------------------

def test_stale_reasons_use_the_real_records():
    now = 100 * H
    recs = [{"id": "a", "session": "s1", "title": "Release 5284cd49d to Cedar Park?", "created": now - 2 * H},
            {"id": "b", "session": "s1", "title": "Release 7a1b2c3 to Cedar Park?", "created": now - H},
            {"id": "c", "session": "s1", "title": "Merge the banner fix?", "created": now - 80 * H},
            {"id": "d", "session": "s2", "title": "Ship it?", "created": now - 60},
            {"id": "e", "session": "s3", "title": "Fresh question?", "created": now - 60}]
    reasons = panel_view.stale_reasons(recs, {"s1": "idle", "s2": "ended", "s3": "working"}, now)
    assert reasons == {"a": "replaced", "c": "old", "d": "ended"}


def test_waiting_items_come_newest_first_with_stale_ones_marked(server, monkeypatch):
    topics("s1-session", "s2-session")
    live(monkeypatch, {"s1-session": "idle", "s2-session": "ended"})
    now = time.time()
    old = decisions.create("s1-session", CHAT, 10, "Old question?", now=now - 4 * DAYS)
    new = decisions.create("s1-session", CHAT, 10, "New question?", now=now - 60)
    gone = decisions.create("s2-session", CHAT, 11, "From an ended session?", now=now - 120)
    from ctb import board
    write_private(state_dir() / board.STATE_FILE, json.dumps({f"{CHAT}:10": {"id": 0, "items": [
        {"k": "remind", "i": 1, "s": "Older reminder", "at": now - 5 * H},
        {"k": "remind", "i": 2, "s": "Newer reminder", "at": now - 60}]}}))
    body = json.loads(call(server, init=signed())[2])
    assert [(d["id"], d["stale"]) for d in body["decisions"]] == [
        (new["id"], ""), (gone["id"], "ended"), (old["id"], "old")]
    assert [r["text"] for r in body["reminders"]] == ["Newer reminder", "Older reminder"]
    assert body["decisions"][0]["url"] == "https://t.me/c/1234567/10"
    assert body["reminders"][0]["url"] == "https://t.me/c/1234567/10"


def test_the_home_screen_lists_every_topics_open_tasks_newest_first(server, monkeypatch):
    topics("s1-session", "s2-session")
    live(monkeypatch, {"s1-session": "idle", "s2-session": "idle"})
    now = time.time()
    from ctb import board
    write_private(state_dir() / board.STATE_FILE, json.dumps({
        f"{CHAT}:10": {"id": 0, "items": [{"k": "check", "i": 1, "s": "Older task", "x": False, "at": now - 5 * H},
                                          {"k": "check", "i": 2, "s": "Ticked task", "x": True, "at": now - 60}]},
        f"{CHAT}:11": {"id": 0, "items": [{"k": "check", "i": 1, "s": "Newer task", "x": False, "at": now - 60},
                                          {"k": "remind", "i": 2, "s": "A reminder", "at": now - 30}]}}))
    body = json.loads(call(server, init=signed())[2])
    assert [t["text"] for t in body["tasks"]] == ["Newer task", "Older task"]  # a ticked one is done: not listed
    assert [t["url"] for t in body["tasks"]] == ["https://t.me/c/1234567/11", "https://t.me/c/1234567/10"]
    class Places:
        def all_places(self):
            return {"s1-session": {"chat": CHAT, "thread": 10, "name": "T0"}, "s2-session": {"chat": CHAT, "thread": 11, "name": "T1"}}
    listed = panel.open_tasks(Places(), now, here=(CHAT, 10))
    assert [(t["topic"], t["here"]) for t in listed] == [("T1", False), ("T0", True)]
    assert "S.openTasks" in panel.PAGE and all("openTasks" in panel.STRINGS[lang] for lang in panel.STRINGS)


def test_clear_all_closes_only_the_stale_ones_and_needs_a_fresh_login(server, monkeypatch):
    topics("s1-session")
    live(monkeypatch, {"s1-session": "idle"})
    now = time.time()
    old = decisions.create("s1-session", CHAT, 10, "Old question?", now=now - 4 * DAYS)
    fresh = decisions.create("s1-session", CHAT, 10, "Fresh question?", now=now - 60)
    body = json.dumps({"ids": [old["id"], fresh["id"]]}).encode()
    stale_login = signed(auth_date=int(time.time()) - panel.DECIDE_MAX_AGE - 5)
    assert call(server, path="/api/dismiss", init=None, body=body)[0] == 403
    assert call(server, path="/api/dismiss", init=stale_login, body=body)[0] == 403
    assert len(decisions.open_decisions()) == 2
    code, _, data = call(server, path="/api/dismiss", init=signed(), body=body)
    assert code == 200 and json.loads(data)["closed"] == [old["id"]]
    assert [r["id"] for r in decisions.open_decisions()] == [fresh["id"]]  # a fresh one is never cleared
    for bad in (b'{"ids": []}', b'{"ids": ["x"]}', b'{"ids": "a"}', b'{"other": 1}'):
        assert call(server, path="/api/dismiss", init=signed(), body=bad)[0] == 400


# --- what each session is doing ----------------------------------------------------------------------

def test_sessions_show_the_current_step_the_last_summary_and_a_topic_link(server, monkeypatch, tmp_path):
    from ctb import status
    topics("s1-session", "s2-session")
    live(monkeypatch, {"s1-session": "working", "s2-session": "idle"})
    transcript = tmp_path / "s2.jsonl"
    transcript.write_text("\n".join(json.dumps(r) for r in [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Earlier reply."}]}},
        {"type": "assistant", "message": {"content": [{"type": "text",
                                                       "text": "Done with **the fix**.\n\nSummary: Fixed the login; tests pass"}]}},
    ]) + "\n")
    monkeypatch.setattr(status, "transcript_of", lambda s: str(transcript) if s == "s2-session" else None)
    progress = state_dir() / "progress"
    progress.mkdir(parents=True, exist_ok=True)
    (progress / "s1-session.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"kind": "action", "t": 1, "l": "🔍 Grep panel"},
        {"kind": "text", "uuid": "u", "text": "thinking"},
        {"kind": "action", "t": 2, "l": "🧪 Run the focused tests\nsecond line"}]) + "\n")
    body = json.loads(call(server, init=signed())[2])
    by = {s["id"]: s for s in body["sessions"]}
    assert by["s1-sessi"]["now"] == "🧪 Run the focused tests"
    assert by["s2-sessi"]["now"] == ""  # only a working session has a current step
    assert by["s2-sessi"]["summary"] == "Fixed the login; tests pass"
    assert by["s1-sessi"]["url"] == "https://t.me/c/1234567/10"


def test_a_reply_without_a_summary_line_shows_its_first_line(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "```\ncode\n```\n# Shipped the **panel** [PR](https://x)\nmore"}]}}) + "\n")
    assert panel_view.last_summary(str(path)) == "Shipped the panel PR"
    assert panel_view.last_summary(None) == "" and panel_view.last_summary(str(tmp_path / "none")) == ""


@pytest.mark.parametrize("chat,thread,url", [(CHAT, 7, "https://t.me/c/1234567/7"), (12345, 7, ""), (CHAT, None, "")])
def test_topic_links_only_for_supergroup_topics(chat, thread, url):
    assert panel_view.topic_url(chat, thread) == url


# --- settings and radio words ---------------------------------------------------------------------------

def test_settings_are_read_and_changed_like_the_bot_menu(server):
    from ctb import autopilot, delivery, voice
    from ctb.compact import auto_compact_limit
    st = json.loads(call(server, init=signed())[2])["settings"]
    assert set(st) == set(panel_settings.SETTINGS)
    for name, value in (("away", True), ("delivery_end", True), ("voice_confirm", False), ("auto_compact", 300),
                        ("agent_notices", True), ("general_offer", False)):
        body = json.dumps({"name": name, "value": value}).encode()
        assert call(server, path="/api/setting", init=None, body=body)[0] == 403
        code, _, data = call(server, path="/api/setting", init=signed(), body=body)
        assert code == 200 and json.loads(data)["settings"][name] == value
    assert autopilot.away_active() and delivery.default_mode() == "end" and not voice.confirm_on()
    assert auto_compact_limit() == 300


@pytest.mark.parametrize("body", [b'{"name": "away", "value": 1}', b'{"name": "auto_compact", "value": 123}',
                                  b'{"name": "auto_compact", "value": true}', b'{"name": "rm -rf", "value": true}',
                                  b'{"name": "away"}'])
def test_settings_refuse_anything_outside_their_choices(server, body):
    assert call(server, path="/api/setting", init=signed(), body=body)[0] == 400


def test_radio_words_need_the_radio_a_fresh_login_and_a_running_station(monkeypatch):
    from ctb import radio
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he", radio={"every_minutes": 20})
    srv = panel.PanelServer(("127.0.0.1", 0), cfg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        said = []
        body = json.dumps({"text": "שלום לצוות"}).encode()
        assert call(srv, path="/api/radio-say", init=None, body=body)[0] == 403
        monkeypatch.setattr(radio, "job_pid", lambda: None)
        assert call(srv, path="/api/radio-say", init=signed(), body=body)[0] == 409  # the radio is off
        monkeypatch.setattr(radio, "say", lambda text: said.append(text) or "radio_said")
        assert call(srv, path="/api/radio-say", init=signed(), body=body)[0] == 200 and said == ["שלום לצוות"]
        assert call(srv, path="/api/radio-say", init=signed(), body=b'{"text": "  "}')[0] == 400
        assert call(srv, path="/api/radio-say", init=signed(),
                    body=json.dumps({"text": "x" * 601}).encode())[0] == 400
    finally:
        srv.shutdown()
        srv.server_close()


def test_radio_words_are_refused_without_a_configured_radio(server):
    assert call(server, path="/api/radio-say", init=signed(), body=b'{"text": "hi"}')[0] == 404
