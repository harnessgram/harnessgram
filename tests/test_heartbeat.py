import json
import time
from conftest import GOOD_TOKEN
from ctb.config import Config
from ctb import heartbeat as H
from ctb.topics import TopicMap, session_state, set_state


def cfg(translate=True):
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=123, translate=translate,
                  engines=["gemini", "google", "mlx", "nllb"])


def test_hebrew_status_and_caching(monkeypatch):
    H.WORDS.clear()
    monkeypatch.setattr("ctb.translate.ask", lambda prompt, c: "דועך")
    line = "✶ Ebbing… (16s · ↓ 1.1k tokens)"
    assert H.hebrew_status(line, cfg()) == "✶ דועך… (16 שנ׳ · ↓ 1.1k טוקנים)"
    assert H.WORDS["Ebbing"] == "דועך"

    # Cached per-process: translate.ask not called second time even if it would raise
    monkeypatch.setattr("ctb.translate.ask", lambda prompt, c: (_ for _ in ()).throw(RuntimeError("fail")))
    assert H.hebrew_status(line, cfg()) == "✶ דועך… (16 שנ׳ · ↓ 1.1k טוקנים)"

    # Fallback to עובד
    assert H.hebrew_status("✶ Thinking… (2m · thought for 10s)", cfg()) == "✶ עובד… (2 דק׳ · חשב 10 שנ׳)"


def test_working_prefers_pane_spinner_over_transcript(monkeypatch):
    # 1. Pane exists and spinner active
    monkeypatch.setattr(H, "pane_spinner", lambda sid: True)
    assert H.working("sess-1", find=lambda sid: None) is True

    # 2. Pane exists and spinner inactive
    monkeypatch.setattr(H, "pane_spinner", lambda sid: False)
    assert H.working("sess-1", find=lambda sid: None) is False

    # 3. No pane -> fallback to transcript timestamp
    monkeypatch.setattr(H, "pane_spinner", lambda sid: None)
    now = 1000.0
    monkeypatch.setattr(H, "last_activity", lambda path: 990.0)
    assert H.working("sess-1", now=now, find=lambda sid: "/fake/transcript.jsonl") is True

    monkeypatch.setattr(H, "last_activity", lambda path: 900.0)  # > 45s ago
    assert H.working("sess-1", now=now, find=lambda sid: "/fake/transcript.jsonl") is False


def test_bottom_with_status_creates_edits_and_deletes_status(tmp_path, bot):
    c = cfg()
    topics = TopicMap(tmp_path / "state" / "topics.json", bot, c.owner_chat_id)
    topics.ensure("sess-1", "Test Session")

    # Enter busy
    set_state(c, "sess-1", "busy", bot=bot)
    st = session_state("sess-1")
    assert st["state"] == "busy"
    assert "status_id" not in st

    # Bottom with status -> sends status and sends placeholder
    set_state(c, "sess-1", "bottom", bot=bot, status="⏳ ✶ עובד… (10 שנ׳)")
    st = session_state("sess-1")
    status_id = st.get("status_id")
    assert type(status_id) is int
    assert st.get("status_text") == "⏳ ✶ עובד… (10 שנ׳)"

    # Call bottom with SAME status -> does not edit status message
    edits_before = len(bot.methods("editMessageText"))
    set_state(c, "sess-1", "bottom", bot=bot, status="⏳ ✶ עובד… (10 שנ׳)")
    assert len(bot.methods("editMessageText")) == edits_before

    # Call bottom with CHANGED status -> edits status message in place
    set_state(c, "sess-1", "bottom", bot=bot, status="⏳ ✶ חושב… (15 שנ׳)")
    assert len(bot.methods("editMessageText")) == edits_before + 1
    st = session_state("sess-1")
    assert st.get("status_text") == "⏳ ✶ חושב… (15 שנ׳)"
    assert st.get("status_id") == status_id

    # Call bottom with status=None -> drops status message
    set_state(c, "sess-1", "bottom", bot=bot, status=None)
    st = session_state("sess-1")
    assert "status_id" not in st
    assert any(call["message_id"] == status_id for call in bot.methods("deleteMessage"))

    # Enter bottom with status again, then leave busy -> leaving busy deletes status
    set_state(c, "sess-1", "bottom", bot=bot, status="⏳ ✶ עובד…")
    st = session_state("sess-1")
    new_status_id = st.get("status_id")
    assert type(new_status_id) is int
    set_state(c, "sess-1", "done", bot=bot)
    st = session_state("sess-1")
    assert "status_id" not in st
    assert any(call["message_id"] == new_status_id for call in bot.methods("deleteMessage"))


def test_hebrew_status_local_only_never_calls_external(monkeypatch):
    H.WORDS.clear()
    called = []
    monkeypatch.setattr("ctb.translate.ask", lambda prompt, c: called.append(prompt) or "דועך")
    line = "✶ Ebbing… (16s · ↓ 1.1k tokens)"

    # translate=False -> fallback to עובד, no ask call
    c_off = Config(bot_token=GOOD_TOKEN, owner_chat_id=123, translate=False)
    assert H.hebrew_status(line, c_off) == "✶ עובד… (16 שנ׳ · ↓ 1.1k טוקנים)"
    assert len(called) == 0

    # engines local-only -> fallback to עובד, no ask call
    H.WORDS.clear()
    c_local = Config(bot_token=GOOD_TOKEN, owner_chat_id=123, translate=True, engines=["mlx", "nllb"])
    assert H.hebrew_status(line, c_local) == "✶ עובד… (16 שנ׳ · ↓ 1.1k טוקנים)"
    assert len(called) == 0


