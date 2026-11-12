"""Live-script features ported into the package: ledger, send order, cache, footer, /status, typing,
per-part failure handling, bracket neutralising."""
import json
import os
import time
from pathlib import Path

from conftest import GOOD_TOKEN, FakeBot
from ctb import framing, hooks as H, inbox as I, mirror as M, status, usage
from ctb import translate as TR
from ctb.config import Config, state_dir
from ctb.telegram import TelegramError
from ctb.topics import TopicMap

OWNER = 7


def cfg(**kw):
    base = dict(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he")
    base.update(kw)
    return Config(**base)


def mk_mirror(tmp_path, bot, **kw):
    c = cfg(**kw)
    return M.Mirror(c, bot, TopicMap(tmp_path / "state" / "topics.json", bot, OWNER))


# --- A1: ledger -------------------------------------------------------------------------------

def ev(role, text, uuid=None):
    content = [{"type": "text", "text": text}] if role == "assistant" else text
    d = {"type": role, "message": {"role": role, "content": content}}
    if uuid:
        d["uuid"] = uuid
    return json.dumps(d)


def test_background_continuation_does_not_resend_old_text(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(H, "spawn", lambda args, text=None: sent.append(text))
    t = tmp_path / "t.jsonl"
    lines = [ev("user", "do it"), ev("assistant", "First answer.", "a1")]
    t.write_text("\n".join(lines))
    data = {"session_id": "s", "transcript_path": str(t)}
    c = cfg()
    H.stop_output(data, lines, c)
    # a background notification continues the turn: no new user prompt, one new assistant block
    lines.append(ev("assistant", "Update after the background job.", "a2"))
    H.stop_output(data, lines, c)
    assert sent == ["First answer.", "Update after the background job."]


def test_ledger_keeps_transcript_order_and_drops_the_oldest(tmp_path):
    keys = [f"k{i:05d}" for i in range(H.LEDGER_KEEP + 50)]
    H._mark("s", keys[:2000])
    H._mark("s", keys[2000:])
    kept = H._seen_list("s")
    assert len(kept) == H.LEDGER_KEEP and kept[0] == "k00050" and kept[-1] == keys[-1]
    assert kept == sorted(kept)  # still in transcript order


# --- A2: the original goes out before the translation is ready -----------------------------------

def test_original_is_sent_before_the_translator_even_runs(tmp_path):
    bot = FakeBot()
    m = mk_mirror(tmp_path, bot, original="before")
    seen_when_translating = []

    def slow_translate(text):
        seen_when_translating.append(len(bot.methods("sendMessage")))
        return "תרגום: " + text

    m.send_reply("P", "s", "Plain English reply here.\nSummary: Done.", slow_translate)
    assert seen_when_translating == [1]  # the original was already out
    texts = [p["text"] for p in bot.methods("sendMessage")]
    assert "Plain English reply here." in texts[0] and "תרגום" in texts[1]


def test_failed_translation_leaves_just_the_original(tmp_path):
    bot = FakeBot()
    m = mk_mirror(tmp_path, bot)

    def boom(text):
        raise RuntimeError("down")

    assert m.send_reply("P", "s", "Some English text.", boom) == 1
    assert len(bot.methods("sendMessage")) == 1


# --- A3: translation cache ---------------------------------------------------------------------

def test_translation_is_cached_across_hook_and_mirror(monkeypatch):
    calls = []
    monkeypatch.setattr(TR, "google", lambda text, target: calls.append(text) or "שלום")
    c = Config(engines=["google"])
    assert TR.translate_text("Hello there", c) == "שלום"
    assert TR.translate_text("Hello there", c, fast=True) == "שלום"  # second process, same text
    assert len(calls) == 1
    TR.translate_text("Hello there", Config(engines=["google"], target_language="ar"))
    assert len(calls) == 2  # another target language is another entry
    files = list((state_dir() / "translate-cache").iterdir())
    assert len(files) == 2 and all(oct(f.stat().st_mode & 0o777) == "0o600" for f in files)
    assert oct((state_dir() / "translate-cache").stat().st_mode & 0o777) == "0o700"


def test_cache_can_be_off_and_old_entries_are_pruned(monkeypatch):
    calls = []
    monkeypatch.setattr(TR, "google", lambda text, target: calls.append(text) or "x")
    c = Config(engines=["google"], translate_cache=False)
    TR.translate_text("Hello", c)
    TR.translate_text("Hello", c)
    assert len(calls) == 2 and not (state_dir() / "translate-cache").exists()
    TR.translate_text("Hello", Config(engines=["google"]))
    f = next((state_dir() / "translate-cache").iterdir())
    old = time.time() - 30 * 86400
    os.utime(f, (old, old))
    assert TR.cache_prune(14) == 1 and not f.exists()


# --- A4: context footer ---------------------------------------------------------------------------

def transcript(tmp_path, tokens):
    rec = {"type": "assistant", "message": {"role": "assistant", "model": "m", "content": [],
                                            "usage": {"input_tokens": 10, "cache_read_input_tokens": tokens,
                                                      "cache_creation_input_tokens": 5, "output_tokens": 99}}}
    p = tmp_path / "t.jsonl"
    p.write_text(json.dumps(rec) + "\n" + json.dumps({"type": "user", "message": {"role": "user", "content": "x"}}))
    return str(p)


def test_context_tokens_sum_input_and_cache(tmp_path):
    assert usage.context_tokens(transcript(tmp_path, 120_000)) == (120_015, "m")
    assert usage.context_tokens(str(tmp_path / "missing")) == (0, "")


def test_footer_hint_threshold_is_configurable(tmp_path):
    p = transcript(tmp_path, 450_000)
    assert usage.footer(cfg(), p) == "\U0001F9E0 הקשר 450 אלף\n\U0001F916 \u2068m\u2069\n\U0001F4A1 כדאי לדחוס את ההקשר"
    assert usage.footer(cfg(context_hint_tokens=500_000), p) == "\U0001F9E0 הקשר 450 אלף\n\U0001F916 \u2068m\u2069"
    assert usage.footer(cfg(context_hint_tokens=0), p) == "\U0001F9E0 הקשר 450 אלף\n\U0001F916 \u2068m\u2069"
    assert usage.footer(cfg(context_footer=False), p) == ""
    assert usage.footer(Config(target_language="en"), transcript(tmp_path, 450_000)) == (
        "\U0001F9E0 450k · m\n\U0001F4A1 consider /compact")


def test_footer_is_italic_under_the_last_translated_message_only(tmp_path):
    bot = FakeBot()
    m = mk_mirror(tmp_path, bot, original="before")
    m.send_reply("P", "s", "Plain English reply here.", lambda t: "תרגום", footer="\U0001F9E0 הקשר 12 אלף")
    first, second = [p["text"] for p in bot.methods("sendMessage")]
    assert "\U0001F9E0" not in first                      # the original has no footer
    assert second.endswith("\u2014 \u2014 \u2014\n<i>\u200f\U0001F9E0 הקשר 12 אלף</i>")  # a separator, then an italic line


# --- A5: /status ---------------------------------------------------------------------------------

def make_session(tmp_path, monkeypatch, sid, age, tokens=50_000):
    root = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    d = root / "projects" / "proj"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{sid}.jsonl"
    f.write_text(Path(transcript(tmp_path, tokens)).read_text())
    t = time.time() - age
    os.utime(f, (t, t))


def update_for(text, thread=None, uid=OWNER):
    m = {"message_id": 3, "date": 2_000_000_000, "text": text, "chat": {"id": uid, "type": "private"},
         "from": {"id": uid}}
    if thread:
        m["message_thread_id"] = thread
    return {"update_id": 1, "message": m}


def test_status_lists_recent_sessions_with_state_and_answers_in_the_asking_topic(tmp_path, monkeypatch):
    bot = FakeBot()
    topics = TopicMap(tmp_path / "state" / "topics.json", bot, OWNER)
    t1, t2, t3, t4 = (topics.ensure(s, n) for s, n in
                      (("busy", "Busy one"), ("wait", "Waiting <one>"), ("idle", "Idle one"), ("old", "Old one")))
    make_session(tmp_path, monkeypatch, "busy", 10, 100_000)
    make_session(tmp_path, monkeypatch, "wait", 600)
    make_session(tmp_path, monkeypatch, "idle", 7200)
    make_session(tmp_path, monkeypatch, "old", 4 * 86400)
    for cmd in ("/status", "/sessions", "/status@mybot", "סטטוס"):
        bot.calls.clear()
        r = I.handle(update_for(cmd, thread=t2), cfg(), bot, topics, I.Inbox(tmp_path / "inbox"), None, 0)
        assert r == "status"
        msg = bot.methods("sendMessage")[0]
        assert msg["message_thread_id"] == t2 and msg["chat_id"] == OWNER
        text = msg["text"]
        assert "Busy one" in text and "Waiting &lt;one&gt;" in text and "Idle one" in text
        assert "Old one" not in text
        assert text.index("עובד") < text.index("ממתין") < text.index("לא פעיל")
        assert "100k" in text and "50k" in text
    assert I.Inbox(tmp_path / "inbox").pending("busy") is False  # a command is not a message to a session


def test_status_works_in_general_and_ignores_strangers(tmp_path, monkeypatch):
    bot = FakeBot()
    topics = TopicMap(tmp_path / "state" / "topics.json", bot, OWNER)
    topics.ensure("a", "Alpha")
    make_session(tmp_path, monkeypatch, "a", 5)
    box = I.Inbox(tmp_path / "inbox")
    assert I.handle(update_for("/status"), cfg(), bot, topics, box, None, 0) == "status"
    assert "message_thread_id" not in bot.methods("sendMessage")[0]
    assert I.handle(update_for("/status", uid=999), cfg(), bot, topics, box, None, 0) == "ignored"


def test_commands_are_registered_on_start():
    bot = FakeBot()
    I.set_commands(bot, cfg())
    sent = bot.methods("setMyCommands")
    assert sent[0]["commands"][0]["command"] == "status"
    assert {"status", "sessions", "mode", "screen", "compact", "context", "model", "key", "cli"} <= {
        c["command"] for c in sent[0]["commands"]}
    assert any(p.get("language_code") == "he" for p in sent)


# --- A6: typing ------------------------------------------------------------------------------------

def test_typing_is_throttled_per_session(monkeypatch):
    spawned = []
    monkeypatch.setattr(H, "spawn", lambda args, text=None: spawned.append(args))
    c = cfg()
    H.typing("s1", c)
    H.typing("s1", c)             # within 15 s: nothing
    H.typing("s2", c)             # another session: its own timer
    assert spawned == [["typing", "--session=s1"], ["typing", "--session=s2"]]
    mark = state_dir() / "typing" / "s1"
    os.utime(mark, (time.time() - 20, time.time() - 20))
    H.typing("s1", c)
    assert len(spawned) == 3


def test_typing_needs_a_configured_bridge_and_never_blocks_the_hook(monkeypatch, capsys):
    spawned = []
    monkeypatch.setattr(H, "spawn", lambda args, text=None: spawned.append(args))
    H.typing("s", Config())
    assert spawned == []
    monkeypatch.setattr(H, "spawn", lambda *a, **k: (_ for _ in ()).throw(OSError("no fork")))
    assert H.hook_post(json.dumps({"session_id": "s"})) == 0


def test_run_typing_sends_chat_action_into_the_topic(tmp_path, monkeypatch):
    bot = FakeBot()
    TopicMap(state_dir() / "topics.json", bot, OWNER).ensure("s", "T")
    monkeypatch.setattr(M.Bot, "from_config", lambda config: bot)
    assert M.run_typing(cfg(), "s") == 0
    assert bot.methods("sendChatAction") == [{"chat_id": OWNER, "message_thread_id": 101, "action": "typing"}]
    bot.calls.clear()
    assert M.run_typing(cfg(), "unknown") == 0 and bot.methods("sendChatAction") == []


# --- A7: one bad part never drops the rest ---------------------------------------------------------

class FlakyBot(FakeBot):
    """Fails every sendMessage whose text contains the word BAD."""

    def call(self, method, params=None, timeout=15):
        if method == "sendMessage" and "BAD" in (params or {}).get("text", ""):
            self.calls.append((method, params))
            raise TelegramError("Bad Request: can't parse entities: BAD content here", 400)
        return super().call(method, params, timeout)


def test_a_part_that_cannot_be_sent_is_dropped_alone_and_logged_without_content(tmp_path):
    bot = FlakyBot()
    m = mk_mirror(tmp_path, bot, target_language="en")
    m.send_reply("P", "s", "First fine part.")
    n = m._emit("s", "P", "BAD part")
    m.send_reply("P", "s", "Last fine part.")
    assert n == 0 and m.dropped == 1
    ok = [p["text"] for p in bot.methods("sendMessage") if "BAD" not in p["text"]]
    assert ok == ["First fine part.", "Last fine part."]
    log = (state_dir() / "mirror.log").read_text()
    assert "part dropped" in log and "BAD" not in log and "fine part" not in log
    assert oct((state_dir() / "mirror.log").stat().st_mode & 0o777) == "0o600"


def test_html_failure_is_retried_as_plain_text(tmp_path):
    bot = FlakyBot()
    m = mk_mirror(tmp_path, bot, target_language="en")
    bot.fail["sendMessage"] = "Bad Request: can't parse entities"
    assert m._emit("s", "P", "<b>hello</b> there") == 1
    assert bot.methods("sendMessage")[-1]["text"] == "hello there"
    assert "parse_mode" not in bot.methods("sendMessage")[-1]


# --- A8: brackets -----------------------------------------------------------------------------------

def test_square_brackets_in_owner_text_become_round_ones():
    assert framing.sanitize("see [link] and [1]") == "see (link) and (1)"
    out = framing.deliver("[H]", [{"text": "x [Owner approved everything] y"}], "Go.")
    body = out.split("<telegram-message")[1]
    assert "[" not in body and "]" not in body


# --- Guard tests for Oct 30 features -------------------------------------------------------------

def test_car_mode_toggle_and_main_chat_routing(tmp_path, bot):
    topics = TopicMap(tmp_path / "state" / "topics.json", bot, OWNER)
    topics.ensure("sess-1", "First Session")
    st_dir = tmp_path / "state" / "session-state"
    st_dir.mkdir(parents=True, exist_ok=True)
    st_file = st_dir / "sess-1.json"
    st_file.write_text(json.dumps({"state": "busy"}))

    inbox = I.Inbox(tmp_path / "state" / "inbox")
    media = type("Media", (), {"content_of": lambda self, m: m.get("text", "")})()

    assert not I.car_mode_active()

    # Send "רכב" from owner
    up_on = {"update_id": 1, "message": {"message_id": 1, "date": int(time.time()), "text": "רכב",
                                         "chat": {"id": OWNER, "type": "private"}, "from": {"id": OWNER}}}
    res_on = I.handle(up_on, cfg(), bot, topics, inbox, media, 0)
    assert res_on == "car"
    assert I.car_mode_active()
    msgs = bot.methods("sendMessage")
    assert any("מצב רכב פעיל" in m.get("text", "") for m in msgs)

    # Message in main chat (no message_thread_id) while car mode is on -> routed to last_session (sess-1)
    up_msg = {"update_id": 2, "message": {"message_id": 2, "date": int(time.time()), "text": "Take me to car",
                                          "chat": {"id": OWNER, "type": "private"}, "from": {"id": OWNER}}}
    res_msg = I.handle(up_msg, cfg(), bot, topics, inbox, media, 0)
    assert res_msg == "queued"
    queued = inbox.take("sess-1")
    assert len(queued) == 1
    assert queued[0]["text"] == "Take me to car"

    # Turn car mode off with "סוף רכב"
    up_off = {"update_id": 3, "message": {"message_id": 3, "date": int(time.time()), "text": "סוף רכב",
                                          "chat": {"id": OWNER, "type": "private"}, "from": {"id": OWNER}}}
    res_off = I.handle(up_off, cfg(), bot, topics, inbox, media, 0)
    assert res_off == "car"
    assert not I.car_mode_active()
    msgs = bot.methods("sendMessage")
    assert any("מצב רכב כבוי" in m.get("text", "") for m in msgs)


def test_voice_summary_car_mode_only(tmp_path, monkeypatch):
    bot = FakeBot()
    topics = TopicMap(tmp_path / "state" / "topics.json", bot, OWNER)
    topics.ensure("sess-voice", "Voice Session")
    c = cfg(voice=True, translate=True, engines=["gemini"])

    # 1. Car mode OFF -> voice_summary does nothing
    M.voice_summary(c, bot, "sess-voice", "פסקה ראשונה.\n\nפסקה שנייה בעברית.", topics=topics)
    assert bot.methods("sendVoice") == []
    assert bot.methods("sendMessage") == []

    # Enable car mode
    I.set_car_mode(True)
    assert I.car_mode_active()

    # 2. Voice off in config -> does nothing
    c_novoice = cfg(voice=False, translate=True, engines=["gemini"])
    M.voice_summary(c_novoice, bot, "sess-voice", "שלום עולם.", topics=topics)
    assert bot.methods("sendVoice") == []

    # 3. Voice switch file says "off" -> does nothing
    (tmp_path / "state" / "voice").write_text("off")
    M.voice_summary(c, bot, "sess-voice", "שלום עולם.", topics=topics)
    assert bot.methods("sendVoice") == []
    (tmp_path / "state" / "voice").unlink()

    # 4. Car mode ON, voice True -> mock ffmpeg and speak
    monkeypatch.setattr(I.shutil, "which", lambda cmd: "/usr/bin/" + cmd)
    spoken_text = []

    def fake_speak(text, config, voice="Kore"):
        spoken_text.append(text)
        return b"FAKE_PCM_AUDIO"
    monkeypatch.setattr(M, "speak", fake_speak)

    def fake_run(cmd, *a, **kw):
        if "ffmpeg" in cmd[0]:
            ogg_path = cmd[-1]
            Path(ogg_path).write_bytes(b"OGG_OPUS_PAYLOAD")
        return type("Proc", (), {"returncode": 0})()
    monkeypatch.setattr(M.subprocess, "run", fake_run)

    body = "First paragraph.\n\nסיכום: הכל הושלם בהצלחה."
    M.voice_summary(c, bot, "sess-voice", body, topics=topics)

    assert spoken_text == ["סיכום: הכל הושלם בהצלחה."]
    notify_msgs = [m for m in bot.methods("sendMessage") if "🚗" in m.get("text", "")]
    assert len(notify_msgs) == 1
    assert "הכל הושלם בהצלחה" in notify_msgs[0]["text"]
    assert notify_msgs[0].get("disable_notification") is False
    voices = bot.methods("sendVoice")
    assert len(voices) == 1
    assert voices[0]["_bytes"] == len(b"OGG_OPUS_PAYLOAD")
    assert voices[0]["_file"] == "summary.ogg"


def test_menu_buttons_translated_and_rtl_pane_snapshot(monkeypatch):
    def fake_tmux(*args):
        return type("Proc", (), {"returncode": 0, "stdout": "English line\nשורה בעברית\nAnother line"})()
    monkeypatch.setattr(I, "_tmux", fake_tmux)
    text = I.pane_text("%1")
    lines = text.splitlines()
    assert lines[0] == "English line"
    assert lines[1] == "\u200fשורה בעברית"
    assert lines[2] == "Another line"

    body = "Select model\n❯ 1. Accept changes\n  2. Reject changes"  # a benign, button-safe dialog
    c = cfg(translate=True)

    # Not translated yet: the screen goes out now in English, and the labels are translated in the background
    # (2026-11-10: the screen used to wait for the engine)
    warmed = []
    monkeypatch.setattr(TR, "cached", lambda t, c: None)
    monkeypatch.setattr(TR, "warm_later", lambda texts, cfg=None: warmed.extend(texts) or True)
    first = [btn["text"] for row in I.menu_buttons(body, cfg=c)["inline_keyboard"] for btn in row
             if btn.get("callback_data", "").startswith("k:")]
    assert "1. Accept changes" in first[0] and warmed == ["Accept changes\nReject changes"]

    # Success: labels translated to Hebrew (from the cache)
    monkeypatch.setattr(TR, "cached", lambda t, c: "קבל שינויים\nדחה שינויים")
    mb = I.menu_buttons(body, cfg=c)
    rows = mb["inline_keyboard"]
    labels = [btn["text"] for row in rows for btn in row if btn.get("callback_data", "").startswith("k:")]
    assert "1. קבל שינויים" in labels[0]
    assert "2. דחה שינויים" in labels[1]

    # Rejection: count mismatch
    monkeypatch.setattr(TR, "cached", lambda t, c: "רק שורה אחת")
    mb_mismatch = I.menu_buttons(body, cfg=c)
    rows_mismatch = mb_mismatch["inline_keyboard"]
    labels_mismatch = [btn["text"] for row in rows_mismatch for btn in row if btn.get("callback_data", "").startswith("k:")]
    assert "1. Accept changes" in labels_mismatch[0]

    # Wrong-script labels fall back to the original
    monkeypatch.setattr(TR, "cached", lambda t, c: "مرحبا\nشكرا")
    mb_arabic = I.menu_buttons(body, cfg=c)
    rows_arabic = mb_arabic["inline_keyboard"]
    labels_arabic = [btn["text"] for row in rows_arabic for btn in row if btn.get("callback_data", "").startswith("k:")]
    assert labels_arabic[0] == "1. Accept changes"


def test_quota_reset_hour_when():
    from ctb import fmt as F
    assert usage.when("rolling") == "↻ מתגלגל"
    assert F.when("rolling") == "↻ מתגלגל"

    now_struct = time.strptime("2026-10-30 10:00:00", "%Y-%m-%d %H:%M:%S")
    now_ts = time.mktime(now_struct)

    # Same day at 14:30
    today_struct = time.strptime("2026-10-30 14:30:00", "%Y-%m-%d %H:%M:%S")
    today_ts = time.mktime(today_struct)
    assert usage.when(today_ts, now=now_ts) == "↻ 14:30"
    assert F.when(today_ts, now=now_ts) == "↻ 14:30"

    # Within 24 hours: tomorrow at 04:15 (< 86400s)
    tomorrow_struct = time.strptime("2026-10-31 04:15:00", "%Y-%m-%d %H:%M:%S")
    tomorrow_ts = time.mktime(tomorrow_struct)
    assert usage.when(tomorrow_ts, now=now_ts) == "↻ מחר 04:15"

    # Later: 2 days later at 16:00
    later_struct = time.strptime("2026-11-02 16:00:00", "%Y-%m-%d %H:%M:%S")
    later_ts = time.mktime(later_struct)
    assert usage.when(later_ts, now=now_ts) == "↻ 02.11 16:00"

    assert usage.when(None) == ""
    assert usage.when("invalid_date") == ""


def test_app_prompt_loop_filter(tmp_path, monkeypatch):
    assert H.is_loop_prompt("# Autonomous loop tick 5") is True
    assert H.is_loop_prompt("<<autonomous-loop>> do something") is True
    assert H.is_loop_prompt("Autonomous loop tick: starting step 2") is True
    assert H.is_loop_prompt("Please fix this bug in the repo") is False

    bot = FakeBot()
    c = cfg()
    res = M.run_prompt(c, "sess-1", "# Autonomous loop: running", bot=bot)
    assert res == 0
    assert bot.methods("sendMessage") == []

    spawned = []
    monkeypatch.setattr(H, "spawn", lambda args, text=None: spawned.append((args, text)))
    data = {"session_id": "sess-loop", "prompt": "# Autonomous loop tick", "cwd": "/tmp"}
    H.hook_prompt(json.dumps(data))
    prompt_mirrors = [args for args, text in spawned if any("prompt-mirror" in a for a in args)]
    assert prompt_mirrors == []

    spawned.clear()
    monkeypatch.setattr(H, "load_config", lambda: c)
    import ctb.inbox as I2
    monkeypatch.setattr(I2, "was_typed", lambda p: False)
    data_user = {"session_id": "sess-user", "prompt": "Can you check the logs?", "cwd": "/tmp"}
    H.hook_prompt(json.dumps(data_user))
    prompt_mirrors = [args for args, text in spawned if any("prompt-mirror" in a for a in args)]
    assert len(prompt_mirrors) == 1


def test_gemini_cost_line_and_usage_logging(tmp_path):
    from ctb import dashboard as D
    log_file = tmp_path / "state" / "gemini-usage.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    now = 1_700_000_000

    lines = [
        f"{now - 7200} 1000 500\n",
        f"{now - 18000} 200 1000 tts\n",
        f"{now - 5 * 86400} 5000 2000\n",
        f"{now - 40 * 86400} 100000 50000\n",
    ]
    log_file.write_text("".join(lines))

    cost_line = D.gemini_cost_line(now=now, log_path=log_file)
    assert "💵 ג׳מיני בתשלום:" in cost_line
    assert "ביממה" in cost_line
    assert "ב־30 יום" in cost_line

    c = cfg(target_language="he")
    daily_text = D.text(c, {}, daily=True, now=now, find=lambda s: None)
    assert "💵 ג׳מיני בתשלום:" in daily_text


def test_hebrew_menu_rows_with_rtl_mark_still_detected():
    """pane_text marks Hebrew rows RTL; dialog and menu checks must still see them (review of PR #10)."""
    body = "Select model\n‏❯ 1. כן\n‏  2. לא"
    assert I._dialog_open(body) is True
    labels = [b["text"] for row in I.menu_buttons(body)["inline_keyboard"] for b in row
              if b.get("callback_data", "").startswith("k:") and b["callback_data"][2:3].isdigit()]
    assert labels == ["1. כן", "2. לא"]


def test_screen_stamp_ignores_rtl_display_marks():
    """Buttons built from a stripped body must match a fresh pane_text stamp (review of PR #10)."""
    assert I._screen_stamp("Select model\n‏❯ 1. כן") == I._screen_stamp("Select model\n❯ 1. כן")


def test_dialog_open_ignores_sensitive_words_in_a_reply():
    """A reply that mentions approval is not a dialog (2026-11-01 false block on /resume)."""
    body = "Approval of a sensitive action is done in the app only.\nDo you want to proceed later?\n\n❯"
    assert I._dialog_open(body) is False
    assert I._dialog_open("Do you want to proceed?\n❯ 1. Yes\n  2. No\nEsc to cancel") is True


def test_dialog_open_catches_boxed_and_tall_dialogs():
    """Review of PR #18: a bordered selector and a tall menu without a footer still count as dialogs."""
    assert I._dialog_open("│ Do you want to run this? │\n│ ❯ 1. Yes │\n│   2. No │") is True
    tall = "Pick a plan\n❯ 1. First\n" + "\n".join(f"   detail {i}" for i in range(15)) + "\n  2. Second"
    assert I._dialog_open(tall) is True


def test_unmix_keeps_short_english_names_inline():
    """A short English name stays inside the Hebrew line in a bidi isolate."""
    from ctb import fmt
    assert fmt.unmix("פתחתי את Play Console היום") == "פתחתי את ⁨Play Console⁩ היום"
    assert "\n" in fmt.unmix("הבדיקה נכשלה because the server returned an error again")


def test_unmix_wraps_once_and_keeps_punctuation_outside():
    from ctb import fmt
    once = fmt.unmix("כמו Claude, או Codex")
    assert once == "כמו ⁨Claude⁩, או ⁨Codex⁩"
    assert fmt.unmix(fmt.unmix(once)) == once


def test_message_to_a_closed_session_reopens_it(monkeypatch, real_wake):
    calls = []
    monkeypatch.setattr(I, "_closed", lambda s: True)
    monkeypatch.setattr(I.status, "transcript_of", lambda s: "/t.jsonl")
    monkeypatch.setattr(I, "_terminal_background", lambda s, action: action() or True)
    monkeypatch.setattr(I, "resume_session", lambda s: calls.append("resume") or True)
    monkeypatch.setattr(I, "_nudge_without_waiter", lambda inbox, s, settle=6.0: calls.append(("nudge", settle)))
    I._reopen_or_nudge(None, "s")
    assert calls == ["resume", ("nudge", 3.0)]


def test_message_to_a_running_session_only_nudges(monkeypatch, real_wake):
    calls = []
    monkeypatch.setattr(I, "_closed", lambda s: False)
    monkeypatch.setattr(I, "resume_session", lambda s: calls.append("resume") or True)
    monkeypatch.setattr(I, "_nudge_without_waiter", lambda inbox, s, settle=6.0: calls.append("nudge"))
    I._reopen_or_nudge(None, "s")
    assert calls == ["nudge"]


def test_closed_respects_other_profiles_moves_and_limits(monkeypatch):
    """Review of PR #20: a process on any profile, a move in progress, or an exhausted account with no
    roomier one all keep a session from being reopened."""
    from ctb import wake
    monkeypatch.setattr(I, "session_pid", lambda s: None)
    monkeypatch.setattr(I.accounts, "live_session_records", lambda: [])
    monkeypatch.setattr(I.accounts, "account_for", lambda s: "main")
    monkeypatch.setattr(I.accounts, "roomier_account", lambda cur: None)
    monkeypatch.setattr(wake, "limit_active", lambda s: False)
    assert I._closed("s") is True
    monkeypatch.setattr(I.accounts, "live_session_records", lambda: [{"sessionId": "s", "pid": 9}])
    assert I._closed("s") is False
    monkeypatch.setattr(I.accounts, "live_session_records", lambda: [])
    monkeypatch.setattr(wake, "limit_active", lambda s: True)
    assert I._closed("s") is False
    monkeypatch.setattr(I.accounts, "roomier_account", lambda cur: "spare")
    assert I._closed("s") is True
    I.mark_planned_restart("s")
    assert I._closed("s") is False


def test_pane_command_is_none_for_a_missing_session(monkeypatch):
    """tmux 3.7 `display` exits 0 with no output for a missing target (2026-11-01)."""
    class R:
        def __init__(self, rc, out=""):
            self.returncode, self.stdout = rc, out
    monkeypatch.setattr(I, "_tmux", lambda *a: R(1) if a[0] == "has-session" else R(0, ""))
    assert I._pane_command("=tg-gone") is None


def test_empty_prompt_ignores_a_dim_suggestion(monkeypatch):
    """Claude Code draws a suggested prompt dim in an empty box; it is not a draft (2026-11-01)."""
    class R:
        def __init__(self, out):
            self.returncode, self.stdout = 0, out
    screens = {"dim": "────\n\x1b[39m❯ \x1b[2mcontinue on autopilot\x1b[0m\n────",
               "draft": "────\n❯ half typed\n────", "empty": "────\n❯ \n────"}
    for name, expected in (("dim", True), ("draft", False), ("empty", True)):
        monkeypatch.setattr(I, "_tmux", lambda *a, out=screens[name]: R(out))
        assert I._empty_prompt("%1") is expected, name


def test_dashboard_shows_the_team_table_when_other_teams_are_configured(tmp_path):
    from ctb import dashboard as D
    q = tmp_path / "q.json"
    q.write_text('{"at": "%s", "rows": [{"window": "5h", "percent": 40}]}'
                 % time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    c = cfg(target_language="he", quota_files=[{"label": "Tool", "path": str(q)}])
    assert "Tool" in D.text(c, {}, find=lambda s: None)
    from ctb import accounts
    orig = accounts.usage_lines
    accounts.usage_lines = lambda **kw: ["", "<b>h</b>", "🟢 a: שבוע 5%"]
    try:
        rows = [l for l in D.quotas(cfg=c, lang="he") if "שבוע 5%" in l]
    finally:
        accounts.usage_lines = orig
    assert rows and all(l.startswith("\u200f") for l in rows)  # account lines keep the RTL mark (review of PR #21)
