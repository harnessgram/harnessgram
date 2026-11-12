"""Live Stop summaries are separate from the agent's authored plan and never create a card."""
import json
import time

import pytest

from ctb import cards, hooks, mirror, progress, topics, translate
from ctb.config import Config, state_dir
from test_hooks import ev, stop_data


@pytest.fixture(params=["inline", "worker"])
def stop(request, monkeypatch, tmp_path):
    cfg = Config(bot_token="t", owner_chat_id=1)
    monkeypatch.setattr(hooks, "spawn", lambda *a, **kw: None)
    monkeypatch.setattr(hooks, "load_config", lambda: cfg)
    monkeypatch.setattr(hooks, "drop_progress", lambda *a, **kw: None)
    monkeypatch.setattr(progress, "run_send_progress", lambda *a, **kw: 0)
    monkeypatch.setattr(topics, "set_state", lambda *a, **kw: None)
    monkeypatch.setattr(mirror, "run_reply", lambda *a, **kw: 0)

    def run(text):
        lines = [ev("user", "do it"), ev("assistant", text, "a1")]
        data = stop_data(tmp_path, lines)
        if request.param == "inline":
            return hooks.stop_output(data, lines, cfg)
        return hooks._finish_stop(data)
    return run


def own(session="sess-1", **kw):
    return cards.publish(session, 1, 7, "progress", "Build", detail="Agent's own detail",
                         steps=["Prepare", "Test"], at=0, now=100, **kw)


def test_stop_writes_the_final_summary_without_overwriting_the_plan(stop):
    rec = own()
    other = own("other-session")
    second = own()
    stop("Summary: Old summary.\nWork continues.\n**Summary:** Tests are running.")
    saved = {r["id"]: r for r in cards.open_cards()}
    for before in (rec, second):
        after = saved[before["id"]]
        assert after["live"] == "Tests are running."
        assert after["live_at"] > 100
        assert all(after[k] == before[k] for k in ("detail", "updated", "steps", "at", "percent"))
        assert cards.view(after, after["live_at"] + 123)["live_age"] == 123
    assert saved[other["id"]] == other


def test_stop_with_no_card_does_not_write_card_state(stop):
    stop("Summary: No card to update.")
    assert not (state_dir() / cards.STATE_FILE).exists()
    assert not (state_dir() / (cards.STATE_FILE + ".lock")).exists()


@pytest.mark.parametrize("kind", ["closed", "percent_done", "steps_done", "automatic", "choice"])
def test_stop_leaves_ineligible_cards_untouched(stop, kind):
    if kind == "automatic":
        cards.sync_auto("sess-1", 1, 7, "Auto", [], now=100)
    elif kind == "choice":
        cards.publish("sess-1", 1, 7, "choice", "Choose", options=["a", "b"], now=100)
    else:
        rec = own(percent=100 if kind == "percent_done" else 20)
        if kind == "closed":
            cards.close("sess-1", rec["id"], now=110)
        elif kind == "steps_done":
            cards.publish("sess-1", 1, 7, "progress", "", ident=rec["id"], at=2, now=110)
    path = state_dir() / cards.STATE_FILE
    before = path.read_bytes()
    stop("Summary: Leave this card alone.")
    assert path.read_bytes() == before


def test_stop_scrubs_summary_paths_links_and_credentials(stop):
    own()
    stop("```text\nSummary: tool output must not appear\n```\n"
         "Summary: Read /Users/example/private.txt and ~/notes/secret.md; "
         "use password=synthetic-password at https://example.invalid/private")
    saved = cards.open_cards()[0]
    assert "[path]" in saved["live"] and "[link]" in saved["live"]
    for leak in ("/Users", "example", "private.txt", "notes", "secret.md", "synthetic-password", "tool output"):
        assert leak not in saved["live"]


def test_stop_skips_a_busy_card_lock_silently(stop):
    own()
    path = state_dir() / cards.STATE_FILE
    before = path.read_bytes()
    with cards._locked():
        started = time.monotonic()
        stop("Summary: Still working.")
        assert time.monotonic() - started < 2
    assert path.read_bytes() == before


