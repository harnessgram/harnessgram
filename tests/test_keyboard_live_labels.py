"""The reply keyboard under every topic: each settings button shows its current value, the context cell shows how
full the window is, every live label is still recognised when tapped (both languages, bidi marks, a dropped emoji
variation selector), the labels fit a phone, and every change that alters a label moves the card's data key."""
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import GOOD_TOKEN
from ctb import accounts, direct, i18n, inbox as I, mirror as M, statusline, usage
from ctb.config import Config, state_dir, write_private
from ctb.topics import TopicMap

OWNER = 7
SID = "0d1a0cbf-0000-4000-8000-0000000000aa"
RLM, FSI, PDI, LRI = "‏", "⁨", "⁩", "⁦"
LEVELS = ("low", "medium", "high", "xhigh", "max")
MODES = ("default", "plan", "acceptEdits", "auto", "bypassPermissions", "dontAsk")
MODELS = ("Opus 5.5", "Claude Fable 5.2", "Sonnet 5.5 (1M context)", "Haiku 5.5")


def cfg(lang="he"):
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he", ui_language=lang)


def transcript(tokens: int, model: str = "claude-opus-5-5") -> Path:
    folder = Path.home() / ".claude" / "projects" / "-tmp-keyboard"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{SID}.jsonl"
    path.write_text(json.dumps({"type": "assistant", "message": {"model": model, "usage": {
        "input_tokens": 10, "cache_read_input_tokens": tokens - 10, "cache_creation_input_tokens": 0}}}) + "\n")
    return path


def save_model(value: str) -> None:
    write_private(state_dir() / "session-models.json", json.dumps({SID: value}))


def save_window(size: int) -> None:
    write_private(state_dir() / statusline.WINDOWS_FILE, json.dumps({SID: size}))


def labels(kb) -> list:
    return [b["text"] for row in kb["keyboard"] for b in row]


def settings_row(kb) -> list:
    return [b["text"] for b in kb["keyboard"][-2]]


@pytest.fixture(autouse=True)
def no_terminal(monkeypatch):
    monkeypatch.setattr(I, "pane_for_session", lambda s: "")
    monkeypatch.setattr(accounts, "multiple", lambda: False)


# --- 1. every settings button shows its value, and a tap on it is recognised ---------------------------------

def variants(label: str) -> list:
    """How the same label can reach us: as sent, behind a direction mark, isolated, without U+FE0F."""
    return [label, RLM + label, FSI + label + PDI, label.replace("️", ""), " " + label + " "]


@pytest.mark.parametrize("lang", ["he", "en"])
@pytest.mark.parametrize("model", MODELS)
def test_each_settings_button_shows_its_value_and_is_recognised(lang, model):
    for level, mode in zip(LEVELS + ("high",), MODES):
        save_model(f"{model}|{level}")
        direct.record(SID, mode=mode)
        row = settings_row(M.live_keyboard("", lang=lang, session=SID))
        base = settings_row(M.fixed_keyboard(lang))
        assert row[0] != base[0] and row[1] != base[1] and row[2] != base[2]  # no generic label while a value is known
        assert row[1] == "🎚️ " + i18n.t("effort_" + level, lang=lang)
        assert row[2] == "🔐 " + i18n.t("mode_short_" + mode, lang=lang)
        for label, kind in zip(row, ("model", "effort", "mode")):
            assert len(label) <= 16, label  # a third of a phone's width
            for seen in variants(label):
                assert I.live_button(seen) == kind, repr(seen)
                assert I._keyboard_word(seen.strip())  # never taken as a typed answer to a dialog


def test_the_model_label_is_short():
    save_model("Claude Sonnet 5.5 (1M context)|high")
    assert settings_row(M.live_keyboard("", lang="en", session=SID))[0] == "🧠 Sonnet 5.5 1M"
    save_model("Opus 5.5 (1M)|high")
    assert settings_row(M.live_keyboard("", lang="he", session=SID))[0] == "🧠 Opus 5.5 1M"