def test_status_line_rejects_extra_word_or_secret(monkeypatch):
    c = Config(bot_token=GOOD_TOKEN, owner_chat_id=123, translate=True)
    # Extra word inside parentheses
    bad1 = "✶ Ebbing… (16s · ↓ 1.1k tokens · secret_key)"
    assert H.hebrew_status(bad1, c) == ""

    bad2 = "✶ Ebbing… (16s · extra)"
    assert H.hebrew_status(bad2, c) == ""

    # Invalid verb with numbers
    bad3 = "✶ Ebbing123… (16s)"
    assert H.hebrew_status(bad3, c) == ""

    # Valid lines accepted
    valid1 = "✶ Ebbing… (16s · ↓ 1.1k tokens)"
    assert H.hebrew_status(valid1, c) != ""

    valid2 = "✶ Reflecting… (1m 44s · esc to interrupt)"
    assert H.hebrew_status(valid2, c) != ""

    # Test status_line rejects pane with extra word
    class FakeOut:
        returncode = 0
        stdout = "output\n" + bad1 + "\n"

    monkeypatch.setattr("ctb.inbox.pane_for_session", lambda sid: "%1")
    monkeypatch.setattr("ctb.inbox._tmux", lambda *args: FakeOut())
    assert H.status_line("sess-1") == ""


def test_pane_spinner_ignores_old_spinner_higher_up(monkeypatch):
    # An old spinner line followed by 6 non-empty lines must NOT count as working
    old_spinner = "✶ Ebbing… (16s · ↓ 1.1k tokens)"
    lines = [old_spinner] + [f"output line {i}" for i in range(1, 7)]
    pane_content = "\n".join(lines) + "\n\n"

    class FakeOut:
        returncode = 0
        stdout = pane_content

    monkeypatch.setattr("ctb.inbox.pane_for_session", lambda sid: "%1")
    monkeypatch.setattr("ctb.inbox._tmux", lambda *args: FakeOut())

    assert H.pane_spinner("sess-1") is False
    assert H.status_line("sess-1") == ""

    # But if the spinner is within the last 6 non-empty lines, it DOES count
    recent_lines = [f"output line {i}" for i in range(1, 4)] + [old_spinner] + ["output line 5"]
    FakeOut.stdout = "\n".join(recent_lines) + "\n"
    assert H.pane_spinner("sess-1") is True
    assert H.status_line("sess-1") == old_spinner


def test_status_deletion_failure_retried_in_sweep(tmp_path, bot):
    c = Config(bot_token=GOOD_TOKEN, owner_chat_id=123)
    topics = TopicMap(tmp_path / "state" / "topics.json", bot, c.owner_chat_id)
    topics.ensure("sess-1", "Test Session")

    # Enter bottom with status (bottom only applies while busy)
    set_state(c, "sess-1", "busy", bot=bot)
    set_state(c, "sess-1", "bottom", bot=bot, status="⏳ ✶ עובד… (10 שנ׳)")
    st = session_state("sess-1")
    status_id = st.get("status_id")
    assert type(status_id) is int

    # When transitioning to done, deleteMessage fails (not "not found")
    from ctb.telegram import TelegramError
    real_call = bot.call

    def flaky(method, params=None, *a, **k):
        if method == "deleteMessage" and (params or {}).get("message_id") == status_id:
            raise TelegramError("Too Many Requests: retry after 5")
        return real_call(method, params, *a, **k)

    bot.call = flaky
    set_state(c, "sess-1", "done", bot=bot)
    bot.call = real_call

    # Status id must be kept in cur["all"] (the retry list)
    st = session_state("sess-1")
    assert "status_id" not in st
    assert status_id in st.get("all", [])

    # Next transition (deleteMessage succeeds) sweeps and removes it from all
    set_state(c, "sess-1", "done", bot=bot)
    st = session_state("sess-1")
    assert status_id not in st.get("all", [])


def test_hebrew_status_google_engine_alone_does_not_call_gemini(monkeypatch):
    """When only 'google' is configured, Gemini (translate.ask) is not called; the verb falls back to עובד."""
    H.WORDS.clear()
    called_ask = []
    monkeypatch.setattr("ctb.translate.ask", lambda prompt, c: called_ask.append(prompt) or "דועך")
    monkeypatch.setattr("ctb.translate.translate_text", lambda text, c: "דועך בגוגל")

    line = "✶ Ebbing… (16s · ↓ 1.1k tokens)"
    c_google = Config(bot_token=GOOD_TOKEN, owner_chat_id=123, translate=True, engines=["google"])
    assert H.hebrew_status(line, c_google) == "✶ עובד… (16 שנ׳ · ↓ 1.1k טוקנים)"
    assert len(called_ask) == 0


def test_working_pane_without_spinner_falls_back_to_transcript(monkeypatch):
    """When a pane exists but no status line matches strict pattern, working() falls back to transcript activity."""
    # Pane exists but spinner inactive / not matching
    monkeypatch.setattr(H, "pane_spinner", lambda sid: False)

    now = 1000.0
    monkeypatch.setattr(H, "last_activity", lambda path: 980.0)  # 20s ago <= ACTIVE (45s)
    assert H.working("sess-1", now=now, find=lambda sid: "/fake/transcript.jsonl") is True

    monkeypatch.setattr(H, "last_activity", lambda path: 920.0)  # 80s ago > ACTIVE (45s)
    assert H.working("sess-1", now=now, find=lambda sid: "/fake/transcript.jsonl") is False
