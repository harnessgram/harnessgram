"""Terminal notices and display padding never leak into Telegram mirrors."""

from types import SimpleNamespace

import pytest

from ctb import inbox, notices


TIP_ROW = "✦ Heads up · Try the new command   1: Learn more   2: Knew this already   0: Dismiss"
TIP_BLOCK = "\n".join([
    "✦ Heads up · Try the new command",
    "  It can save time",
    "  1: Learn more   2: Knew this already   0: Dismiss",
])


@pytest.mark.parametrize("tip", [TIP_ROW, TIP_BLOCK, TIP_BLOCK + "\n↓ 2 more",
                                  "\u200f" + TIP_BLOCK.replace("\n", "\n\u200f")])
def test_every_tip_shape_is_removed_from_relayed_screen_text(tip):
    relayed = inbox.relay_screen_text("Ready   for you\n" + tip + "\n❯")

    assert relayed == "Ready for you\n❯"
    assert "Heads up" not in relayed and "Dismiss" not in relayed


def test_prose_padding_and_box_edges_collapse_but_code_and_tables_do_not():
    screen = "\n".join([
        "│ This   is    prose │",
        "```python",
        "value  =  first   + second",
        "```",
        "| name  | value   |",
        "| one   | two     |",
    ])

    assert inbox.relay_screen_text(screen) == "\n".join([
        "This is prose",
        "```python",
        "value  =  first   + second",
        "```",
        "| name  | value   |",
        "| one   | two     |",
    ])


def test_code_indented_after_pane_text_margin_rules_and_inline_bars_survive():
    body = "\n".join(["Run this:", "  x  =  1", "━━━━━━", "a │ b", "│ boxed   prose │", "╭────╮", "╰────╯"])
    assert inbox.relay_screen_text(body).splitlines() == ["Run this:", "  x  =  1", "━━━━━━", "a │ b", "boxed prose"]


def _relay(monkeypatch, translated, cached=None):
    sent = []
    bot = SimpleNamespace(call=lambda method, params: sent.append((method, params)) or {"message_id": 7})
    cfg = SimpleNamespace(home_chat_id=-10, ui_language="he", translate=True, target_language="he")
    monkeypatch.setattr(notices, "home_chat", lambda _cfg: -10)
    import ctb.translate as T
    monkeypatch.setattr(T, "translate_text", translated)
    monkeypatch.setattr(T, "cached", lambda text, cfg: cached)
    assert notices._relay(cfg, bot, "sample", 3, None, notices.find(TIP_ROW)) == 7
    return sent[0][1]


def test_notice_is_relayed_in_hebrew_first_with_the_english_on_its_own_line(monkeypatch):
    params = _relay(monkeypatch, lambda text, cfg, fast=False: "נסו   את הפקודה החדשה")
    head, local, blank, english = params["text"].split("\n")
    assert local == "\u200fנסו את הפקודה החדשה"
    assert blank == "" and english.startswith("Heads up") and "  " not in english
    assert not any(ch.isascii() and ch.isalpha() for ch in local)
    labels = [b["text"] for row in params["reply_markup"]["inline_keyboard"][:-1] for b in row]
    assert labels == ["1. מידע נוסף", "2. כבר ידעתי", "0. סגירה"]
    assert [b["callback_data"].split(":")[1] for row in params["reply_markup"]["inline_keyboard"][:-1]
            for b in row] == ["1", "2", "0"]


def test_failed_translation_keeps_the_original_notice(monkeypatch):
    def boom(text, cfg, fast=False):
        raise RuntimeError("engine down")
    params = _relay(monkeypatch, boom)
    assert params["text"].split("\n")[1].startswith("Heads up")


def test_a_slow_translation_never_holds_the_notice(monkeypatch):
    import threading
    gate = threading.Event()
    monkeypatch.setattr(notices, "TRANSLATE_WAIT", 0.05)
    params = _relay(monkeypatch, lambda text, cfg, fast=False: gate.wait(5) and "מאוחר")
    gate.set()
    assert params["text"].split("\n")[1].startswith("Heads up")


def test_a_cached_translation_is_used_without_an_engine_call(monkeypatch):
    def engine(text, cfg, fast=False):
        raise AssertionError("engine called")
    params = _relay(monkeypatch, engine, cached="<b>נסו</b>")
    assert params["text"].split("\n")[1] == "\u200f&lt;b&gt;נסו&lt;/b&gt;"