@pytest.mark.parametrize("lang", ["he", "en"])
def test_unknown_values_keep_the_plain_labels_and_still_work(lang):
    row = settings_row(M.live_keyboard("", lang=lang, session=SID))
    assert row == [i18n.t(k, lang=lang) for k in ("fixed_btn_model", "fixed_btn_thinking", "fixed_btn_permissions")]
    assert [I.live_button(x) for x in row] == ["model", "effort", "mode"]


def test_the_transcript_model_and_a_later_effort_fill_the_buttons_without_a_status_line(tmp_path, monkeypatch):
    path = tmp_path / f"{SID}.jsonl"
    rows = [{"type": "assistant", "effort": "medium", "message": {"model": "claude-opus-5-5"}},
            {"type": "user", "message": {"role": "user", "content": "<local-command-stdout>Set effort level to max</local-command-stdout>"}}]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    from ctb import status
    monkeypatch.setattr(status, "transcript_of", lambda s: str(path) if s == SID else None)
    assert settings_row(M.live_keyboard("", lang="en", session=SID))[:2] == ["🧠 Opus 5.5", "🎚️ maximum"]


def test_a_later_effort_wins_over_the_level_the_status_line_saved(tmp_path, monkeypatch):
    path = tmp_path / f"{SID}.jsonl"
    rows = [{"type": "assistant", "effort": "high", "message": {"model": "claude-opus-5-5"}},
            {"type": "user", "message": {"role": "user", "content": "<local-command-stdout>Set effort level to low</local-command-stdout>"}}]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    from ctb import status
    monkeypatch.setattr(status, "transcript_of", lambda s: str(path) if s == SID else None)
    usage._reply_models.clear()
    save_model("Opus 5.5|high")  # saved before the /effort; the status line did not draw again since
    assert usage.session_model(SID) == ("Opus 5.5", "low")


# --- 2. the permission mode: the terminal first, else what Claude reported ------------------------------------

class Pane:
    def __init__(self, mode, hidden=False):
        self.mode, self.hidden, self.keys = mode, hidden, []

    def __call__(self, *args):
        if args[0] == "send-keys":
            self.keys.append(args[-1])
            cycle = ["default", "acceptEdits", "plan", "auto", "bypassPermissions"]
            self.mode = cycle[(cycle.index(self.mode) + 1) % len(cycle)]
        if args[0] == "capture-pane":
            line = f"  ⏵⏵ {I.MODE_BAR[self.mode]} on (shift+tab to cycle)" if not self.hidden else "Allow this?\n❯ 1. Yes"
            return SimpleNamespace(returncode=0, stdout="output\n" + line)
        return SimpleNamespace(returncode=0, stdout="")


def test_the_mode_falls_back_to_what_claude_reported(monkeypatch):
    direct.record(SID, mode="acceptEdits")
    assert settings_row(M.live_keyboard("", lang="he", session=SID))[2] == "🔐 עריכות"  # no terminal at all
    monkeypatch.setattr(I, "pane_for_session", lambda s: "%1")
    monkeypatch.setattr(I, "_tmux", Pane("auto", hidden=True))
    assert settings_row(M.live_keyboard("", lang="he", session=SID))[2] == "🔐 עריכות"  # a question hides the line
    monkeypatch.setattr(I, "_tmux", Pane("plan"))
    assert settings_row(M.live_keyboard("", lang="en", session=SID))[2] == "🔐 Plan"  # the screen is the newest


def test_a_mode_switched_from_telegram_is_known_at_once(monkeypatch, bot):
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    monkeypatch.setattr(I, "pane_for_session", lambda s: "%1")
    monkeypatch.setattr(I, "_tmux", Pane("default"))
    monkeypatch.setattr(I, "keyboard_back", lambda *a, **k: None)
    monkeypatch.setattr(accounts, "session_is_idle", lambda s, now=None: True)
    direct.record(SID, mode="default")
    before = M.card_data_key(SID)
    assert I.switch_mode(cfg(), bot, {"message_thread_id": 5}, SID, "plan") == "switched"
    assert direct.current(SID)["mode"] == "plan" and M.card_data_key(SID) != before
    # the next hook reports the same mode: no "changed in Claude" line for a change made from Telegram
    assert direct.from_hook({"session_id": SID, "permission_mode": "plan"}) == []


# --- 3. the context cell: size and how full the window is ---------------------------------------------------

