"""Voice digest scheduling, durable delivery, privacy and Cockpit settings."""
import datetime as dt
import importlib
import json
import os
import threading
import time

import pytest

from ctb import config, panel_page, panel_settings, radio, radio_talk, radio_voice, voice_digest as D
from conftest import GOOD_TOKEN
from ctb.telegram import TelegramError

TEXT = "מסלול: מסך הבית מוכן והבדיקות עברו. אפשר להמשיך לשלב הבא."


@pytest.fixture
def clock(monkeypatch):
    old = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "Asia/Jerusalem")
    time.tzset()
    yield dt.datetime(2026, 11, 9, 12).timestamp()
    if old is None:
        monkeypatch.delenv("TZ", raising=False)
    else:
        monkeypatch.setenv("TZ", old)
    time.tzset()


def cfg(level="important"):
    return config.Config(bot_token=GOOD_TOKEN, owner_chat_id=7, radio={"voice": "Kore"}, voice_digest=level)


def add(c, now, key="one", important=True, text=TEXT, **extra):
    D.enqueue(c, key, dict(text=text, say=text, replied_at=now, important=important, trivial=False, **extra))


@pytest.fixture
def audio(monkeypatch):
    said = []
    monkeypatch.setattr(D, "audio", lambda c, text: said.append(text) or b"OggS-test")
    return said


@pytest.mark.parametrize("level,seconds", [("important", 3600), ("hourly", 3600), ("every_3_hours", 10800)])
def test_cadence_and_no_repeat_after_restart(clock, bot, audio, level, seconds, monkeypatch):
    c = cfg(level)
    add(c, clock)
    assert not D.tick(c, bot, clock + seconds - 1)
    assert D.tick(c, bot, clock + seconds)
    assert not D.tick(c, bot, clock + seconds + 1)
    importlib.reload(D)
    monkeypatch.setattr(D, "audio", lambda c, text: audio.append(text) or b"OggS-test")
    assert not D.tick(c, bot, clock + seconds + 2)
    add(c, clock + seconds + 3, "two")
    assert not D.tick(c, bot, clock + 2 * seconds - 1)
    assert D.tick(c, bot, clock + 2 * seconds)
    assert len(audio) == 2
    state = config.read_map(D.folder() / "state.json")
    assert "pending" not in state and state["last"] == clock + 2 * seconds


@pytest.mark.parametrize("level,mirror", [("off", True), ("hourly", False)])
def test_disabled_never_collects_or_sends(clock, bot, audio, level, mirror):
    c = cfg(level)
    c.mirror = mirror
    add(c, clock)
    assert not D.tick(c, bot, clock + 3600)
    assert not audio


@pytest.mark.parametrize("source,expected", [
    ("Details of an ordinary ongoing investigation without a milestone.", False),
    ("Details of ongoing work.\n\nSummary: Nothing new.", False),
    ("Details of ongoing work.\n\nSummary: Still working.", False),
    ("Details of ongoing work.\n\nSummary: The screen is ready.", True),
    ("Decision: keep the current screen for this release.", True),
    ("The background job completed and its output is available.", True),
    ("An error prevented the upload. Please try later.", True),
    ("המשימה הושלמה והקובץ מוכן לבדיקה.", True),
])
def test_importance_rule(source, expected):
    assert D.important(source) is expected


def test_important_level_and_trivial_filter(clock, bot, audio):
    c = cfg()
    add(c, clock, "one", important=False)
    add(c, clock, "two")
    D.enqueue(c, "three", {"text": "OK.", "replied_at": clock, "important": True})
    assert D.tick(c, bot, clock + 3600)
    assert audio == [TEXT]
    assert not list((D.folder() / "pending").glob("*.json"))


def test_hourly_includes_ordinary_updates(clock, bot, audio):
    c = cfg("hourly")
    add(c, clock, important=False)
    assert D.tick(c, bot, clock + 3600)


@pytest.mark.parametrize("day,hour,minute,expected", [
    (9, 22, 59, False), (9, 23, 0, True), (10, 6, 59, True), (10, 7, 0, False),
    (13, 11, 59, False), (13, 12, 0, True), (14, 12, 0, True), (15, 7, 0, False),
])
def test_quiet_hours_and_sabbath(clock, day, hour, minute, expected):
    assert D.quiet(cfg(), dt.datetime(2026, 11, day, hour, minute).timestamp()) is expected


