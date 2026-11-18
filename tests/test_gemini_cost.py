"""Gemini spend: routine text goes to the cheap routine model and only a reply's closing summary keeps the
owner-facing model; the same text is never sent twice at once; a step title is never lost from its cache (and so
asked again); every answered call is logged with its model and caller; paid speech stops at the day's cap."""
import io
import json
import subprocess
import threading
import time

import pytest

from conftest import GOOD_TOKEN
from ctb import dashboard, progress as P, radio, radio_voice as V, translate as T
from ctb.config import Config, state_dir

MAIN, LITE = "gemini-3.8-flash", "gemini-3.5-flash-lite"


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def ok(text, tokens=(10, 4)):
    return Resp(json.dumps({"candidates": [{"content": {"parts": [{"text": text}]}}],
                            "usageMetadata": {"promptTokenCount": tokens[0], "candidatesTokenCount": tokens[1]}}).encode())


def gem_cfg(**kw):
    kw.setdefault("gemini_model", MAIN)
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=7, gemini_vertex_project="p", gemini_token_command=["tok"],
                  target_language="he", translate=True, engines=["gemini"], **kw)


@pytest.fixture
def wire(monkeypatch):
    """A fake Vertex: records (model, request body) and answers in Hebrew. No network, no real credential."""
    seen = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, "c\n", ""))

    def urlopen(req, timeout=None):
        seen.append((req.full_url.rsplit("/", 1)[1].split(":")[0], json.loads(req.data)))
        return ok("שלום")

    monkeypatch.setattr(T.urllib.request, "urlopen", urlopen)
    return seen


@pytest.mark.parametrize("kw,owner,want", [
    ({"gemini_fallback_model": LITE}, False, [LITE, MAIN]),  # routine: the cheap model first
    ({"gemini_fallback_model": LITE}, True, [MAIN, LITE]),  # owner-facing: the configured model first
    ({"gemini_fallback_model": LITE, "gemini_routine_model": MAIN}, False, [MAIN, LITE]),  # opted out
    ({"gemini_routine_model": LITE}, False, [LITE, MAIN]),
    ({}, False, [MAIN]),  # nothing cheaper configured: unchanged
    ({}, True, [MAIN]),
])
def test_routine_text_starts_on_the_cheap_model(kw, owner, want):
    c = gem_cfg(**kw)
    if owner:
        with T.owner_facing():
            assert T._models(c) == want
    else:
        assert T._models(c) == want


def test_routine_call_goes_to_lite_without_the_thinking_level_and_owner_call_keeps_it(wire):
    c = gem_cfg(gemini_fallback_model=LITE, gemini_thinking="low")
    assert T.ask("hi", c) == "שלום"
    with T.owner_facing():
        assert T.gemini("hello", c) == "שלום"
    (m1, b1), (m2, b2) = wire
    assert m1 == LITE and "thinkingConfig" not in b1["generationConfig"]
    assert m2 == MAIN and b2["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "low"}


def caller_of_ask(c):
    return T.ask("hi", c)


def test_usage_log_names_the_model_and_the_caller_never_the_text(wire):
    c = gem_cfg(gemini_fallback_model=LITE)
    caller_of_ask(c)
    caller_of_ask(c)
    rows = [r.split() for r in (state_dir() / "gemini-usage.log").read_text().splitlines()]
    assert [r[1:6] for r in rows] == [["10", "4", "text", LITE, "test_gemini_cost.caller_of_ask"]] * 2
    assert rows[0][6] == rows[1][6] and len(rows[0][6]) == 10  # the same request twice has the same short hash
    assert "hi" not in (state_dir() / "gemini-usage.log").read_text()


def test_failures_name_the_caller(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, "c\n", ""))

    def urlopen(req, timeout=None):
        raise TimeoutError()

    monkeypatch.setattr(T.urllib.request, "urlopen", urlopen)
    caller_of_ask(gem_cfg())
    assert "src=test_gemini_cost.caller_of_ask" in (state_dir() / "gemini-failures.log").read_text()