@pytest.mark.parametrize(("reply", "expected"), [
    ("First sentence. Second sentence.", "First sentence."),
    ("First line\nSecond line", "First line"),
    ("x" * 230, "x" * 200),
    ("```text\nSummary: raw file contents\n```\nSafe sentence. More.", "Safe sentence."),
])
def test_stop_falls_back_to_a_short_first_sentence(stop, reply, expected):
    own()
    stop(reply)
    assert cards.open_cards()[0]["live"] == expected


def test_live_translation_uses_only_the_existing_cache(monkeypatch):
    own()
    seen = []
    monkeypatch.setattr(translate, "cached", lambda text, cfg: seen.append(text) or "הבדיקות פועלות")
    monkeypatch.setattr(translate, "translate_text", lambda *a, **k: pytest.fail("translation engine called"))
    hooks.live_card("sess-1", "Summary: Tests are running.", Config(translate=True))
    assert seen == ["Tests are running."]
    assert cards.open_cards()[0]["live"] == "הבדיקות פועלות"
    monkeypatch.setattr(translate, "cached", lambda text, cfg: None)
    hooks.live_card("sess-1", "Summary: English fallback.", Config(translate=True))
    assert cards.open_cards()[0]["live"] == "English fallback."


def test_live_fields_survive_agent_edits_and_are_scrubbed_when_loaded():
    rec = own()
    cards.update_live("sess-1", "Working", now=200)
    edited = cards.publish("sess-1", 1, 7, "progress", "", ident=rec["id"], at=1, now=210)
    assert edited["live"] == "Working" and edited["live_at"] == 200
    assert edited["detail"] == rec["detail"]
    path = state_dir() / cards.STATE_FILE
    data = json.loads(path.read_text())
    data[rec["id"]]["live"] = "Read /private/notes.txt"
    path.write_text(json.dumps(data))
    assert cards.open_cards()[0]["live"] == "Read [path]"


def test_stale_plan_reminder_is_ten_minutes_and_asks_for_rewritten_steps():
    assert cards.PROGRESS_STALE_AFTER == hooks.PROGRESS_STALE == 600
    rec = own()
    cards.update_live("sess-1", "New work", now=700)
    assert not cards.is_stale(rec, 700) and cards.is_stale(cards.open_cards()[0], 701)
    assert cards.stale_reminder("sess-1", 701) == rec["id"]
    assert "--step" in hooks.PROGRESS_UPDATE and "do not just bump --at" in hooks.PROGRESS_UPDATE


def test_a_quiet_background_reply_never_reaches_the_card(stop, monkeypatch):
    rec = own()
    monkeypatch.setattr(hooks, "background_only_turn", lambda *a, **kw: True)
    stop("Summary: internal background detail.")
    saved = {r["id"]: r for r in cards.open_cards()}
    assert "live" not in saved[rec["id"]]


def test_background_counts_use_full_session_ids():
    from ctb import panel
    rec = {"kind": "progress", "state": "open", "session": "12345678-b", "steps": ["a"], "at": 0, "percent": 0}
    out = panel._card_activity(rec, {}, {"12345678-a": 1, "12345678-b": 3}, {})
    assert out["bg"] == 3


@pytest.mark.parametrize("line", ["Summary: Nothing new for you.", "Summary: No change.", "Summary: אין חדש בשבילך.",
                                  "Summary: Still waiting"])
def test_a_no_news_summary_keeps_the_last_real_outcome(line):
    rec = own()
    hooks.live_card("sess-1", "Summary: Tests passed; installing now.", Config())
    hooks.live_card("sess-1", line, Config())
    saved = {r["id"]: r for r in cards.open_cards()}
    assert saved[rec["id"]]["live"] == "Tests passed; installing now."


def test_a_real_summary_after_a_no_news_one_still_updates():
    rec = own()
    hooks.live_card("sess-1", "Summary: Nothing new for you.", Config())
    hooks.live_card("sess-1", "Summary: Installed; cards now update themselves.", Config())
    assert {r["id"]: r for r in cards.open_cards()}[rec["id"]]["live"] == "Installed; cards now update themselves."
