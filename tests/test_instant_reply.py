"""Instant replies: the reply goes out at once exactly as Claude wrote it, then the same message is edited in
place to its translation with the original folded under it (config `original`: "edit", the default). Interim
text between tool steps follows the same instant-then-edit path, and the live activity card folds older steps."""
import threading
import time

from conftest import GOOD_TOKEN, FakeBot
from ctb import flood
from ctb import mirror as M
from ctb import progress as P
from ctb.config import Config, state_dir
from ctb.telegram import FloodSkipped, TelegramError
from ctb.topics import TopicMap

OWNER = 7
TEXT = "Plain English reply here."


def mk(tmp_path, bot, **kw):
    c = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he", **kw)
    return M.Mirror(c, bot, TopicMap(tmp_path / "state" / "topics.json", bot, OWNER))


def sends(bot):
    return bot.methods("sendMessage")


def edits(bot):
    return [p for p in bot.methods("editMessageText") if "message_id" in p]


def test_default_mode_is_instant_edit():
    assert Config().original == "edit"


def test_original_is_visible_before_the_translation_is_ready(tmp_path):
    bot = FakeBot()
    seen_before = []

    def slow(text):
        deadline = time.monotonic() + 2
        while not sends(bot) and time.monotonic() < deadline:
            time.sleep(0.01)
        seen_before.append(bool(sends(bot)))
        return "תרגום מלא"

    sent = mk(tmp_path, bot).send_reply("P", "s", TEXT, slow)
    assert seen_before == [True]
    assert sent == 1 and len(sends(bot)) == 1  # one message, one notification
    first = sends(bot)[0]
    assert TEXT in first["text"] and "תרגום" not in first["text"]
    (edit,) = edits(bot)
    assert edit["message_id"] == first["message_id"] and edit["chat_id"] == first["chat_id"]
    assert edit["text"].index("תרגום מלא") < edit["text"].index("<blockquote expandable>")
    assert TEXT in edit["text"] and edit["parse_mode"] == "HTML"
    assert "disable_notification" not in edit  # an edit never pings


def test_summary_line_is_pinned_in_hebrew_after_the_edit(tmp_path):
    bot = FakeBot()
    mk(tmp_path, bot).send_reply("P", "s", "Did the work.\n\nSummary: all good",
                                 lambda t: "עשיתי את העבודה.\n\nהכול תקין")
    assert sends(bot)[0]["text"].lstrip("‏").startswith("📌 all good")
    assert edits(bot)[0]["text"].lstrip("‏").startswith("📌 הכול תקין")


def test_failed_translation_keeps_the_original_with_a_note(tmp_path):
    bot = FakeBot()
    m = mk(tmp_path, bot)

    def boom(text):
        raise RuntimeError("engine down")

    assert m.send_reply("P", "s", TEXT, boom) == 1 and m.dropped == 0
    (edit,) = edits(bot)
    assert TEXT in edit["text"] and "התרגום לא זמין" in edit["text"]


def test_timed_out_translation_keeps_the_original_with_a_note(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "TRANSLATE_TIMEOUT", 0.05)
    release = threading.Event()
    bot = FakeBot()
    mk(tmp_path, bot).send_reply("P", "s", TEXT, lambda t: release.wait(2) and "מאוחר")
    release.set()
    assert len(sends(bot)) == 1 and "התרגום לא זמין" in edits(bot)[0]["text"]


def test_multi_part_reply_edits_each_part_in_place(tmp_path):
    bot = FakeBot()
    original = "\n\n".join(f"Paragraph {i} " + "word " * 150 for i in range(10))
    mk(tmp_path, bot, original="edit-off").send_reply(
        "P", "s", original, lambda t: t.replace("Paragraph", "פסקה").replace("word", "מילה"))
    first_sends = [p for p in sends(bot) if "Paragraph" in p["text"]]
    assert len(first_sends) >= 2
    edited = {p["message_id"]: p["text"] for p in edits(bot)}
    assert all("פסקה" in edited[p["message_id"]] for p in first_sends)


def test_edit_overflow_sends_the_rest_as_follow_ups(tmp_path):
    bot = FakeBot()
    sent = mk(tmp_path, bot).send_reply("P", "s", "Short. " + "x " * 1400,
                                        lambda t: ("ארוך מאוד " * 400) + "\n\n" + ("סוף " * 300))
    followups = sends(bot)[1:]
    assert followups and sent == 1 + len(followups)
    every = "".join(p["text"] for p in edits(bot) + followups)
    assert "סוף" in every and "<blockquote expandable>" in every
    assert all(len(p["text"]) <= M.TELEGRAM_LIMIT for p in edits(bot) + followups)