def test_dated_holiday_windows_and_validation(clock, bot, audio):
    c = cfg()
    c.voice_digest_quiet_windows.append({"start": "2026-11-09T08:00", "end": "2026-11-10T20:00"})
    add(c, clock)
    assert not D.tick(c, bot, clock + 3600)
    assert not audio
    assert D.valid_windows(c.voice_digest_quiet_windows)
    for windows in ([{}], [None], [{"start": "fri 99:00", "end": "sat 20:00"}], "bad"):
        assert not D.valid_windows(windows)
        c.voice_digest_quiet_windows = windows
        assert D.quiet(c, clock)


def test_mirror_switch_checked_live(clock, bot, audio):
    c = cfg()
    add(c, clock)
    config.save_raw({"mirror": False})
    assert not D.tick(c, bot, clock + 3600)
    assert not audio


def test_switch_off_during_audio_prevents_delivery(clock, bot, monkeypatch):
    c = cfg()
    add(c, clock)
    def make_audio(*args):
        config.save_raw({"mirror": True, "voice_digest": "off"})
        return b"OggS-test"
    monkeypatch.setattr(D, "audio", make_audio)
    assert not D.tick(c, bot, clock + 3600)
    assert not bot.calls


def test_failed_send_retries_next_round_once_then_drops(clock, bot, audio, monkeypatch, caplog):
    c = cfg()
    add(c, clock)
    calls = []
    def fail(*args):
        calls.append(args)
        raise TelegramError("private provider detail", 400, api_rejected=True)
    monkeypatch.setattr(bot, "upload", fail)
    assert not D.tick(c, bot, clock + 3600)
    importlib.reload(D)
    monkeypatch.setattr(D, "audio", lambda c, text: audio.append(text) or b"OggS-test")
    assert not D.tick(c, bot, clock + 3630)
    assert not D.tick(c, bot, clock + 3660)
    assert len(calls) == 2
    assert caplog.text.count("dropped after two failed attempts") == 1
    assert "private provider detail" not in caplog.text


def test_tts_failure_also_has_only_one_retry(clock, bot, monkeypatch, caplog):
    c = cfg()
    add(c, clock)
    def fail(*args):
        raise RuntimeError("no audio")
    monkeypatch.setattr(D, "audio", fail)
    D.tick(c, bot, clock + 3600)
    D.tick(c, bot, clock + 3630)
    D.tick(c, bot, clock + 3660)
    assert not bot.calls
    assert caplog.text.count("dropped after two failed attempts") == 1


def test_successful_retry_and_home_general_voice_caption(clock, bot, audio, monkeypatch):
    c = cfg()
    c.chat_id = -100555
    add(c, clock)
    original = bot.upload
    def reject(*args):
        raise TelegramError("not accepted", 429, api_rejected=True)
    monkeypatch.setattr(bot, "upload", reject)
    assert not D.tick(c, bot, clock + 3600)
    monkeypatch.setattr(bot, "upload", original)
    assert D.tick(c, bot, clock + 3630)
    method, fields = bot.calls[-1]
    assert method == "sendVoice" and fields["chat_id"] == str(c.home_chat)
    assert fields["caption"] == TEXT and "message_thread_id" not in fields
    assert not D.tick(c, bot, clock + 3660)


def test_crash_around_send_is_not_replayed(clock, bot, audio, monkeypatch):
    c = cfg()
    add(c, clock)
    def crash(*args):
        raise SystemExit()
    monkeypatch.setattr(bot, "upload", crash)
    with pytest.raises(SystemExit):
        D.tick(c, bot, clock + 3600)
    importlib.reload(D)
    monkeypatch.setattr(D, "audio", lambda c, text: audio.append(text) or b"OggS-test")
    assert not D.tick(c, bot, clock + 3630)
    assert not list((D.folder() / "pending").glob("*.json"))


