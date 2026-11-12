"""The keyboard's context cell (📏) follows the session's real context size: it was drawn once into the saved
status card and then kept its old number, or had none at all (2026-11-10: 82 of 123 live cards had no context
line). Now the card text always carries the current size, the card is redrawn at every turn end when the size
moved a step, mid-turn at a throttled pace, and a compaction's smaller size counts at once."""
import json
from pathlib import Path

from conftest import GOOD_TOKEN, FakeBot
from ctb import mirror as M
from ctb.config import Config, state_dir
from ctb.topics import TopicMap

OWNER = 7
SID = "0d1a0cbf-0000-4000-8000-000000000001"
THREAD = 40


def cfg():
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he", ui_language="he")


def transcript(tokens: int, compact_to: int = 0) -> Path:
    folder = Path.home() / ".claude" / "projects" / "-tmp-proj"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{SID}.jsonl"
    rows = [{"type": "assistant", "message": {"model": "claude-opus-5-5", "usage": {
        "input_tokens": 10, "cache_read_input_tokens": tokens - 10, "cache_creation_input_tokens": 0}}}]
    if compact_to:
        rows.append({"type": "system", "subtype": "compact_boundary", "compactMetadata": {"postTokens": compact_to}})
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def topic(bot):
    tmap = TopicMap(state_dir() / "topics.json", bot, OWNER)
    tmap.ensure(SID, "Title", thread=THREAD)
    return tmap


def context_cell(markup) -> str:
    return next((b["text"] for row in (markup or {}).get("keyboard", []) for b in row if b["text"].startswith("📏")), "")


def last_card(bot):
    return [p for p in bot.methods("sendMessage") if "reply_markup" in p and "keyboard" in p["reply_markup"]][-1]


def test_a_saved_card_without_a_context_line_gets_the_current_size():
    transcript(184_000)
    live = "\n⏳ שבועית 73% · ↻ ⁦14.11 16:00⁩ · לפני 0 דק׳"  # the shape of most live cards: no context line
    out = M.restamp_footer(live, SID, "he")
    assert out.startswith("📏 הקשר 184 אלף")  # the quota line is restamped from the saved usage
    assert context_cell(M.live_keyboard(out, lang="he", session=SID)) == "📏 184 אלף"


def test_a_stale_context_number_is_replaced_in_every_footer_shape():
    transcript(130_000)
    assert M.with_context("📏 הקשר 114 אלף", SID, "he") == "📏 הקשר 130 אלף"
    assert M.with_context("🧠 הקשר 90 אלף\n🤖 Opus", SID, "he").startswith("🧠 הקשר 130 אלף")
    assert M.with_context("🧠 90k · Opus 5.5 · high", SID, "en").startswith("🧠 130k · Opus 5.5")
    assert M.with_context("📏 Context 90k", SID, "en") == "📏 Context 130k"


def test_a_compaction_counts_at_once():
    transcript(380_000, compact_to=42_000)
    assert M.with_context("📏 הקשר 380 אלף", SID, "he") == "📏 הקשר 42 אלף"


def test_turn_end_redraws_the_card_when_the_context_moved(monkeypatch):
    bot = FakeBot()
    tmap = topic(bot)
    transcript(100_000)
    assert M.refresh_card(cfg(), bot, SID, topics=tmap, create=True)
    assert context_cell(last_card(bot)["reply_markup"]) == "📏 100 אלף"
    # the next turn grew the context; the card was drawn a moment ago, which no longer holds it back
    transcript(131_000)
    assert M.refresh_card(cfg(), bot, SID, topics=tmap, turn_end=True)
    card = last_card(bot)
    assert context_cell(card["reply_markup"]) == "📏 131 אלף" and "הקשר 131 אלף" in card["text"]


def test_turn_end_is_quiet_when_nothing_moved():
    bot = FakeBot()
    tmap = topic(bot)
    transcript(100_000)
    M.refresh_card(cfg(), bot, SID, topics=tmap, create=True)
    bot.calls.clear()
    transcript(104_000)  # under one CONTEXT_STEP: no traffic at all
    assert not M.refresh_card(cfg(), bot, SID, topics=tmap, turn_end=True)
    assert bot.calls == []


def test_mid_turn_redraws_wait_the_short_context_backoff_only():
    bot = FakeBot()
    tmap = topic(bot)
    transcript(100_000)
    t0 = 1_800_000_000.0
    M.refresh_card(cfg(), bot, SID, topics=tmap, create=True, now=t0)
    transcript(150_000)
    assert not M.refresh_card(cfg(), bot, SID, topics=tmap, now=t0 + M.CONTEXT_BACKOFF - 5)  # throttled
    assert M.refresh_card(cfg(), bot, SID, topics=tmap, now=t0 + M.CONTEXT_BACKOFF + 5)
    assert context_cell(last_card(bot)["reply_markup"]) == "📏 150 אלף"
    assert M.CONTEXT_BACKOFF < M.CARD_BACKOFF


def test_the_heartbeat_refreshes_a_card_whose_context_alone_moved(monkeypatch):
    from ctb import accounts, heartbeat
    bot = FakeBot()
    tmap = topic(bot)
    transcript(100_000)
    t0 = 1_800_000_000.0
    M.refresh_card(cfg(), bot, SID, topics=tmap, create=True, now=t0)
    transcript(160_000)
    monkeypatch.setattr(accounts, "live_session_records", lambda: [{"sessionId": SID}])
    drawn = heartbeat.refresh_cards(cfg(), bot, now=t0 + M.CONTEXT_BACKOFF + 5)
    assert drawn == 1
    assert context_cell(last_card(bot)["reply_markup"]) == "📏 160 אלף"


def test_run_reply_refreshes_the_card_at_turn_end(monkeypatch):
    seen = []
    monkeypatch.setattr(M, "refresh_card", lambda c, b, session, **kw: seen.append((session, kw)) or True)
    bot = FakeBot()
    monkeypatch.setattr(M, "build", lambda c: M.Mirror(c, bot, TopicMap(state_dir() / "topics.json", bot, OWNER)))
    monkeypatch.setattr(M, "limit_offer", lambda *a, **k: False)
    c = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="en")
    assert M.run_reply(c, "T", SID, "Done.", False) == 0
    assert seen and seen[0][0] == SID and seen[0][1].get("turn_end") is True


def test_saved_card_texts_keep_working_without_a_transcript():
    assert M.with_context("\n⏳ שבועית 73%", "no-such-session", "he") == "\n⏳ שבועית 73%"


def test_the_compaction_tip_never_repeats_the_model_name_in_an_english_card():
    transcript(450_000)  # review of #150: the tip sat between the number and the model, and every redraw
    out = "🧠 184k · Opus 5.5 · high"  # took it for the model and added the name again
    for _ in range(3):
        out = M.restamp_footer(out, SID, "en")
    first = out.splitlines()[0]
    assert first.startswith("🧠 450k · Opus 5.5 · high") and first.count("Opus 5.5") == 1
    assert first.endswith(" · worth compacting") and first.count("worth compacting") == 1
    assert M.with_context("📏 Context 90k", SID, "en") == "📏 Context 450k · worth compacting"