def context_cell(kb) -> str:
    return next(x for x in labels(kb) if x.startswith("📏"))


def test_the_context_cell_shows_how_full_the_window_is():
    transcript(182_000)
    assert context_cell(M.live_keyboard("", lang="en", session=SID)) == "📏 182k"  # window unknown: no guess
    save_window(1_000_000)
    assert context_cell(M.live_keyboard("", lang="en", session=SID)) == "📏 182k · 18%"
    assert context_cell(M.live_keyboard("", lang="he", session=SID)) == "📏 182 אלף · 18%"
    save_window(200_000)
    transcript(150_000)
    assert context_cell(M.live_keyboard("📏 הקשר 90 אלף", lang="he", session=SID)) == "📏 150 אלף · 75%"  # live, not saved


def test_a_1m_model_name_tells_the_window():
    transcript(250_000)
    save_model("Opus 5.5 (1M context)|high")
    assert context_cell(M.live_keyboard("", lang="en", session=SID)) == "📏 250k · 25%"


def test_the_status_line_saves_the_window_size():
    statusline.run(json.dumps({"session_id": SID, "model": {"display_name": "Opus 5.5"},
                               "context_window": {"context_window_size": 200_000}}))
    assert usage.context_window(SID) == 200_000
    for bad in ("200000", 5, None, True):
        statusline.save_window(SID, bad)
    assert usage.context_window(SID) == 200_000
    statusline.save_window("not-a-session", 1_000_000)
    assert usage.context_window("not-a-session") == 0


@pytest.mark.parametrize("cell,kind", [
    ("📏 182k", "context"), ("📏 182k · 18%", "context"), ("📏 1,182 אלף · 100%", "context"), ("📏 182 k", "context"),
    ("📅 41%", "quota"), ("📅 41% · ↻ 14.11", "quota"), ("⏱️ 64%", "quota"), ("⏱️ 70% · 📅 30%", "quota"),
])
def test_info_cells_are_recognised_with_every_value(cell, kind):
    for seen in variants(cell):
        assert I.live_button(seen) == kind, repr(seen)


@pytest.mark.parametrize("typed", ["📏 how big is it now?", "📏 182k is a lot, compact please",
                                   "🧠 " + "think about this for a long while", "👤 someone with a long name",
                                   "📅 tomorrow", "hello", ""])
def test_typed_messages_are_not_taken_for_a_tap(typed):
    assert I.live_button(typed) == ""


def test_tapping_the_context_cell_answers_with_the_real_window(bot, monkeypatch):
    topics = TopicMap(state_dir() / "topics.json", bot, OWNER)
    thread = topics.ensure(SID, "Title")
    monkeypatch.setattr(I, "keyboard_back", lambda *a, **k: None)
    transcript(150_000)
    save_window(200_000)
    msg = {"message_id": 5, "date": int(time.time()), "text": RLM + "📏 150 אלף · 75%", "message_thread_id": thread,
           "chat": {"id": OWNER, "type": "private"}, "from": {"id": OWNER}}
    media = SimpleNamespace(content_of=lambda m: m.get("text") or "")
    assert I.handle({"message": msg}, cfg(), bot, topics, I.Inbox(state_dir() / "inbox"), media, time.time()) == "context"
    assert "150 אלף מתוך 200 אלף (75%)" in bot.methods("sendMessage")[-1]["text"]


@pytest.mark.parametrize("label,result", [("🧠 Opus 5.5", "model"), ("🎚️ גבוהה מאוד", "effort"),
                                          ("🔐 בלי הגבלות", "mode"), ("🎚️ very high", "effort")])
def test_a_tap_on_a_live_label_opens_its_menu(bot, monkeypatch, label, result):
    topics = TopicMap(state_dir() / "topics.json", bot, OWNER)
    thread = topics.ensure(SID, "Title")
    monkeypatch.setattr(I, "keyboard_back", lambda *a, **k: None)
    media = SimpleNamespace(content_of=lambda m: m.get("text") or "")
    for seen in (label, RLM + label, FSI + label + PDI):
        msg = {"message_id": 5, "date": int(time.time()), "text": seen, "message_thread_id": thread,
               "chat": {"id": OWNER, "type": "private"}, "from": {"id": OWNER}}
        assert I.handle({"message": msg}, cfg(), bot, topics, I.Inbox(state_dir() / "inbox"), media, time.time()) == result
        assert I.Inbox(state_dir() / "inbox").take(SID) == []  # never delivered to the session as text