def test_only_one_digest_in_flight(clock, bot, monkeypatch):
    c = cfg()
    add(c, clock)
    entered, release = threading.Event(), threading.Event()
    def make_audio(*args):
        entered.set()
        assert release.wait(5)
        return b"OggS-test"
    monkeypatch.setattr(D, "audio", make_audio)
    thread = threading.Thread(target=lambda: D.tick(c, bot, clock + 3600))
    thread.start()
    try:
        assert entered.wait(5)
        assert not D.tick(c, bot, clock + 3600)
    finally:
        release.set()
        thread.join(5)
    assert sum(method == "sendVoice" for method, _ in bot.calls) == 1


def test_spool_and_voice_are_private_with_redaction_off(clock, bot, audio, monkeypatch):
    c = cfg()
    c.redact_outgoing = False
    email = "ownerlocal@example.com"
    account = "sample-account-code"
    token = "gh" + "p_" + "AbC91z" * 8
    line = f"מסלול: הבדיקות עברו עבור ownerlocal. account_id={account} {token}"
    monkeypatch.setattr(radio_talk, "brief_line", lambda *a, **k: line)
    record = radio_talk.on_reply(c, email, "מסלול", f"Summary: {email} account_id={account} finished the job.",
                                now=clock, radio_on=lambda: True, mark={"sent_at": {"nested": email}})
    assert record is not None
    for path in list((radio.folder() / "replies").glob("*.json")) + list((D.folder() / "pending").glob("*.json")):
        raw = path.read_text()
        assert all(secret not in raw for secret in (email, "ownerlocal", account, token))
        assert path.stat().st_mode & 0o777 == 0o600
    # The live station consumes its own spool before the digest's next round.
    for path in (radio.folder() / "replies").glob("*.json"):
        path.unlink()
    assert D.tick(c, bot, clock + 3600)
    caption = bot.calls[-1][1]["caption"]
    assert caption == audio[0]
    assert all(secret not in caption for secret in (email, "ownerlocal", account, token))


@pytest.mark.skipif(__import__("shutil").which("ff" "mpeg") is None, reason="needs the audio encoder binary (not on CI)")
def test_radio_pcm_uses_existing_voice_and_encodes_real_opus(clock, monkeypatch):
    called = []
    def speech(text, c, settings, cache, usage):
        called.append((text, cache, usage.path))
        return bytes(radio_voice.RATE * 2), "say"
    monkeypatch.setattr(radio_voice, "speak", speech)
    payload = D.audio(cfg(), TEXT)
    assert payload.startswith(b"OggS") and b"OpusHead" in payload
    assert called[0][0] == TEXT and called[0][2] == radio.folder() / "tts-usage.json"


def test_cockpit_round_trip_and_language_keys():
    assert panel_settings.read()["voice_digest"] == "important"
    for value in D.LEVELS:
        panel_settings.write("voice_digest", value)
        assert panel_settings.read()["voice_digest"] == value
        assert config.load().voice_digest == value
        for lang in ("he", "en"):
            assert panel_page.STRINGS[lang]["digest_" + value]
    for value in (True, 1, "daily", None):
        with pytest.raises(ValueError):
            panel_settings.write("voice_digest", value)
    for lang in ("he", "en"):
        assert panel_page.STRINGS[lang]["set_voice_digest"]
        assert panel_page.STRINGS[lang]["hint_voice_digest"]
    assert 'setSetting("voice_digest",v,d)' in panel_page.PAGE
    for value in ("bad", False, []):
        with pytest.raises(config.ConfigError):
            config.from_dict({"voice_digest": value})
    with pytest.raises(config.ConfigError):
        config.from_dict({"voice_digest_quiet_windows": [{}]})


def test_overflow_waits_for_the_next_interval(clock, bot, audio):
    c = cfg("hourly")
    text = "מסלול: " + "המסך מוכן והבדיקות עברו. " * 20
    add(c, clock, "one", text=text)
    add(c, clock, "two", text=text)
    assert D.tick(c, bot, clock + 3600)
    assert len(audio[0]) <= D.MAX_TEXT
    assert len(list((D.folder() / "pending").glob("*.json"))) == 1
    assert not D.tick(c, bot, clock + 3630)
    assert D.tick(c, bot, clock + 7200)
    assert len(audio) == 2