def test_the_same_text_asked_at_once_is_translated_once(monkeypatch):
    calls = []

    def engines(text, cfg, fast):
        calls.append(text)
        time.sleep(0.3)
        return "הסיכום של הסוכן"

    monkeypatch.setattr(T, "_engines", engines)
    c = gem_cfg()
    out = []
    threads = [threading.Thread(target=lambda: out.append(T.translate_text("The agent summary.", c))) for _ in range(3)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert calls == ["The agent summary."] and out == ["הסיכום של הסוכן"] * 3


def test_a_hung_first_asker_never_blocks_the_second_for_good(monkeypatch):
    monkeypatch.setattr(T, "IN_FLIGHT_WAIT", 0.2)
    started, release = threading.Event(), threading.Event()
    calls = []

    def engines(text, cfg, fast):
        calls.append(text)
        if len(calls) == 1:
            started.set()
            release.wait(5)
        return "שלום"

    monkeypatch.setattr(T, "_engines", engines)
    c = gem_cfg()
    first = threading.Thread(target=lambda: T.translate_text("Hello there.", c))
    first.start()
    started.wait(5)
    with T.in_flight("x", wait=0.2):
        pass  # an unrelated key is never held up
    t0 = time.monotonic()
    assert T.translate_text("Hello there.", c) == "שלום"
    assert time.monotonic() - t0 < 2 and len(calls) == 2
    release.set()
    first.join()


def test_a_step_title_added_by_another_process_during_a_slow_call_is_kept(monkeypatch):
    path = state_dir() / P.HEADINGS_CACHE_FILE

    def ask(prompt, cfg, **k):
        # Another session's job stores its title while this call waits for Gemini.
        P._heading_store(path, "Run the tests", "מריץ את הבדיקות")
        return "קורא את הקובץ"

    monkeypatch.setattr(T, "ask", ask)
    c = gem_cfg()
    assert P.heading_he("Read the file", c) == "קורא את הקובץ"
    cache = json.loads(path.read_text(encoding="utf-8"))
    assert cache == {"Run the tests": "מריץ את הבדיקות", "Read the file": "קורא את הקובץ"}
    monkeypatch.setattr(T, "ask", lambda *a, **k: pytest.fail("a cached title is never asked again"))
    assert P.heading_he("Run the tests", c) == "מריץ את הבדיקות"


def test_the_heading_cache_keeps_the_newest_titles(monkeypatch):
    monkeypatch.setattr(P, "HEADINGS_KEEP", 3)
    path = state_dir() / P.HEADINGS_CACHE_FILE
    for n in range(5):
        P._heading_store(path, f"t{n}", f"כ{n}")
    assert list(json.loads(path.read_text(encoding="utf-8"))) == ["t2", "t3", "t4"]


def test_only_the_summary_is_translated_on_the_owner_facing_model():
    seen = []

    def routine(text):
        seen.append(("routine", T._owner_facing.get(), text))
        return "גוף ראשון\n\nגוף שני"

    def summary(text):
        with T.owner_facing():
            seen.append(("summary", T._owner_facing.get(), text))
            return "הכל עבר."

    reply = "First body paragraph.\n\nSecond body paragraph.\n\nEverything passed."
    out = T.translate_reply(reply, routine, "he", summary_fn=summary)
    assert out == "גוף ראשון\n\nגוף שני\n\nהכל עבר."
    assert sorted(seen) == [("routine", False, "First body paragraph.\n\nSecond body paragraph."),
                            ("summary", True, "Everything passed.")]


def test_a_summary_already_in_hebrew_or_alone_uses_the_owner_facing_call():
    calls = []
    fn = lambda text: calls.append(("body", text)) or "גוף"  # noqa: E731
    owner = lambda text: calls.append(("summary", text)) or "סיכום"  # noqa: E731
    assert T.translate_reply("A body paragraph here.\n\nהכל עבר כבר.", fn, "he", summary_fn=owner).endswith("הכל עבר כבר.")
    assert calls == [("body", "A body paragraph here.")]
    calls.clear()
    assert T.translate_reply("Only the summary here.", fn, "he", summary_fn=owner) == "סיכום"
    assert calls == [("summary", "Only the summary here.")]


def test_owner_facing_and_routine_translations_have_separate_cache_keys():
    c = gem_cfg()
    routine = T._cache_path("The same summary.", c)
    with T.owner_facing():
        owner = T._cache_path("The same summary.", c)
    assert routine != owner


def test_invalid_routine_translation_falls_back_to_the_main_model(wire, monkeypatch):
    def urlopen(req, timeout=None):
        model = req.full_url.rsplit("/", 1)[1].split(":")[0]
        wire.append((model, json.loads(req.data)))
        return ok("The tests pass and the batch is ready." if model == LITE else "הבדיקות עברו והאצווה מוכנה.")

    monkeypatch.setattr(T.urllib.request, "urlopen", urlopen)
    assert T.gemini("The tests pass and the batch is ready.", gem_cfg(gemini_routine_model=LITE)) == \
        "הבדיקות עברו והאצווה מוכנה."
    assert [model for model, _ in wire] == [LITE, MAIN]


def test_a_failed_summary_call_fails_the_reply_translation():
    def broken(text):
        raise T.TranslateError("no engine")

    with pytest.raises(T.TranslateError):
        T.translate_reply("Body text here.\n\nThe summary.", lambda text: "גוף", "he", summary_fn=broken)


def test_the_mirror_translates_the_summary_owner_facing(monkeypatch):
    from ctb import mirror as M
    seen = []
    monkeypatch.setattr(M, "translate_text",
                        lambda text, cfg, **k: seen.append((T._owner_facing.get(), text)) or "תרגום")

    class FakeMirror:
        dropped, last_error = False, None

        def send_reply(self, title, session, text, fn, footer, answer_to=None):
            fn("The body.")
            self.summary_fn("The summary.")

    monkeypatch.setattr(M, "build", lambda cfg: FakeMirror())
    monkeypatch.setattr(M, "record_outcome", lambda ok: None)
    monkeypatch.setattr(M, "refresh_card", lambda *a, **k: None)
    monkeypatch.setattr(M.usage, "footer", lambda cfg, transcript: "")
    monkeypatch.setattr(M.usage, "compact_footer", lambda cfg, transcript: "")
    monkeypatch.setattr("ctb.scheduled.suppressed", lambda *a: False)
    assert M.run_reply(gem_cfg(), "t", "s1", "The body.\n\nSummary: The summary.", True) == 0
    assert seen == [(False, "The body."), (True, "The summary.")]


def test_the_dashboard_prices_each_logged_model():
    log = state_dir() / "gemini-usage.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    now = 1_800_000_000
    log.write_text(f"{now} 1000000 0 text {MAIN} x.y abc\n{now} 1000000 0 text {LITE} x.y abc\n{now} 1000000 0\n")
    line = dashboard.gemini_cost_line(now=now, log_path=log, lang="en")
    assert "2.10" in line  # 1.50 + 0.30 + 0.30 (an old line without a model keeps the old price)


def test_paid_speech_stops_at_the_daily_cap_and_starts_again_the_next_day(tmp_path, monkeypatch):
    """A fake clock: the cap is a hard stop that survives a restart (a fresh ledger object) and resets at midnight."""
    clock = [time.mktime((2026, 11, 17, 9, 0, 0, 0, 0, -1))]
    localtime = time.localtime
    monkeypatch.setattr(V.time, "time", lambda: clock[0])
    monkeypatch.setattr(V.time, "localtime", lambda at=None: localtime(clock[0] if at is None else at))
    monkeypatch.setattr(V, "system_voice", lambda *a: b"free")
    monkeypatch.setattr(V, "NO_CEILING", set())
    sent = []

    def vertex(text, cfg, model, voice, style, max_seconds=0, **kw):
        sent.append(max_seconds)
        return b"\x01\x00" * V.RATE * 5, 5.0  # 5 billed seconds each, inside the reservation

    monkeypatch.setattr(V, "vertex", vertex)
    s = radio.settings(Config(radio={"voice_route": "cloud", "tts_daily_minutes": 1}))
    routes = []
    for n in range(20):
        _, route = V.speak(f"מבזק מספר {n}", Config(), s, tmp_path / "cache", V.paid_usage(tmp_path))
        routes.append(route)
    assert routes.count("vertex") == len(sent) and 1 < len(sent) <= 12
    assert routes[-5:] == ["say"] * 5 and V.paid_usage(tmp_path).today() <= 60  # a hard stop for the rest of the day
    clock[0] += 86400
    _, route = V.speak("מבזק של מחר", Config(), s, tmp_path / "cache", V.paid_usage(tmp_path))
    assert route == "vertex"