# --- 4. layout: a phone, both languages ------------------------------------------------------------------------

@pytest.mark.parametrize("lang", ["he", "en"])
def test_the_layout_fits_a_phone(lang, monkeypatch):
    monkeypatch.setattr(accounts, "multiple", lambda: True)
    monkeypatch.setattr(accounts, "account_for", lambda s: "work")
    monkeypatch.setattr(accounts, "email", lambda a: "superdeveloper@example.net")
    transcript(950_000)
    save_window(1_000_000)
    save_model("Claude Sonnet 5.5 (1M context)|xhigh")
    direct.record(SID, mode="bypassPermissions")
    kb = M.live_keyboard("\n⏳ 5h 30% · weekly 41% · ↻ ⁦14.11 16:00⁩", lang=lang, session=SID)
    rows = [[b["text"] for b in r] for r in kb["keyboard"]]
    assert [len(r) for r in rows] == [3, 3, 4]  # info, settings, actions (closest to the thumb)
    assert rows[0][0].startswith("📏") and rows[0][1].startswith("📅") and rows[0][2].startswith("👤")
    for row in rows:
        for label in row:
            assert len(label) <= (11 if len(row) == 4 else 17), label
    assert "16:00" not in rows[0][1]  # a later day by its date only


def test_the_account_cell_shows_only_with_more_than_one_account(monkeypatch):
    monkeypatch.setattr(accounts, "account_for", lambda s: "work")
    monkeypatch.setattr(accounts, "email", lambda a: "dev@example.com")
    transcript(10_000)
    assert not any(x.startswith("👤") for x in labels(M.live_keyboard("", lang="he", session=SID)))
    monkeypatch.setattr(accounts, "multiple", lambda: True)
    assert "👤 dev" in labels(M.live_keyboard("", lang="he", session=SID))


# --- 5. freshness: what changes a label changes the card's data key -------------------------------------------

def test_label_changes_move_the_card_data_key(monkeypatch):
    transcript(100_000)
    keys = [M.card_data_key(SID)]
    save_window(200_000)  # the context cell gains its "· 50%"
    keys.append(M.card_data_key(SID))
    save_model("Opus 5.5|high")
    keys.append(M.card_data_key(SID))
    direct.record(SID, mode="plan")
    keys.append(M.card_data_key(SID))
    monkeypatch.setattr(accounts, "multiple", lambda: True)  # the account cell appears
    keys.append(M.card_data_key(SID))
    assert len(set(keys)) == len(keys)
    # every one of them is a label change the mid-turn pace may redraw, like a context step
    assert all(M._only_context_moved(a, b) for a, b in zip(keys, keys[1:]))


# --- 6. review of #218 ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("typed", ["🧠 think again", "🔐 check permissions", RLM + "🧠 think again",
                                   FSI + "🔐 check permissions" + PDI, "🎚️ more please", "👤 who is this",
                                   "📅 50% done?", "⏱️ 3% left, hurry"])
def test_an_icon_at_the_start_of_a_typed_message_is_not_a_tap(typed):
    save_model("Opus 5.5|high")
    assert I.live_button(typed, session=SID) == "" and not I._keyboard_word(typed, session=SID)


def test_a_typed_message_with_a_settings_icon_reaches_the_agent(bot, monkeypatch):
    topics = TopicMap(state_dir() / "topics.json", bot, OWNER)
    thread = topics.ensure(SID, "Title")
    monkeypatch.setattr(I, "keyboard_back", lambda *a, **k: None)
    msg = {"message_id": 5, "date": int(time.time()), "text": "🧠 think again", "message_thread_id": thread,
           "chat": {"id": OWNER, "type": "private"}, "from": {"id": OWNER}}
    media = SimpleNamespace(content_of=lambda m: m.get("text") or "")
    assert I.handle({"message": msg}, cfg(), bot, topics, I.Inbox(state_dir() / "inbox"), media, time.time()) \
        not in ("model", "effort", "mode")