def test_quiet_window_beginning_during_tts_stops_send(clock, bot, audio, monkeypatch):
    now = dt.datetime(2026, 11, 9, 22, 59, 59).timestamp()
    c = cfg()
    add(c, now - 3600)
    instants = iter([0.0, 2.0])
    monkeypatch.setattr(D.time, "monotonic", lambda: next(instants))
    assert not D.tick(c, bot, now)
    assert audio and not bot.calls
    state = config.read_map(D.folder() / "state.json")
    assert state["pending"]["attempts"] == 0


def test_restart_after_success_before_input_cleanup_does_not_repeat(clock, bot, audio, monkeypatch):
    c = cfg()
    add(c, clock)
    cleanup = D._cleanup
    calls = []
    def crash_after_success(state):
        calls.append(1)
        if state.get("done"):
            raise SystemExit()
        return cleanup(state)
    monkeypatch.setattr(D, "_cleanup", crash_after_success)
    with pytest.raises(SystemExit):
        D.tick(c, bot, clock + 3600)
    monkeypatch.setattr(D, "_cleanup", cleanup)
    assert not D.tick(c, bot, clock + 3630)
    assert sum(method == "sendVoice" for method, _ in bot.calls) == 1
    assert not list((D.folder() / "pending").glob("*.json"))


def test_changed_cadence_is_respected_after_generation(clock, bot, monkeypatch):
    c = cfg("hourly")
    add(c, clock)
    def audio_then_change(*args):
        config.save_raw({"bot_token": GOOD_TOKEN, "owner_chat_id": 7, "voice_digest": "every_3_hours"})
        return b"OggS-test"
    monkeypatch.setattr(D, "audio", audio_then_change)
    assert not D.tick(c, bot, clock + 3600)
    assert not D.tick(c, bot, clock + 3630)
    assert D.tick(c, bot, clock + 10800)
    assert len(bot.calls) == 1


def test_new_files_pass_public_hygiene():
    from test_public_hygiene import KINDS, ROOT, check
    for name in ("src/ctb/radio_privacy.py", "src/ctb/radio_suitability.py", "src/ctb/voice_digest.py", "tests/test_voice_digest.py"):
        lines = (ROOT / name).read_text().splitlines()
        for kind in KINDS:
            for i, line in enumerate(lines):
                assert not check(kind, line, lines[i + 1] if i + 1 < len(lines) else "", name), (name, kind, i + 1)


def test_privacy_in_nested_metadata_and_encoded_source():
    from ctb.radio_privacy import scrub
    value = {"name": "ownerlocal", "say": "ownerlocal סיים בהצלחה.",
             "metadata": {"account_id": 123456789, "token": "opaque", "nested": ["ownerlocal"]}}
    clean = scrub(value, "ownerlocal%40example.com")
    assert "ownerlocal" not in str(clean)
    assert "123456789" not in str(clean) and "opaque" not in str(clean)


@pytest.mark.parametrize("label", ["account ID abc123", "user-id: abc123", '"chat_id": "abc123"',
                                  "token q!9", "api key is q!9", "password=q!9"])
def test_privacy_learns_short_labelled_values_before_shortening(clock, bot, audio, monkeypatch, label):
    c = cfg()
    c.redact_outgoing = False
    private = "abc123" if "abc123" in label else "q!9"
    line = f"מסלול: הבדיקות עברו עבור {private}. אפשר להמשיך לשלב הבא."
    monkeypatch.setattr(radio_talk, "brief_line", lambda *a, **k: line)
    record = radio_talk.on_reply(c, "session-test", "מסלול", f"Summary: completed the work; {label}",
                                now=clock, radio_on=lambda: True)
    assert record is not None and private not in str(record)
    for path in (radio.folder() / "replies").glob("*.json"):
        assert private not in path.read_text()
    for path in (D.folder() / "pending").glob("*.json"):
        assert private not in path.read_text()
    assert D.tick(c, bot, clock + 3600)
    assert private not in audio[0] and private not in bot.calls[-1][1]["caption"]