def test_refused_edit_falls_back_to_a_follow_up(tmp_path):
    bot = FakeBot()
    bot.fail["editMessageText"] = "Bad Request: message to edit not found"
    m = mk(tmp_path, bot)
    assert m.send_reply("P", "s", TEXT, lambda t: "תרגום") == 2 and m.dropped == 0
    assert "תרגום" in sends(bot)[1]["text"]


def test_closed_flood_gate_is_waited_out_once(tmp_path, monkeypatch):
    bot = FakeBot()
    slept = []
    monkeypatch.setattr(M.time, "sleep", lambda s: slept.append(s))
    real = flood.optional_call
    calls = {"n": 0}

    def gated(b, method, params=None, budget=True, **kw):
        calls["n"] += 1
        if method == "editMessageText" and calls["n"] == 1:
            raise FloodSkipped("flood gate closed", 3.0)
        return real(b, method, params, budget=budget, **kw)

    monkeypatch.setattr(flood, "optional_call", gated)
    assert mk(tmp_path, bot).send_reply("P", "s", TEXT, lambda t: "תרגום") == 1
    assert slept == [3.0] and len(edits(bot)) == 1


def test_long_flood_limit_never_sends_around_the_gate(tmp_path, monkeypatch):
    # Review of #143: a flood wait over the cap fell through to an essential send. Now the edit waits as
    # Telegram asks, up to EDIT_FLOOD_WAIT in all, then leaves the original as it is.
    bot = FakeBot()
    slept = []
    monkeypatch.setattr(M.time, "sleep", lambda s: slept.append(s))
    flood.record_429(OWNER, 60)  # the shared gate stays closed (the mocked sleep does not move time)
    m = mk(tmp_path, bot)
    assert m.send_reply("P", "s", TEXT, lambda t: "תרגום") == 1
    assert edits(bot) == [] and len(sends(bot)) == 1 and TEXT in sends(bot)[0]["text"]
    assert slept and sum(slept) <= M.EDIT_FLOOD_WAIT and m.dropped == 0


def test_yes_no_buttons_survive_the_edit(tmp_path):
    bot = FakeBot()
    mk(tmp_path, bot).send_reply("P", "s", "Should I start with three groups?", lambda t: "להתחיל עם שלוש קבוצות?")
    asked = [p for p in sends(bot) if "say:yes" in str(p.get("reply_markup"))]
    edit = [p for p in edits(bot) if p["message_id"] == asked[0]["message_id"]][0]
    assert "say:yes" in str(edit.get("reply_markup"))


def test_interim_text_is_edited_to_its_translation_with_the_original_folded():
    bot = FakeBot()
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he")
    assert P.translate_interim(cfg, bot, OWNER, 5, [[41, "Now the second phase"]], lambda t: "עכשיו השלב השני") == 1
    (edit,) = edits(bot)
    assert edit["message_id"] == 41
    assert edit["text"].index("עכשיו השלב השני") < edit["text"].index("<blockquote expandable>Now the second phase")


def test_interim_translation_failure_adds_a_note():
    bot = FakeBot()
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he")

    def boom(text):
        raise RuntimeError("down")

    assert P.translate_interim(cfg, bot, OWNER, 5, [[41, "Now the second phase"]], boom) == 0
    assert "התרגום לא זמין" in edits(bot)[0]["text"]


def test_interim_edit_that_would_overflow_follows_as_a_new_message():
    bot = FakeBot()
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he")
    P.translate_interim(cfg, bot, OWNER, 5, [[41, "a" * 1800]], lambda t: "ב" * 3000)
    assert edits(bot) == [] and sends(bot)[0]["message_thread_id"] == 5


def test_card_tags_core_tools_and_folds_older_steps():
    rows = [{"kind": "action", "t": i, "l": f"⚙️ step {i}", "d": f"cmd {i}", "tool": "Bash"} for i in range(8)]
    cards = [P.card(r, False) for r in rows]
    assert "⁨Bash⁩: step 0" in cards[0]
    text = P.render_cards("en", cards, 0)
    folded = text.split("<blockquote expandable>")[1].split("</blockquote>")[0]
    assert "step 0" in folded and "cmd 0" not in folded
    assert "cmd 7" in text and "5 earlier steps" in text


