"""Automatic cards use only fake transcripts, fixture configs, and offline translator fakes."""
import json
import re
import threading
import time

import pytest

from ctb import auto_cards as A, auto_progress as P, cards, config, hooks, panel_page, panel_settings

S = "a1b2c3d4-0000-4000-8000-000000000001"
SUMMARY = {"title": "הכנת הכרטיסים", "steps": ["סקירת העבודה", "הכנת השינוי", "בדיקת התוצאה"],
           "at": 1, "percent": 40}


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path))
    for module in (config, cards, hooks):
        monkeypatch.setattr(module, "state_dir", lambda: tmp_path)
    P._cache.clear()
    monkeypatch.setattr(A, "watcher", A.Watcher())
    monkeypatch.setattr(A, "_scan_at", None)
    return tmp_path


def use(name, **inp):
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "name": name, "id": "tool", "input": inp}]}}


def write(path, *events):
    with path.open("a") as f:
        for event in events:
            f.write(json.dumps(event) + "\n")


def row(path, state="working", bg=0, session=S):
    return {"session": session, "path": str(path), "state": state, "bg": bg,
            "title": "כרטיסי עבודה", "chat": -100, "thread": 7}


def test_auto_cards_source_is_short_scrubbed_incremental_and_bounded(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    write(path, use("Bash", description="בדיקת הקוד", command="DO_NOT_COPY_COMMAND"),
          use("Read", file_path="DO_NOT_COPY_PATH"),
          {"type": "user", "message": {"role": "user", "content": [
              {"type": "tool_result", "content": "DO_NOT_COPY_OUTPUT"}]}},
          {"type": "assistant", "message": {"role": "assistant", "content": [
              {"type": "thinking", "thinking": "DO_NOT_COPY_THINKING"},
              {"type": "text", "text": "הבדיקה מתקדמת"},
              {"type": "text", "text": "password = DO_NOT_COPY_FILE_CONTENT"},
              {"type": "text", "text": "DO_NOT_COPY_QUOTED_ANSWER"},
              {"type": "text", "text": "DO_NOT_COPY_PARAGRAPH\ncontent"},
              {"type": "text", "text": "DO_NOT_COPY_LONG " * 50}]}})
    secret = "sk_" + "live_abcdefghijklmnop123456789"
    write(path, use("Bash", description=f"Inspect /Users/example/private.txt at https://example.invalid/x {secret}"),
          use("TodoWrite", todos=[{"content": "בדיקת המצב", "status": "pending", "extra": "DO_NOT_COPY_TASK_DETAIL"}]))
    found = P.activity_snapshot(str(path))
    text = json.dumps(found, ensure_ascii=False)
    for leak in ("DO_NOT_COPY", "example", "private.txt", secret):
        assert leak not in text
    assert {"kind": "description", "text": "בדיקת הקוד"} in found["activities"]
    assert "הבדיקה מתקדמת" not in text
    assert not any(a.get("kind") in ("status", "tool") for a in found["activities"])
    with monkeypatch.context() as guard:
        guard.setattr("builtins.open", lambda *a, **kw: pytest.fail("unchanged transcript was reopened"))
        assert P.activity_snapshot(str(path)) == found
        assert P.task_list(str(path)) == found["tasks"]
    write(path, *(use("Bash", description="Read") for _ in range(P.MAX_ACTIVITIES + 5)))
    latest = P.activity_snapshot(str(path))
    assert len(latest["activities"]) == P.MAX_ACTIVITIES
    assert latest["revision"] > found["revision"]


def test_auto_cards_fallback_without_tasks_updates_same_card(tmp_path):
    path = tmp_path / "events.jsonl"
    write(path, use("Bash", description="Read"))
    watcher = A.Watcher()
    watcher.sync([row(path)], 1000)
    first = cards.open_cards()[0]
    assert 3 <= len(first["steps"]) <= 6 and first["automatic"]
    write(path, use("Bash", description="Run tests", command="ignored"))
    watcher.sync([row(path)], 1030)
    rec = cards.open_cards()[0]
    assert rec["id"] == first["id"] and "בדיקת התוצאה" in rec["steps"] and rec["updated"] == 1030
    watcher.sync([row(path)], 1040)
    assert cards.open_cards()[0]["updated"] == 1030  # polling is not progress
    write(path, use("Bash", description="Run tests again"))
    watcher.sync([row(path)], 1050)
    assert cards.open_cards()[0]["updated"] == 1050  # real activity, even if grouped steps are unchanged
    watcher.sync([row(path, "idle")], 1060)
    assert not cards.open_cards()


def test_auto_cards_task_fallback_preserves_status_and_hebrew_names():
    snap = {"tasks": [{"content": "Read code", "status": "completed"},
                      {"content": "תיקון React", "status": "in_progress"},
                      {"content": "Run tests", "status": "pending"}], "activities": [], "revision": 1}
    summary = A.fallback({"title": "עבודה עם React"}, snap)
    rec = cards.sync_auto(S, -100, 7, "unused", [], summary=summary, now=1000)
    assert rec["at"] == 1 and rec["percent"] == 33
    assert rec["title"] == "עבודה עם \u2068React\u2069"
    assert rec["steps"][1] == "תיקון \u2068React\u2069"
    for value in [rec["title"], *rec["steps"]]:
        assert re.search("[א-ת]", value)
        assert not re.search("[A-Za-z]", re.sub("\u2068.*?\u2069", "", value))




def test_auto_cards_background_continues_until_background_ends(tmp_path):
    path = tmp_path / "events.jsonl"
    watcher = A.Watcher()
    watcher.sync([row(path, "idle", bg=1)], 1000)
    assert cards.open_cards()[0]["bg"] == 1
    watcher.sync([row(path, "ended", bg=1)], 1001)
    assert not cards.open_cards()



def test_auto_cards_use_task_list_nudge_only_while_fresh_and_enabled(tmp_path):
    hooks.turn_started(S, now=1000, background=1)
    cards.sync_auto(S, -100, 7, "כרטיס", [], summary=SUMMARY, now=1000)
    assert hooks.progress_nudge(S, now=1001) == hooks.TASK_LIST_NUDGE
    assert hooks.progress_nudge(S, now=1599) == ""
    assert hooks.progress_nudge(S, now=1601) == hooks.PROGRESS_NUDGE
    cards.sync_auto(S, -100, 7, "כרטיס", [], summary=SUMMARY, refresh=True, now=1601)
    assert hooks.progress_nudge(S, now=1602) == ""  # the preceding full nudge still rate limits reminders
    A.set_enabled(False)
    assert hooks.progress_nudge(S, now=2202) == hooks.PROGRESS_NUDGE


def test_auto_cards_fresh_auto_suppresses_stale_manual_update():
    hooks.turn_started(S, now=1000, background=1)
    own = cards.publish(S, -100, 7, "progress", "כרטיס ידני", percent=10, now=1000)
    # Legacy state from competing processes: still suppress before asking to update the old plan.
    with cards._locked() as path:
        data = cards._load(path)
        data["b" * 16] = dict(own, id="b" * 16, automatic=True, updated=1600)
        cards._save(path, data)
    assert hooks.progress_nudge(S, now=1601) == hooks.TASK_LIST_NUDGE
    assert hooks.progress_nudge(S, now=2201) == hooks.PROGRESS_UPDATE.format(id=own["id"])


def test_auto_cards_setting_controls_deterministic_cards():
    assert A.enabled()
    for name in ("auto_cards",):
        assert panel_settings.allowed(name, True) and not panel_settings.allowed(name, "yes")
        assert "set_" + name in panel_page.STRINGS["he"]
        assert '"' + name + '"' in panel_page.PAGE
        panel_settings.write(name, True)
        assert panel_settings.SETTINGS[name][0]() is True
        panel_settings.write(name, False)
        assert panel_settings.SETTINGS[name][0]() is False


def test_auto_cards_heartbeat_start_is_async_and_rate_limited(monkeypatch):
    calls = []
    class Thread:
        def __init__(self, **kw):
            calls.append(kw)
        def start(self):
            pass  # do not run the collector on the caller
    monkeypatch.setattr(A.threading, "Thread", Thread)
    monkeypatch.setattr(A, "_scan_guard", threading.Lock())
    A.start(config.Config(), 1000)
    A.start(config.Config(), 1001)
    A.start(config.Config(), 2000)  # in flight still: coalesced
    assert len(calls) == 1 and calls[0]["daemon"]
    A._scan_guard.release()







def test_auto_cards_metadata_cache_expires_without_transcript_changes(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text("{}\n")
    calls = []
    read = lambda p: calls.append(p) or len(calls)
    assert P.cached(str(path), "bg", read, now=1000, max_age=600) == 1
    assert P.cached(str(path), "bg", read, now=1599, max_age=600) == 1
    assert P.cached(str(path), "bg", read, now=1600, max_age=600) == 2
    assert len([key for key in P._cache if key.startswith("bg\0")]) == 1


def test_auto_cards_oversized_partial_records_are_skipped_and_next_record_survives(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    monkeypatch.setattr(P, "CHUNK", 1024)
    path.write_bytes(b'{"type":"assistant","message":{"content":"' + b"x" * (P.MAX_LINE + 1000))
    assert P.activity_snapshot(str(path))["activities"] == []
    with path.open("ab") as f:
        f.write(b'"}}\n')
    write(path, use("Bash", description="Read"))
    assert P.activity_snapshot(str(path))["activities"] == [{"kind": "description", "text": "Read"}]


def test_auto_cards_todowrite_hook_obeys_disabled_setting(monkeypatch):
    from ctb import decisions
    monkeypatch.setattr(hooks, "load_config", lambda: config.Config())
    monkeypatch.setattr(decisions, "_place_of", lambda *a: (-100, 7))
    monkeypatch.setattr(hooks, "typing", lambda *a: None)
    monkeypatch.setattr(hooks, "append_progress", lambda *a: None)
    A.set_enabled(False)
    hooks.hook_post(json.dumps({"session_id": S, "tool_name": "TodoWrite", "tool_input": {"todos": [
        {"content": "בדיקת העבודה", "status": "in_progress"}]}}))
    assert not cards.open_cards()


def test_auto_cards_heartbeat_runs_local_watcher_with_mirroring_off(monkeypatch):
    from ctb import heartbeat, policy
    calls = []
    monkeypatch.setattr(policy, "on", lambda: False)
    monkeypatch.setattr(heartbeat.Bot, "from_config", lambda cfg: object())
    monkeypatch.setattr(A, "start", lambda cfg, now: calls.append(now))
    assert heartbeat.run(config.Config(bot_token="1:x", owner_chat_id=7), once=True) == 0
    assert len(calls) == 1


def test_auto_cards_todowrite_fast_path_uses_hebrew_three_steps_and_preserves_agent():
    todos = [{"content": "Read code", "status": "in_progress"}]
    rec = cards.sync_todos(S, -100, 7, todos, "Work plan", now=1000)
    assert rec["title"] == "עבודה על \u2068Work\u2069" and len(rec["steps"]) == 3
    assert all(re.search("[א-ת]", s) for s in rec["steps"])
    own = cards.publish(S, -100, 7, "progress", "כרטיס ידני", percent=10, now=1001)
    assert cards.sync_todos(S, -100, 7, todos, "Work plan", now=1002) is None
    assert [c["id"] for c in cards.open_cards()] == [own["id"]]
    A.set_enabled(False)
    assert cards.sync_todos("other", -100, 8, todos, "Work plan", now=1003) is None




def test_auto_cards_panel_keeps_background_card_outside_visible_session_slice(monkeypatch):
    from ctb import panel, sessions_menu, status
    rec = cards.sync_auto(S, -100, 7, "עבודה ברקע", [], summary=SUMMARY, bg=2, now=1000)
    monkeypatch.setattr(status, "transcript_of", lambda session: None)
    entry = sessions_menu.Entry(S, "עבודה ברקע", 7, 0, "idle", False, -100)
    panel._auto_cards(config.Config(), [entry], {}, {}, 1001)
    assert [c["id"] for c in cards.open_cards()] == [rec["id"]]
    assert cards.open_cards()[0]["bg"] == 2


def test_a_same_tick_update_still_invalidates_a_stale_background_result():
    from ctb import cards
    fp1, fp2 = "a" * 64, "b" * 64
    first = cards.sync_auto("s-tick", 1, 2, "כותרת", [{"content": "שלב", "status": "in_progress"}],
                            now=1000.0, activity_fingerprint=fp1)
    started = first["updated"]
    second = cards.sync_auto("s-tick", 1, 2, "כותרת", [{"content": "שלב אחר", "status": "in_progress"}],
                             now=started, activity_fingerprint=fp2)  # same tick (or a clock rollback)
    assert second["updated"] > started
    stale = cards.sync_auto("s-tick", 1, 2, "סיכום ישן", [{"content": "שלב", "status": "in_progress"}],
                            now=started, expected_id=first["id"], expected_updated=started,
                            activity_fingerprint=fp1)
    assert stale is None


def test_english_title_uses_offline_translation_in_watcher(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    write(path, use("Bash", description="Read"))
    calls = []

    def translate(text, cfg, fast=False):
        calls.append((text, tuple(cfg.engines), cfg.target_language, fast))
        return "תיקון גשר HarnessGram"

    cfg = config.Config(engines=["gemini", "nllb", "google", "mlx"])
    watcher = A.Watcher(translate)
    watcher.sync([row(path) | {"title": "Fix HarnessGram bridge"}], 1000, cfg=cfg)
    assert calls == [("Fix HarnessGram bridge", ("nllb",), "he", True)]
    assert cards.open_cards()[0]["title"] == "תיקון גשר \u2068HarnessGram\u2069"


def test_translation_runs_outside_watcher_lock_and_stale_result_is_discarded(tmp_path):
    path = tmp_path / "events.jsonl"
    write(path, use("Bash", description="Read"))
    entered, release = threading.Event(), threading.Event()

    def translate(*_args, **_kwargs):
        entered.set()
        release.wait(2)
        return "תיקון גשר HarnessGram"

    watcher = A.Watcher(translate)
    english = row(path) | {"title": "Fix HarnessGram bridge"}
    worker = threading.Thread(target=lambda: watcher.sync(
        [english], 1000, cfg=config.Config(engines=["nllb"])))
    worker.start()
    assert entered.wait(1)
    started = time.monotonic()
    watcher.sync([english], 1001, initial_only=True, cfg=config.Config(engines=["nllb"]))
    assert time.monotonic() - started < 0.5
    write(path, use("Bash", description="Run tests"))
    watcher.sync([english], 1002, cfg=config.Config(engines=["nllb"]))
    release.set()
    worker.join(1)
    assert cards.open_cards()[0]["title"] == "עבודה על \u2068HarnessGram\u2069"


def test_title_translation_is_cached_across_activity_changes(tmp_path):
    path = tmp_path / "events.jsonl"
    write(path, use("Bash", description="Read"))
    calls = []
    watcher = A.Watcher(lambda text, *_args, **_kwargs: calls.append(text) or "תיקון HarnessGram")
    english = row(path) | {"title": "Fix HarnessGram"}
    watcher.sync([english], 1000, cfg=config.Config(engines=["nllb"]))
    write(path, use("Bash", description="Run tests"))
    watcher.sync([english], 1030, cfg=config.Config(engines=["nllb"]))
    assert calls == ["Fix HarnessGram"]
    assert cards.open_cards()[0]["title"] == "תיקון \u2068HarnessGram\u2069"


def test_heartbeat_translates_card_seeded_by_panel_refresh(tmp_path):
    path = tmp_path / "events.jsonl"
    write(path, use("Bash", description="Read"))
    watcher = A.Watcher(lambda *_args, **_kwargs: "תיקון גשר HarnessGram")
    english = row(path) | {"title": "Fix HarnessGram bridge"}
    watcher.sync([english], 1000, initial_only=True, cfg=config.Config(engines=["nllb"]))
    assert cards.open_cards()[0]["title"] == "עבודה על \u2068HarnessGram\u2069"
    watcher.sync([english], 1001, cfg=config.Config(engines=["nllb"]))
    assert cards.open_cards()[0]["title"] == "תיקון גשר \u2068HarnessGram\u2069"


def test_english_title_timeout_falls_back_with_isolated_product(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    write(path, use("Bash", description="Read"))
    entered, release = threading.Event(), threading.Event()

    def slow(*_args, **_kwargs):
        entered.set()
        release.wait(2)
        return "תרגום מאוחר"

    monkeypatch.setattr(A, "TITLE_TRANSLATION_TIMEOUT", 0.01)
    watcher = A.Watcher(slow)
    try:
        watcher.sync([row(path) | {"title": "Fix HarnessGram bridge"}], 1000,
                     cfg=config.Config(engines=["nllb", "gemini"]))
        assert entered.is_set()
        assert cards.open_cards()[0]["title"] == "עבודה על \u2068HarnessGram\u2069"
        assert not watcher.sessions[S]["title_ready"]
    finally:
        release.set()


def test_late_title_translation_is_cached(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    write(path, use("Bash", description="Read"))
    entered, release = threading.Event(), threading.Event()

    def slow(*_args, **_kwargs):
        entered.set()
        release.wait(2)
        return "תרגום מאוחר"

    monkeypatch.setattr(A, "TITLE_TRANSLATION_TIMEOUT", 0.01)
    watcher = A.Watcher(slow)
    title = "Fix HarnessGram bridge"
    watcher.sync([row(path) | {"title": title}], 1000, cfg=config.Config(engines=["nllb"]))
    assert entered.is_set() and title not in watcher.translations
    release.set()
    for _ in range(100):
        if watcher.translations.get(title) == "תרגום מאוחר":
            break
        time.sleep(0.01)
    assert watcher.translations[title] == "תרגום מאוחר"


def test_mlx_is_never_used_for_card_titles():
    calls = []
    result = A._translated_title("Fix bridge", config.Config(engines=["mlx"]),
                                 lambda *_args, **_kwargs: calls.append(1) or "תיקון גשר")
    assert result is None and calls == []


def test_busy_translation_is_retried_without_new_activity(tmp_path):
    path = tmp_path / "events.jsonl"
    write(path, use("Bash", description="Read"))
    calls = []
    watcher = A.Watcher(lambda *_args, **_kwargs: calls.append(1) or "תיקון HarnessGram")
    english = row(path) | {"title": "Fix HarnessGram"}
    assert A._translation_lock.acquire(blocking=False)
    try:
        watcher.sync([english], 1000, cfg=config.Config(engines=["nllb"]))
    finally:
        A._translation_lock.release()
    assert not watcher.sessions[S]["title_ready"] and calls == []
    watcher.sync([english], 1001, cfg=config.Config(engines=["nllb"]))
    assert watcher.sessions[S]["title_ready"] and calls == [1]
    assert cards.open_cards()[0]["title"] == "תיקון \u2068HarnessGram\u2069"


@pytest.mark.parametrize("value", [
    "Fix https://example.invalid/private",
    "Fix /Users/example/private.txt",
    "Fix person" + chr(64) + "example.invalid",
])
def test_title_fallback_does_not_use_scrub_placeholders_as_product_names(value):
    assert not any(word in A.title_fallback(value).casefold() for word in ("link", "path", "email"))


def test_old_model_setting_is_ignored_safely(tmp_path):
    (tmp_path / "auto_cards_model").write_text("1")
    assert "auto_cards_model" not in panel_settings.SETTINGS
    assert A.enabled()