def test_structured_private_values_scrub_unlabelled_references_and_keys():
    from ctb.radio_privacy import scrub
    private = {"metadata": {"account_id": "abc123", "access_token": "q!9", "user_id": 42}}
    clean = scrub({"text": "abc123 q!9 42 142", "q!9": "q!9"}, private)
    assert clean["text"] == "חשבון חשבון חשבון 142"
    assert all(value not in str(clean) for value in ("abc123", "q!9"))
    assert scrub(clean) == clean


@pytest.mark.timeout(3)
def test_privacy_label_scan_handles_long_ordinary_underscored_names():
    from ctb.radio_privacy import scrub
    label = "sample_" * 100 + "value"
    assert scrub({label: "הבדיקות הסתיימו בהצלחה."}) == {label: "הבדיקות הסתיימו בהצלחה."}


def test_loaded_config_secrets_scrub_editor_spool_and_legacy_digest(clock, bot, audio, monkeypatch):
    from ctb.radio_privacy import configured_identifiers
    c = cfg()
    c.redact_outgoing = False
    c._cached_token = "q!9"
    c.gemini_vertex_account = "abc123"
    seen = []
    def edit(prompt):
        seen.append(prompt)
        return f"<<<הבדיקה עברה עבור {c._cached_token} וחשבון {c.gemini_vertex_account}.>>>"
    record = radio_talk.on_reply(c, "test-session", "מסלול", "Summary: q!9 abc123 completed the test.",
                                ask=edit, now=clock, radio_on=lambda: True)
    assert record is not None
    assert c._cached_token in configured_identifiers(c)
    assert all(secret not in str(seen) + str(record) for secret in ("q!9", "abc123"))
    # A pre-existing pending batch must pass the same boundary again on delivery.
    config.write_private(D.folder() / "state.json", json.dumps({"pending": {
        "text": "מסלול: הבדיקה עבור q!9 abc123 הסתיימה בהצלחה.", "ids": [],
        "attempts": 0, "important": True, "level": "important"}}))
    assert D.tick(c, bot, clock + 3600)
    assert all(secret not in audio[0] + bot.calls[-1][1]["caption"] for secret in ("q!9", "abc123"))


@pytest.mark.parametrize("failure", [TimeoutError("accepted but response lost"), RuntimeError("connection lost"),
                                    TelegramError("timeout"), TelegramError("bad gateway", 502, api_rejected=True),
                                    TelegramError("unconfirmed rejection", 400), None, False])
def test_uncertain_send_is_consumed_without_replay_after_restart(clock, bot, audio, monkeypatch, failure, caplog):
    c = cfg()
    add(c, clock)
    accepted = []
    def accept_then_lose_response(*args):
        accepted.append(args)
        if isinstance(failure, Exception):
            raise failure
        return failure
    monkeypatch.setattr(bot, "upload", accept_then_lose_response)
    assert not D.tick(c, bot, clock + 3600)
    importlib.reload(D)
    monkeypatch.setattr(D, "audio", lambda c, text: audio.append(text) or b"OggS-test")
    assert not D.tick(c, bot, clock + 3630)
    assert len(accepted) == 1 and len(audio) == 1
    assert "pending" not in config.read_map(D.folder() / "state.json")
    assert not list((D.folder() / "pending").glob("*.json"))
    assert "delivery uncertain" in caplog.text and "response lost" not in caplog.text


@pytest.mark.parametrize("reason", ["closed", "budget"])
def test_shared_flood_gate_defers_batch_without_spending_attempt(clock, bot, audio, monkeypatch, reason):
    from ctb import flood
    c = cfg()
    chat = str(c.home_chat)
    if reason == "closed":
        flood.record_429(chat, 60)
    else:
        for _ in range(flood.OPTIONAL_PER_MIN):
            assert flood.allow_optional(chat)
    add(c, clock)
    for offset in (3600, 3630, 3660):
        assert not D.tick(c, bot, clock + offset)
        state = config.read_map(D.folder() / "state.json")
        assert state["pending"]["attempts"] == 0
        assert len(list((D.folder() / "pending").glob("*.json"))) == 1
    assert not bot.calls
    # Opening the same shared gate resumes the saved batch, not a new delivery.
    (config.state_dir() / flood.STATE_FILE).unlink()
    active = []
    original = bot.upload
    def upload(*args):
        active.append(flood.optional_active())
        return original(*args)
    monkeypatch.setattr(bot, "upload", upload)
    assert D.tick(c, bot, clock + 3690)
    assert active == [True] and len(bot.calls) == 1