def test_unchanged_translation_does_not_edit(tmp_path):
    bot = FakeBot()
    mk(tmp_path, bot).send_reply("P", "s", TEXT, lambda t: t)
    assert len(sends(bot)) == 1 and edits(bot) == []


def test_not_modified_counts_as_success(tmp_path):
    bot = FakeBot()
    bot.fail["editMessageText"] = "Bad Request: message is not modified"
    assert mk(tmp_path, bot).send_reply("P", "s", TEXT, lambda t: "תרגום") == 1
    assert len(sends(bot)) == 1


def test_last_reply_points_at_the_edited_message(tmp_path):
    from ctb.topics import latest_reply
    bot = FakeBot()
    mk(tmp_path, bot).send_reply("P", "s", "Did it.\n\nSummary: done", lambda t: "עשיתי.\n\nבוצע")
    first = sends(bot)[0]
    assert latest_reply(OWNER, first["message_thread_id"]) == first["message_id"]
    assert edits(bot)[0]["message_id"] == first["message_id"]


def write_rows(session, rows):
    import json
    path = state_dir() / "progress"
    path.mkdir(parents=True, exist_ok=True)
    with open(path / f"{session}.jsonl", "a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def test_interim_text_goes_out_untranslated_first_then_is_edited(monkeypatch):
    from ctb import translate
    bot = FakeBot()
    TopicMap(state_dir() / "topics.json", bot, OWNER).ensure("s", "T")
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he", translate=True)
    order = []
    monkeypatch.setattr(translate, "translate_text",
                        lambda text, c, **k: order.append("translate") or "השלב השני")
    real_call = bot.call

    def call(method, params=None, timeout=15):
        if method == "sendMessage" and (params or {}).get("text") == "Now the second phase":
            order.append("send-original")
        return real_call(method, params, timeout)

    bot.call = call
    write_rows("s", [{"kind": "text", "uuid": "u1", "text": "Now the second phase"}])
    bot.calls.clear()
    P.run_send_progress(cfg, "s", bot=bot, throttle=0, now_fn=lambda: 1000.0)
    assert order[:2] == ["send-original", "translate"]
    original = [p for p in sends(bot) if p["text"] == "Now the second phase"]
    assert len(original) == 1
    assert [p["message_id"] for p in edits(bot)] == [original[0]["message_id"]]
    assert "השלב השני" in edits(bot)[0]["text"]


def test_fast_stop_flush_never_waits_for_a_translation(monkeypatch):
    from ctb import translate
    bot = FakeBot()
    TopicMap(state_dir() / "topics.json", bot, OWNER).ensure("s", "T")
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he", translate=True)

    def never(*a, **k):
        raise AssertionError("a fast flush must not translate")

    monkeypatch.setattr(translate, "translate_text", never)
    from ctb import hooks
    spawned = []
    monkeypatch.setattr(hooks, "spawn", lambda args, text=None: spawned.append(args))
    write_rows("s", [{"kind": "text", "uuid": "u1", "text": "Now the second phase"}])
    P.run_send_progress(cfg, "s", bot=bot, throttle=0, now_fn=lambda: 1000.0, fast=True)
    assert [p["text"] for p in sends(bot) if "phase" in p["text"]] == ["Now the second phase"]
    assert edits(bot) == []
    # 2026-11-10: the text is no longer left in English: a detached run translates it after the Stop
    assert spawned == [["progress", "--session=s", "--jobs"]]


def test_untagged_rows_keep_their_heading():
    assert P.tool_tagged("📋 Plan (1/3)\n⬜ a", "TodoWrite") == "📋 Plan (1/3)\n⬜ a"
    assert P.tool_tagged("🤖 [writer] run", "Bash") == "🤖 [writer] run"
    assert P.tool_tagged("‏📖 קורא קובץ", "Read") == "‏📖 ⁨Read⁩: קורא קובץ"


# --- review of #143 ------------------------------------------------------------------------------------

class EditBot(FakeBot):
    """Refuses editMessageText for the message ids in `refuse` (as if they were deleted)."""

    def __init__(self, refuse=()):
        super().__init__()
        self.refuse = set(refuse)

    def call(self, method, params=None, timeout=15):
        if method == "editMessageText" and (params or {}).get("message_id") in self.refuse:
            self.calls.append((method, params))
            raise TelegramError("Bad Request: message to edit not found")
        return super().call(method, params, timeout)


def two_part_reply():
    return "\n\n".join(f"Paragraph {i} " + "word " * 150 for i in range(10))


def two_part_translation(text):
    return text.replace("Paragraph", "פסקה").replace("word", "מילה")


def test_team_edit_stays_withheld_when_only_the_original_matched(tmp_path):
    # P1: the English part matched a block pattern; its translation does not. The group must not get it.
    from test_team import GROUP, by_chat, mirror_rig
    for mode in ("edit", "edit-off"):
        m, bot = mirror_rig(tmp_path / mode, block_patterns=[r"(?i)internal-only"])
        m.cfg.target_language = "he"
        m.cfg.original = mode
        m.send_reply("Shared one", "shared", "This is internal-only material.", lambda t: "חומר פנימי בלבד.")
        team = by_chat(bot, GROUP)
        assert team and all("חומר" not in t and "internal-only" not in t for t in team), (mode, team)
        assert any("חומר פנימי" in t for t in by_chat(bot, OWNER))


def test_team_follow_up_stays_withheld_when_only_the_original_matched(tmp_path):
    from test_team import GROUP, by_chat, mirror_rig
    m, bot = mirror_rig(tmp_path, block_patterns=[r"(?i)internal-only"])
    m.cfg.target_language = "he"
    m.cfg.original = "edit-off"
    bot.fail["editMessageText"] = "Bad Request: message to edit not found"  # the team's edit is refused
    m.send_reply("Shared one", "shared", "This is internal-only material.", lambda t: "חומר פנימי בלבד.")
    assert all("חומר" not in t for t in by_chat(bot, GROUP))


def test_refused_first_edit_keeps_the_translation_in_order(tmp_path):
    # P2: part 1's edit failed, part 2's succeeded, and part 1's translation came last. Now every part
    # from the first refused edit on follows in order.
    probe = FakeBot()
    mk(tmp_path / "probe", probe, original="edit-off").send_reply("P", "s", two_part_reply(), two_part_translation)
    first_id = sends(probe)[0]["message_id"]
    bot = EditBot(refuse={first_id})
    mk(tmp_path, bot, original="edit-off").send_reply("P", "s", two_part_reply(), two_part_translation)
    originals = [p for p in sends(bot) if "Paragraph" in p["text"]]
    follow = [p["text"] for p in sends(bot) if p not in originals]
    assert len(follow) >= 2 and "פסקה 0 " in follow[0] and "פסקה 9 " in follow[-1]
    assert not [p for p in edits(bot) if p["message_id"] != first_id]  # no later part edited out of order


def test_owner_follow_up_never_duplicates_the_team_copy(tmp_path):
    from test_team import GROUP, mirror_rig
    m, bot = mirror_rig(tmp_path)
    m.cfg.target_language = "he"
    owner_ids = set()
    real = bot.call

    def call(method, params=None, timeout=15):
        result = real(method, params, timeout)
        if method == "sendMessage" and params.get("chat_id") != GROUP:
            owner_ids.add(result["message_id"])
        if method == "editMessageText" and params.get("message_id") in owner_ids:
            raise TelegramError("Bad Request: message can't be edited")
        return result

    bot.call = call
    m.send_reply("Shared one", "shared", TEXT, lambda t: "תרגום")
    group_sends = [p for meth, p in bot.calls if meth == "sendMessage" and p.get("chat_id") == GROUP]
    group_edits = [p for meth, p in bot.calls if meth == "editMessageText" and p.get("chat_id") == GROUP]
    assert len(group_sends) == 1 and len(group_edits) == 1  # edited in place, never sent again
    owner_sends = [p for meth, p in bot.calls if meth == "sendMessage" and p.get("chat_id") != GROUP]
    assert len(owner_sends) == 2 and "תרגום" in owner_sends[1]["text"]


def test_buttons_follow_the_translation_when_their_message_cannot_be_edited(tmp_path):
    # P2: the replacement for a refused edit lost the yes/no buttons.
    probe = FakeBot()
    mk(tmp_path / "probe", probe).send_reply("P", "s", "Should I start?", lambda t: "להתחיל?")
    bot = EditBot(refuse={sends(probe)[0]["message_id"]})
    mk(tmp_path, bot).send_reply("P", "s", "Should I start?", lambda t: "להתחיל?")
    follow = [p for p in sends(bot) if "להתחיל" in p["text"]]
    assert len(follow) == 1 and "say:yes" in str(follow[0].get("reply_markup"))


def test_buttons_sit_on_the_final_part_only(tmp_path):
    bot = FakeBot()
    question = two_part_reply() + "\n\nShould I continue?"
    mk(tmp_path, bot, original="edit-off").send_reply("P", "s", question,
                                                       lambda t: two_part_translation(t).replace("Should I continue?", "להמשיך?"))
    shown = edits(bot) + [p for p in sends(bot) if "Paragraph" not in p["text"]]  # what the translation became
    with_buttons = [p for p in shown if "say:yes" in str(p.get("reply_markup"))]
    assert len(with_buttons) == 1 and "להמשיך" in with_buttons[0]["text"]


def test_index_points_at_the_follow_up_that_replaced_a_refused_edit(tmp_path):
    # P2: the index kept the original's id after its translation arrived as a new message.
    probe = FakeBot()
    mk(tmp_path / "probe", probe).send_reply("P", "s", "Did it.\n\nSummary: done", lambda t: "עשיתי.\n\nבוצע")
    bot = EditBot(refuse={sends(probe)[0]["message_id"]})
    m = mk(tmp_path, bot)
    indexed = []
    m._index_reply = lambda session, mid, text, has_summary: indexed.append((mid, text))
    m.send_reply("P", "s", "Did it.\n\nSummary: done", lambda t: "עשיתי.\n\nבוצע")
    follow = [p for p in sends(bot) if "בוצע" in p["text"]]
    assert indexed == [(follow[0]["message_id"], "עשיתי.\n\nבוצע")]


def test_index_keeps_the_original_when_the_translation_did_not_arrive(tmp_path, monkeypatch):
    monkeypatch.setattr(M.time, "sleep", lambda s: None)
    flood.record_429(OWNER, 600)
    bot = FakeBot()
    m = mk(tmp_path, bot)
    indexed = []
    m._index_reply = lambda session, mid, text, has_summary: indexed.append((mid, text))
    m.send_reply("P", "s", "Did it.\n\nSummary: done", lambda t: "עשיתי.\n\nבוצע")
    assert indexed == [(sends(bot)[0]["message_id"], "Did it.\n\ndone")]


def test_long_interim_translation_is_split_into_complete_parts():
    # P1: a translation over 4096 characters was cut at body[:4096], losing the rest and maybe a tag.
    bot = FakeBot()
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he")
    he = "\n\n".join(f"פסקה {i} עם <סימנים> & עוד " + "מילה " * 120 for i in range(12))
    assert P.translate_interim(cfg, bot, OWNER, 5, [[41, "x " * 900]], lambda t: he) == 1
    parts = [p["text"] for p in sends(bot)]
    assert len(parts) >= 2 and edits(bot) == []
    assert all(len(t) <= 4096 for t in parts)
    import html as H
    joined = H.unescape("".join(t.lstrip("\u200f") for t in parts))
    assert all(f"פסקה {i} " in joined for i in range(12))
    assert all(t.count("&") == t.count(";") or "&amp;" in t for t in parts)  # no cut entity


def test_interim_parts_resume_after_a_flood_limit_without_duplicates():
    from ctb.telegram import TelegramError as TE
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he")
    he = "\n\n".join(f"פסקה {i} " + "מילה " * 150 for i in range(10))

    class Flaky(FakeBot):
        def __init__(self):
            super().__init__()
            self.n = 0

        def call(self, method, params=None, timeout=15):
            if method == "sendMessage":
                self.n += 1
                if self.n == 2:
                    self.calls.append((method, params))
                    raise TE("Too Many Requests", 429, 120)
            return super().call(method, params, timeout)

    bot = Flaky()
    state, saved = {}, []
    import pytest
    with pytest.raises(P.InterimFlood):
        P.translate_interim(cfg, bot, OWNER, 5, [[41, "x " * 900]], lambda t: he, state=state,
                            persist=lambda: saved.append(dict(state)))
    assert state["tr_sent"] == 1 and saved
    (state_dir() / flood.STATE_FILE).unlink(missing_ok=True)
    calls = []
    P.translate_interim(cfg, bot, OWNER, 5, [[41, "x " * 900]], lambda t: calls.append(t) or "שונה", state=state)
    assert calls == []  # the saved translation is reused, never translated again
    delivered = [p["text"] for meth, p in bot.calls if meth == "sendMessage"]
    firsts = [t for t in delivered if "פסקה 0 " in t]
    assert len(firsts) == 1 and any("פסקה 9 " in t for t in delivered)