@pytest.mark.parametrize("old", ["🧠 Opus 5.5 (1M context)", RLM + "🎚️ חשיבה", RLM + "🧠 מודל", "🔐 Permissions",
                                 "🧠 Model", "🎚️ Thinking", "🔐 הרשאות", "🎚️ גבוהה מאוד", "🔐 Bypass"])
def test_labels_of_older_keyboards_still_on_phones_are_taps(old):
    assert I.live_button(old) in ("model", "effort", "mode")


def test_the_current_model_of_the_topic_is_a_tap_even_when_not_in_the_table():
    save_model("Opus 4.1|high")
    assert I.live_button("🧠 Opus 4.1", session=SID) == "model"
    assert I.live_button("🧠 Opus 4.1") == ""  # without the topic's session it is not a label we sent


def test_after_a_compaction_without_a_size_the_old_number_is_not_shown():
    path = transcript(380_000)
    with open(path, "a") as f:
        f.write(json.dumps({"type": "system", "subtype": "compact_boundary", "compactMetadata": {}}) + "\n")
    save_window(1_000_000)
    kb = M.live_keyboard("📏 הקשר 380 אלף", lang="he", session=SID)
    assert not any(x.startswith("📏") for x in labels(kb))


def test_window_sizes_written_at_once_are_all_kept():
    import threading
    sessions = [f"0d1a0cbf-0000-4000-8000-{i:012x}" for i in range(80)]
    threads = [threading.Thread(target=statusline.save_window, args=(s, 200_000 + i)) for i, s in enumerate(sessions)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert [usage.context_window(s) for s in sessions] == [200_000 + i for i in range(len(sessions))]


def test_an_unchanged_window_size_is_not_written_again(monkeypatch):
    statusline.save_window(SID, 200_000)
    writes = []
    from ctb import config
    monkeypatch.setattr(config, "write_private", lambda *a, **k: writes.append(a))
    statusline.save_window(SID, 200_000)
    assert writes == []


def effort_rows(tmp_path, monkeypatch, at: float):
    from datetime import datetime, timezone
    stamp = datetime.fromtimestamp(at, timezone.utc).isoformat().replace("+00:00", "Z")
    path = tmp_path / f"{SID}.jsonl"
    rows = [{"type": "assistant", "effort": "high", "timestamp": stamp, "message": {"model": "claude-opus-5-5"}},
            {"type": "user", "timestamp": stamp,
             "message": {"role": "user", "content": "<local-command-stdout>Set effort level to low</local-command-stdout>"}}]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    from ctb import status
    monkeypatch.setattr(status, "transcript_of", lambda s: str(path) if s == SID else None)
    usage._reply_models.clear()


def test_a_level_the_status_line_reported_after_the_effort_command_wins(tmp_path, monkeypatch):
    effort_rows(tmp_path, monkeypatch, time.time() - 60)  # /effort low a minute ago
    statusline.save_model(SID, "Opus 5.5", "high")       # then the status line reported high
    assert usage.session_model(SID) == ("Opus 5.5", "high")


def test_an_effort_command_after_the_status_line_report_wins(tmp_path, monkeypatch):
    statusline.save_model(SID, "Opus 5.5", "high")
    effort_rows(tmp_path, monkeypatch, time.time() + 60)  # /effort low after it
    assert usage.session_model(SID) == ("Opus 5.5", "low")


def test_account_cells_are_recognised_by_the_accounts_there_are(monkeypatch):
    monkeypatch.setattr(accounts, "names", lambda: ["main", "work"])
    monkeypatch.setattr(accounts, "label", lambda n: {"main": "dev", "work": "superdeveloper"}[n])
    for cell in ("👤 dev", "👤 superdeve…", "👤 work"):
        for seen in variants(cell):
            assert I.live_button(seen) == "account", repr(seen)
    assert I.live_button("👤 someone") == ""


# --- 7. second review of #218 -----------------------------------------------------------------------------------

def test_a_label_sent_earlier_is_still_a_tap_after_the_value_changed():
    save_model("Opus 4.1|high")  # a model that is not in the table
    M.live_keyboard("", lang="en", session=SID)  # the keyboard on the phone shows "🧠 Opus 4.1"
    save_model("Opus 5.5|high")  # the session moved on; the old keyboard is still there
    assert I.live_button("🧠 Opus 4.1", session=SID) == "model"
    assert I.live_button("🧠 Opus 4.1", session="0d1a0cbf-0000-4000-8000-0000000000bb") == ""  # another topic's


@pytest.mark.parametrize("bad", ["📏 1,2k", "📏 ,k", "📏 1,2 אלף", "📏 12k · 1000%", "📏 12k · %", "📏 0012,000k"])
def test_only_context_shapes_the_keyboard_makes_are_taps(bad):
    assert I.live_button(bad) == ""


def test_an_odd_context_text_reaches_the_agent(bot, monkeypatch):
    topics = TopicMap(state_dir() / "topics.json", bot, OWNER)
    thread = topics.ensure(SID, "Title")
    monkeypatch.setattr(I, "keyboard_back", lambda *a, **k: None)
    msg = {"message_id": 5, "date": int(time.time()), "text": "📏 1,2k", "message_thread_id": thread,
           "chat": {"id": OWNER, "type": "private"}, "from": {"id": OWNER}}
    media = SimpleNamespace(content_of=lambda m: m.get("text") or "")
    assert I.handle({"message": msg}, cfg(), bot, topics, I.Inbox(state_dir() / "inbox"), media, time.time()) != "context"


def test_tapping_an_old_context_cell_after_an_unmeasured_compaction(bot, monkeypatch):
    topics = TopicMap(state_dir() / "topics.json", bot, OWNER)
    thread = topics.ensure(SID, "Title")
    monkeypatch.setattr(I, "keyboard_back", lambda *a, **k: None)
    path = transcript(380_000)
    with open(path, "a") as f:
        f.write(json.dumps({"type": "system", "subtype": "compact_boundary", "compactMetadata": {}}) + "\n")
    msg = {"message_id": 5, "date": int(time.time()), "text": "📏 380 אלף", "message_thread_id": thread,
           "chat": {"id": OWNER, "type": "private"}, "from": {"id": OWNER}}
    media = SimpleNamespace(content_of=lambda m: m.get("text") or "")
    assert I.handle({"message": msg}, cfg(), bot, topics, I.Inbox(state_dir() / "inbox"), media, time.time()) == "context"
    text = bot.methods("sendMessage")[-1]["text"]
    assert "380" not in text and text == i18n.t("context_info_unknown", lang="he")


def test_the_same_level_reported_again_after_an_effort_command_wins(tmp_path, monkeypatch):
    statusline.save_model(SID, "Opus 5.5", "high")
    times = json.loads((state_dir() / statusline.MODEL_TIMES).read_text())
    times[SID] -= 120  # reported two minutes ago
    write_private(state_dir() / statusline.MODEL_TIMES, json.dumps(times))
    effort_rows(tmp_path, monkeypatch, time.time() - 60)  # then /effort low, a minute ago
    assert usage.session_model(SID) == ("Opus 5.5", "low")
    path = str(tmp_path / f"{SID}.jsonl")
    statusline.run(json.dumps({"session_id": SID, "model": {"display_name": "Opus 5.5"},
                               "effort": {"level": "high"}, "transcript_path": path}))  # high again, now
    assert usage.session_model(SID) == ("Opus 5.5", "high")
    writes = []
    from ctb import config
    monkeypatch.setattr(config, "write_private", lambda *a, **k: writes.append(a))
    statusline.run(json.dumps({"session_id": SID, "model": {"display_name": "Opus 5.5"},
                               "effort": {"level": "high"}, "transcript_path": path}))
    assert not [w for w in writes if "session-models" in str(w[0])]  # nothing new: no write on the next draw


def test_model_reports_written_at_once_keep_value_and_time():
    import threading
    sessions = [f"0d1a0cbf-0000-4000-8000-{i:012x}" for i in range(80)]
    threads = [threading.Thread(target=statusline.save_model, args=(s, "Opus 5.5", "high")) for s in sessions]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    models_ = json.loads((state_dir() / "session-models.json").read_text())
    times = json.loads((state_dir() / statusline.MODEL_TIMES).read_text())
    assert all(models_.get(s) == "Opus 5.5|high" and s in times for s in sessions)