@pytest.mark.parametrize("fallback", [False, True])
def test_unsuitable_editor_or_plain_fallback_never_spools(clock, monkeypatch, fallback):
    from ctb import radio_suitability as S
    monkeypatch.setattr(S, "SHARED_CHECKER", S.SHARED_CHECKER.with_name("missing-checker.py"))
    source = "Summary: עדכון מהצוות: מעשה טבח אירע במקום והבדיקה הסתיימה."
    out = "" if fallback else "<<<עדכון מהצוות: מעשה טבח אירע במקום והבדיקה הסתיימה.>>>"
    assert radio_talk.brief_line(cfg(), "מסלול", source, ask=lambda _: out) == ""
    assert radio_talk.on_reply(cfg(), "test-session", "מסלול", source, ask=lambda _: out,
                               radio_on=lambda: True, now=clock) is None
    assert not list((radio.folder() / "replies").glob("*.json"))
    assert not list((D.folder() / "pending").glob("*.json"))


@pytest.mark.parametrize("legacy", [False, True])
def test_unsuitable_final_digest_never_reaches_audio_or_send(clock, bot, audio, monkeypatch, legacy):
    from ctb import radio_suitability as S
    monkeypatch.setattr(S, "SHARED_CHECKER", S.SHARED_CHECKER.with_name("missing-checker.py"))
    c = cfg()
    text = "מסלול: תיאור מעשה טבח שאינו מתאים לילדים ולכן יש לדלג עליו."
    add(c, clock, text=text)
    if legacy:
        config.write_private(D.folder() / "state.json", json.dumps({"pending": {
            "ids": ["one.json"], "text": text, "attempts": 0, "level": "important", "important": True}}))
    assert not D.tick(c, bot, clock + 3600)
    assert not D.tick(c, bot, clock + 3630)
    assert not audio and not bot.calls
    assert not list((D.folder() / "pending").glob("*.json"))


def test_shared_suitability_checker_is_called_offline_with_private_input(tmp_path, monkeypatch):
    from ctb import radio_suitability as S
    script = tmp_path / "check.py"
    script.write_text("import pathlib, sys\np = pathlib.Path(sys.argv[1])\n"
                      "assert p.stat().st_mode & 0o777 == 0o600\n"
                      "sys.exit(1 if 'shared-only-rejection' in p.read_text() else 0)\n")
    monkeypatch.setattr(S, "SHARED_CHECKER", script)
    assert S.suitable(TEXT)
    assert not S.suitable("shared-only-rejection")  # absent from the fallback list
    script.write_text("raise RuntimeError('checker unavailable')")
    assert not S.suitable(TEXT)  # a broken installed checker fails closed


def test_shared_checker_rejection_also_blocks_safe_looking_digest(clock, bot, audio, tmp_path, monkeypatch):
    from ctb import radio_suitability as S
    script = tmp_path / "check.py"
    script.write_text("raise SystemExit(1)\n")
    monkeypatch.setattr(S, "SHARED_CHECKER", script)
    c = cfg()
    add(c, clock)
    assert not D.tick(c, bot, clock + 3600)
    assert not audio and not bot.calls
    assert "pending" not in config.read_map(D.folder() / "state.json")


def test_suitability_fallback_when_checker_cannot_be_called(monkeypatch, tmp_path):
    from ctb import radio_suitability as S
    script = tmp_path / "check.py"
    script.touch()
    monkeypatch.setattr(S, "SHARED_CHECKER", script)
    def unavailable(*args, **kwargs):
        raise OSError("python unavailable")
    monkeypatch.setattr(S.subprocess, "run", unavailable)
    assert S.suitable(TEXT)
    assert not S.suitable("וַיִּשְׁכַּב אֶת־בִּלְהָה")
    assert not S.suitable("תיאור מעשה טבח")
