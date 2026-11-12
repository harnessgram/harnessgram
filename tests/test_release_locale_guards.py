import os
import time
from datetime import datetime, timezone

import pytest

from ctb import heartbeat, usage, inbox, mirror, radio_board
from ctb.config import Config


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="system time zone switching needs tzset")
@pytest.mark.parametrize("zone, expected", [("America/New_York", "2026-07-01T20:00:00-04:00"),
                                            ("Asia/Tokyo", "2026-07-02T20:00:00+09:00")])
def test_reset_uses_system_zone(monkeypatch, zone, expected):
    old = os.environ.get("TZ")
    try:
        monkeypatch.setenv("TZ", zone)
        time.tzset()
        assert usage.reset_time("resets 8pm", datetime(2026, 7, 1, 23, tzinfo=timezone.utc)).isoformat() == expected
    finally:
        if old is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", old)
        time.tzset()


def test_english_spinner_and_reset_labels():
    cfg = Config(ui_language="en")
    assert heartbeat.hebrew_status("✶ Thinking… (16s · ↓ 1k tokens)", cfg) == "✶ Thinking… (16s · ↓ 1k tokens)"
    assert usage.when("rolling", lang="en") == "↻ rolling"
    assert usage.when("rolling", lang="he") == "↻ מתגלגל"


def test_context_size_uses_ui_language():
    assert mirror.context_label(182_000, 1_000_000, "en") == "📏 182k · 18%"
    assert mirror.context_label(182_000, 1_000_000, "he") == "📏 182 אלף · 18%"


@pytest.mark.parametrize("lang, expected", [("en", "General"), ("he", "כללי")])
def test_radio_board_general_project_uses_ui_language(lang, expected):
    sources = {"questions": [{"project": radio_board.HOME, "text": "Example", "at": 1}]}
    assert radio_board.collect(sources, {}, 10, lang=lang)[0]["name"] == expected


@pytest.mark.parametrize("ui, target, expected", [
    ("en", "he", "Written directly in the session"),
    ("he", "en", "נכתב ישירות בסשן"),
])
def test_direct_prompt_label_uses_ui_not_reply_language(monkeypatch, ui, target, expected):
    class Bot:
        def __init__(self):
            self.sent = None

        def call(self, method, params):
            self.sent = params["text"]

    class Place:
        chat = 1
        thread = 2

    monkeypatch.setattr(mirror, "place", lambda *args: Place())
    bot = Bot()
    cfg = Config(ui_language=ui, target_language=target, redact_outgoing=False)
    assert mirror.run_prompt(cfg, "sample", "Sample", bot=bot) == 0
    assert bot.sent == f"💻 {expected}:\nSample"


@pytest.mark.parametrize("target, translated", [("he", "مرحبا\nشكرا"),
                                                 ("he", "選択\n拒否"),
                                                 ("he", "בחר Model\nדחה"),
                                                 ("ja", "مرحبا\nشكرا")])
def test_translated_menu_rejects_wrong_script(monkeypatch, target, translated):
    monkeypatch.setattr("ctb.translate.cached", lambda text, cfg: translated)
    cfg = Config(translate=True, target_language=target)
    body = "Select model\n❯ 1. Accept changes\n  2. Reject changes"
    rows = inbox.menu_buttons(body, cfg)["inline_keyboard"]
    assert rows[0][0]["text"] == "1. Accept changes"
    assert rows[0][0]["callback_data"].startswith("k:1:")


def test_translated_menu_keeps_original_on_separate_line(monkeypatch):
    monkeypatch.setattr("ctb.translate.cached", lambda text, cfg: "קבל שינויים\nדחה שינויים")
    cfg = Config(translate=True, target_language="he")
    body = "Select model\n❯ 1. Accept changes\n  2. Reject changes"
    rows = inbox.menu_buttons(body, cfg)["inline_keyboard"]
    assert rows[0][0]["text"] == "1. קבל שינויים\n(Accept changes)"
    assert rows[1][0]["text"] == "2. דחה שינויים\n(Reject changes)"


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="system time zone switching needs tzset")
def test_reset_weekly_date_uses_future_dst_offset(monkeypatch):
    old = os.environ.get("TZ")
    try:
        monkeypatch.setenv("TZ", "America/New_York")
        time.tzset()
        stamp = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
        assert usage.reset_time("resets Nov 2 at 8pm", stamp).isoformat() == "2026-11-02T20:00:00-05:00"
    finally:
        if old is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", old)
        time.tzset()


def test_extracted_screens_keep_hebrew_in_the_language_layer():
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "ctb"
    for filename in ("panel_youtube.py", "radio_post.py", "setup_page.py",
                     "studio/install.py", "studio/provider.py"):
        tree = ast.parse((root / filename).read_text(encoding="utf-8"))
        literals = [node for node in ast.walk(tree) if isinstance(node, ast.Constant)
                    and isinstance(node.value, str) and any("\u0590" <= c <= "\u05ff" for c in node.value)]
        assert not literals, filename


def test_youtube_callback_page_keeps_both_languages():
    from ctb import panel_youtube

    assert "החיבור הושלם" in panel_youtube.page("he", True, "sample").decode()
    assert "Not connected" in panel_youtube.page("en", False, "sample").decode()


@pytest.mark.parametrize("target, translated", [("ar", "مرحبا\nشكرا"), ("zh", "選択\n拒否"),
                                                ("hi", "कृपया\nनहीं")])
def test_translated_menu_accepts_matching_target_script(monkeypatch, target, translated):
    monkeypatch.setattr("ctb.translate.cached", lambda text, cfg: translated)
    cfg = Config(translate=True, target_language=target)
    body = "Select model\n❯ 1. Accept changes\n  2. Reject changes"
    rows = inbox.menu_buttons(body, cfg)["inline_keyboard"]
    assert rows[0][0]["text"] == "1. " + translated.splitlines()[0] + "\n(Accept changes)"
    assert rows[0][0]["callback_data"].startswith("k:1:")


def test_reset_clock_in_the_repeated_hour_stays_in_the_future(monkeypatch):
    """When clocks go back, a reset inside the repeated hour must not be read as already past."""
    import time
    from datetime import datetime, timezone
    from ctb import usage
    monkeypatch.setenv("TZ", "Asia/Jerusalem")
    time.tzset()
    try:
        stamp = datetime(2026, 10, 24, 23, 15, tzinfo=timezone.utc)  # 01:15, second pass
        reset = usage.reset_time("resets 1:30am", stamp)
        assert reset.timestamp() - stamp.timestamp() == 15 * 60
        late = datetime(2026, 10, 24, 22, 45, tzinfo=timezone.utc)  # 01:45, first pass: 01:30 comes again
        assert usage.reset_time("resets 1:30am", late).timestamp() - late.timestamp() == 45 * 60
        first = datetime(2026, 10, 24, 22, 15, tzinfo=timezone.utc)  # 01:15, first pass
        assert usage.reset_time("resets 1:30am", first).timestamp() - first.timestamp() == 15 * 60
    finally:
        monkeypatch.undo()
        time.tzset()
