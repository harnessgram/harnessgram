"""The morning health check: delivered replies, translation engines and the listener, from local records."""
import json
import time

from conftest import GOOD_TOKEN
from ctb import dashboard, mirror
from ctb.config import Config, state_dir, write_private

NOW = 1_800_000_000.0


def cfg(**kw):
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=7, **dict({"ui_language": "en"}, **kw))


def outcomes(*rows):
    write_private(state_dir() / mirror.OUTCOMES, "\n".join(f"{int(NOW - age)} {o}" for age, o in rows) + "\n")


def listener(state="listening", age=5):
    write_private(state_dir() / "listener.status.json", json.dumps({"state": state, "at": NOW - age}))


def cache(name, engine, age=60):
    import os
    path = state_dir() / "translate-cache" / name
    write_private(path, json.dumps({"text": "x", "engine": engine}))
    os.utime(path, (NOW - age, NOW - age))


def test_every_final_reply_records_its_outcome(bot, monkeypatch):
    class M:
        dropped = False
        last_error = None

        def send_reply(self, *a, **k):
            pass
    monkeypatch.setattr(mirror, "build", lambda c: M())
    monkeypatch.setattr(mirror, "limit_offer", lambda *a, **k: False)
    assert mirror.run_reply(cfg(), "T", "s", "hello", False) == 0
    M.dropped = True
    assert mirror.run_reply(cfg(), "T", "s", "hello", False) == 1
    rows = (state_dir() / mirror.OUTCOMES).read_text().split("\n")
    assert [r.split()[1] for r in rows if r] == ["ok", "fail"]


def test_outcome_log_is_bounded():
    for _ in range(mirror.OUTCOMES_KEEP + 200):
        mirror.record_outcome(True)
    assert len((state_dir() / mirror.OUTCOMES).read_text().splitlines()) < mirror.OUTCOMES_KEEP * 2


def test_healthy_day():
    outcomes((100, "ok"), (200, "ok"), (90000, "fail"))     # the failure is older than 24 hours
    listener()
    cache("a", "mlx"); cache("b", "mlx"); cache("c", "nllb"); cache("d", "gemini", age=90000)
    lines = dashboard.health(now=NOW, lang="en")
    assert lines == ["🩺 <b>Bridge health (24 hours)</b>", "", "✅ All 2 final replies were delivered",
                     "🔤 Translations: mlx 66% · nllb 33% (total 3)", "🟢 The listener is polling Telegram"]


def test_failures_and_a_dead_listener_are_reported():
    outcomes((10, "ok"), (20, "fail"), (30, "fail"))
    listener(state="backoff")
    lines = dashboard.health(now=NOW, lang="en")
    assert "⚠️ 2 of 3 final replies were not delivered" in lines
    assert lines[-1].startswith("🔴 The listener is not running")
    assert dashboard.health(now=NOW, lang="en")[-1] == lines[-1]


def test_no_records_still_gives_a_listener_line():
    assert dashboard.health(now=NOW, lang="en")[2:] == ["🔴 The listener is not running. Run: harnessgram doctor"]


def test_hebrew_report_has_no_mixed_lines():
    outcomes((10, "ok"))
    cache("a", "mlx")
    lines = dashboard.health(now=NOW, lang="he")
    assert lines[2] == "‏✅ כל 1 התשובות הסופיות נמסרו"
    assert "\n".join(lines).count("\nmlx 100%") == 1


def test_only_the_morning_summary_carries_the_report():
    daily = dashboard.text(cfg(), {}, daily=True, now=NOW)
    assert "Bridge health" in daily and "Bridge health" not in dashboard.text(cfg(), {}, now=NOW)
