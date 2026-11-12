"""One source for the reply keyboard under every topic.

The layout of that keyboard is built in exactly one place (mirror.fixed_keyboard, reached through
mirror.current_keyboard and mirror.live_keyboard). These tests keep it that way:

* a static scan: no module names the reply-keyboard shape outside the builder, and the old second builder
  (`reply_keyboard`, the pre-redesign layout) stays gone;
* a runtime sweep: with the builder replaced by a sentinel layout, every sender (replies, the question
  follow-up, the original copy, the status card, its repost after a button, the wake refresh, the card
  refresh, /keyboard) sends only the sentinel layout plus the live info cells;
* stale state: a card, label signature, data key and "already got the keyboard" list written for an older
  layout never make a phone keep, or get, that layout again.
"""
import ast
import json
import re
import time
from pathlib import Path

import pytest

from conftest import GOOD_TOKEN, FakeBot
import ctb
from ctb import i18n, inbox as I, mirror as M, wake
from ctb.config import Config, state_dir, write_private
from ctb.topics import TopicMap

OWNER = 7
SID = "0d1a0cbf-0000-4000-8000-0000000000bb"
SRC = Path(ctb.__file__).parent
SHAPE_WORDS = {"one_time_keyboard", "resize_keyboard", "is_persistent"}
INFO_CELLS = ("📏", "⏱️", "📅", "👤")
MARKS_FILE = "keyboard-topics.json"  # {"layout": id, "topics": [...]}; older builds wrote a bare list


def cfg(lang="he"):
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he", ui_language=lang)


# --- 1. static: the shape of a reply keyboard is named in one place -----------------------------------------------

class Scan(ast.NodeVisitor):
    """Where in the package a reply keyboard is built: (file, function, what)."""

    def __init__(self, name):
        self.name, self.stack, self.found = name, [], []

    def visit_FunctionDef(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def where(self):
        return self.stack[0] if self.stack else "<module>"

    def visit_Constant(self, node):
        if isinstance(node.value, str) and node.value in SHAPE_WORDS:
            self.found.append((self.name, self.where(), node.value))

    def visit_Dict(self, node):
        for key in node.keys:
            if isinstance(key, ast.Constant) and key.value == "keyboard":
                self.found.append((self.name, self.where(), "dict literal with a keyboard key"))
        self.generic_visit(node)

    def visit_Call(self, node):
        if isinstance(node.func, ast.Name) and node.func.id == "dict" and any(k.arg == "keyboard" for k in node.keywords):
            self.found.append((self.name, self.where(), "dict(..., keyboard=...)"))
        self.generic_visit(node)

    def visit_Name(self, node):
        if node.id in ("KEYBOARD", "KEYBOARD_EN"):
            self.found.append((self.name, self.where(), "a layout constant: " + node.id))

    def visit_Attribute(self, node):
        if node.attr in ("KEYBOARD", "KEYBOARD_EN"):
            self.found.append((self.name, self.where(), "a layout constant: ." + node.attr))
        self.generic_visit(node)


def test_the_reply_keyboard_is_built_in_one_place_only():
    found = []
    for path in sorted(SRC.glob("*.py")):
        scan = Scan(path.name)
        scan.visit(ast.parse(path.read_text(encoding="utf-8")))
        found += scan.found
    allowed = {("mirror.py", "fixed_keyboard"), ("mirror.py", "live_keyboard")}
    stray = [f for f in found if (f[0], f[1]) not in allowed]
    assert not stray, f"a reply keyboard is built outside mirror.fixed_keyboard / live_keyboard: {stray}"
    # and the two sites that are allowed do the one thing each
    assert {f[2] for f in found if f[1] == "live_keyboard"} == {"dict(..., keyboard=...)"}
    assert {f[2] for f in found if f[1] == "fixed_keyboard"} >= {"dict literal with a keyboard key"} | SHAPE_WORDS


def test_the_pre_redesign_second_builder_is_gone():
    assert not hasattr(M, "reply_keyboard")
    assert "reply_keyboard" not in {n.name for n in ast.walk(ast.parse((SRC / "mirror.py").read_text(encoding="utf-8")))
                                    if isinstance(n, ast.FunctionDef)}
    # live_keyboard takes no layout from its caller: a sender cannot hand it another one
    assert "base_kb" not in M.live_keyboard.__code__.co_varnames[:M.live_keyboard.__code__.co_argcount]


def test_the_layout_constants_are_the_builders_output_of_the_moment(monkeypatch):
    assert M.KEYBOARD == M.current_keyboard("he") and M.KEYBOARD_EN == M.current_keyboard("en")
    sentinel = {"keyboard": [[{"text": "Z"}]], "is_persistent": False, "one_time_keyboard": True, "resize_keyboard": True}
    monkeypatch.setattr(M, "fixed_keyboard", lambda lang: sentinel)
    assert M.KEYBOARD is sentinel and M.get_keyboard("en") is sentinel


# --- 2. runtime: every sender sends the one layout ---------------------------------------------------------------

def sentinel_layout(lang):
    return {"keyboard": [[{"text": f"ZZ-{lang}-1"}, {"text": f"ZZ-{lang}-2"}], [{"text": f"ZZ-{lang}-3"}]],
            "is_persistent": False, "one_time_keyboard": True, "resize_keyboard": True}


def reply_keyboards(bot, since=0):
    """Every reply keyboard (not an inline one) that went out through the fake bot, with its method."""
    return [(m, p["reply_markup"]) for m, p in bot.calls[since:]
            if isinstance(p.get("reply_markup"), dict) and "keyboard" in p["reply_markup"]]


def assert_only_the_one_layout(sent, lang):
    allowed = {b["text"] for row in sentinel_layout(lang)["keyboard"] for b in row}
    for method, markup in sent:
        texts = [b["text"] for row in markup["keyboard"] for b in row]
        stray = [x for x in texts if x not in allowed and not x.startswith(INFO_CELLS)]
        assert not stray, f"{method} sent a layout of its own: {stray}"
        assert allowed <= set(texts)


class ImmediateTimer:
    def __init__(self, delay, fn):
        self.fn, self.daemon = fn, True

    def start(self):
        self.fn()

    def cancel(self):
        pass


@pytest.fixture
def rig(monkeypatch):
    monkeypatch.setattr(M, "fixed_keyboard", sentinel_layout)
    monkeypatch.setattr(I, "pane_for_session", lambda s: "")
    monkeypatch.setattr(I.threading, "Timer", ImmediateTimer)  # keyboard_back's timer runs at once, on this thread
    bot = FakeBot()
    c = cfg("he")
    topics = TopicMap(state_dir() / "topics.json", bot, OWNER)
    thread = topics.ensure(SID, "Sample topic")
    return bot, c, topics, thread


def forget_keyboard_marks():
    """Start each sender from a topic nothing was sent to: no keyboard marks, no card, no pace."""
    for name in (MARKS_FILE, "status-footer.json", "status-labels.json",
                 M.CARD_DATA_FILE, M.CARD_PACE_FILE):
        (state_dir() / name).unlink(missing_ok=True)


def handle_text(text, thread, c, bot, topics, tmp):
    message = {"message_id": 3, "date": 2_000_000_000, "text": text, "chat": {"id": OWNER, "type": "private"},
               "from": {"id": OWNER}, "message_thread_id": thread}
    media = type("M", (), {"content_of": lambda self, m: m.get("text") or ""})()
    return I.handle({"update_id": 1, "message": message}, c, bot, topics, I.Inbox(tmp / "inbox"), media, 0)


def test_every_sender_sends_the_one_layout(rig, tmp_path):
    bot, c, topics, thread = rig
    mirror = M.Mirror(c, bot, topics)
    footer = "📏 הקשר 40 אלף"
    senders = {
        "first reply": lambda: mirror.send_reply("Sample topic", SID, "Done.", None, footer),
        "question reply (follow-up keyboard)": lambda: mirror.send_reply("Sample topic", SID, "Shall I go on?", None, footer),
        "original copy": lambda: mirror._send_original(SID, "Sample topic", "text", folded=False, reply_keyboard=True),
        "status card, new": lambda: M.status_message(c, thread, footer, M.get_keyboard("he"), bot=bot),
        "status card, repost": lambda: M.status_message(c, thread, footer, M.get_keyboard("he"), bot=bot, repost=True),
        "status card, asked": lambda: M.status_message(c, thread, footer, M.get_keyboard("he"), asked=True, bot=bot),
        "card refresh": lambda: M.refresh_card(c, bot, SID, topics, create=True, force=True),
        "keyboard back after a button": lambda: I.keyboard_back(thread, delay=0, cfg=c, bot=bot),
        "wake refresh": lambda: wake.refresh_status(SID, str(_transcript(tmp_path)), c, bot),
        "/keyboard": lambda: handle_text("/keyboard", thread, c, bot, topics, tmp_path),
    }
    seen = {}
    for name, send in senders.items():
        forget_keyboard_marks()  # the reply path sends its keyboard once per topic
        before = len(bot.calls)
        send()
        seen[name] = reply_keyboards(bot, before)
        assert_only_the_one_layout(seen[name], "he")
    silent = [name for name, sent in seen.items() if not sent]
    assert not silent, f"these senders sent no reply keyboard at all, so the sweep proved nothing for them: {silent}"


def _transcript(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text("")
    return path


def test_the_english_layout_is_the_one_sent_in_the_english_ui(rig):
    bot, c, topics, thread = rig
    c = cfg("en")
    M.status_message(c, thread, "📏 Context 40k", M.get_keyboard("en"), bot=bot)
    assert_only_the_one_layout(reply_keyboards(bot), "en")


# --- 3. stale state never brings an old layout back --------------------------------------------------------------

def legacy_only_labels():
    """The labels of the layouts before the current one that the current one does not have."""
    keys = ("btn_continue", "btn_stop", "btn_escape", "btn_status", "btn_screen", "btn_compact", "btn_permissions",
            "btn_context", "btn_model")
    old = {i18n.t(k, lang=lang) for k in keys for lang in ("he", "en")}
    now = {b["text"] for lang in ("he", "en") for row in M.get_keyboard(lang)["keyboard"] for b in row}
    return old - now


def labels_of(markup):
    return [b["text"] for row in markup["keyboard"] for b in row]


def test_the_old_layouts_have_labels_the_current_one_does_not():
    assert legacy_only_labels(), "the stale-label checks below would prove nothing"


def age_the_state_to_an_older_layout(key, thread):
    """What an older build left in the state folder: a card drawn for another layout (its label signature, its
    data key without today's layout id), the topic marked as having got that layout's keyboard, saved labels and
    footers. Everything a replay could be fed from."""
    base = state_dir()
    old_rows = [[{"text": "⏹️ עצור"}, {"text": "🔐 הרשאות"}, {"text": "🎚️ חשיבה"}],
                [{"text": "🧠 מודל"}, {"text": "🗜️ דחיסה"}, {"text": "⚙️ עוד"}]]
    sigs = json.loads((base / "status-labels.json").read_text(encoding="utf-8"))
    sigs[key] = json.dumps([[b["text"] for b in r] for r in old_rows], ensure_ascii=False)  # no layout id either
    write_private(base / "status-labels.json", json.dumps(sigs, ensure_ascii=False))
    data = json.loads((base / M.CARD_DATA_FILE).read_text(encoding="utf-8"))
    data[key] = re.sub(r"~k[0-9a-f]{8}$", "", data[key])
    write_private(base / M.CARD_DATA_FILE, json.dumps(data, ensure_ascii=False))
    write_private(base / MARKS_FILE, json.dumps({"layout": "an-older-one", "topics": [thread]}))
    write_private(base / "keyboard-labels.json", json.dumps({SID: {"⏹️ עצור מיד": "mode", "🧠 Old model": "model"}}))
    write_private(base / "keyboard.json", json.dumps({"keyboard": old_rows}))  # a layout cached as a file, if one ever is
    write_private(base / "telegram-status-text.json", json.dumps({key: "📏 הקשר 40 אלף"}, ensure_ascii=False))


def test_a_card_drawn_for_an_older_layout_is_drawn_again(monkeypatch):
    monkeypatch.setattr(I, "pane_for_session", lambda s: "")
    bot = FakeBot()
    c = cfg("he")
    topics = TopicMap(state_dir() / "topics.json", bot, OWNER)
    thread = topics.ensure(SID, "Sample topic")
    now = time.time()
    assert M.refresh_card(c, bot, SID, topics, create=True, force=True, now=now)
    key = str(thread)
    first_card = json.loads((state_dir() / "status-footer.json").read_text(encoding="utf-8"))[key]
    assert M.refresh_card(c, bot, SID, topics, turn_end=True, now=now + 1) is False  # nothing moved: quiet
    age_the_state_to_an_older_layout(key, thread)
    bot.calls.clear()

    assert M.refresh_card(c, bot, SID, topics, turn_end=True, now=now + 2) is True, \
        "a card carrying an older layout stayed on the phone"
    posted = [p for m, p in bot.calls if m == "sendMessage"]
    assert len(posted) == 1 and labels_of(posted[0]["reply_markup"])[-len(labels_of(M.get_keyboard("he"))):] \
        == labels_of(M.get_keyboard("he"))
    assert any(m == "deleteMessage" and p["message_id"] == first_card for m, p in bot.calls)
    for _, markup in reply_keyboards(bot):
        assert not set(labels_of(markup)) & legacy_only_labels()


def test_the_next_reply_after_a_layout_change_carries_the_keyboard_again(monkeypatch):
    monkeypatch.setattr(I, "pane_for_session", lambda s: "")
    bot = FakeBot()
    c = cfg("he")
    topics = TopicMap(state_dir() / "topics.json", bot, OWNER)
    thread = topics.ensure(SID, "Sample topic")
    mirror = M.Mirror(c, bot, topics)
    mirror.send_reply("Sample topic", SID, "First.", None, "")
    mirror.send_reply("Sample topic", SID, "Second.", None, "")
    assert len(reply_keyboards(bot)) == 1  # once per topic while the layout stands
    write_private(state_dir() / MARKS_FILE, json.dumps({"layout": "an-older-one", "topics": [thread]}))
    bot.calls.clear()
    mirror.send_reply("Sample topic", SID, "Third.", None, "")
    sent = reply_keyboards(bot)
    assert len(sent) == 1 and labels_of(sent[0][1])[-len(labels_of(M.get_keyboard("he"))):] == labels_of(M.get_keyboard("he"))
    mirror.send_reply("Sample topic", SID, "Fourth.", None, "")
    assert len(reply_keyboards(bot)) == 1  # and again once, not on every reply


def test_the_layout_id_follows_the_layout(monkeypatch):
    before = M.layout_id()
    assert before == M.layout_id()
    monkeypatch.setattr(M, "fixed_keyboard", sentinel_layout)
    assert M.layout_id() != before


def test_no_sender_resends_a_label_of_an_older_layout(tmp_path, monkeypatch):
    """The sweep over real state: every reply keyboard any sender produces has none of the old layouts' labels."""
    monkeypatch.setattr(I, "pane_for_session", lambda s: "")
    for lang in ("he", "en"):
        forget_keyboard_marks()
        bot = FakeBot()
        c = cfg(lang)
        topics = TopicMap(state_dir() / f"topics-{lang}.json", bot, OWNER)
        thread = topics.ensure(SID + lang, "Sample topic")
        mirror = M.Mirror(c, bot, topics)
        mirror.send_reply("Sample topic", SID + lang, "Is it fine?", None, "📏 40k")
        M.status_message(c, thread, "📏 40k", M.get_keyboard(lang), bot=bot)
        sent = reply_keyboards(bot)
        assert sent
        for _, markup in sent:
            assert not set(labels_of(markup)) & legacy_only_labels()


# --- 4. review of the first version: atomic marks, no needless redraw, mark only after a send that went through ---

def test_the_marks_and_their_layout_are_one_file_written_in_one_step(monkeypatch):
    base = state_dir()
    write_private(base / MARKS_FILE, json.dumps({"layout": "an-older-one", "topics": [5, 6]}))
    M.keyboard_sent(7)
    data = json.loads((base / MARKS_FILE).read_text(encoding="utf-8"))
    assert data == {"layout": M.layout_id(), "topics": [7]}  # the old layout's topics are void, the new list is stamped
    assert not (base / "keyboard-layout.json").exists()
    # a write that fails leaves the old file whole: still the old layout, so every topic stays due
    write_private(base / MARKS_FILE, json.dumps({"layout": "an-older-one", "topics": [5, 6]}))
    real = M.write_private

    def crash(path, text):
        raise OSError("disk")
    monkeypatch.setattr(M, "write_private", crash)
    M.keyboard_sent(7)  # best effort: the failure is only logged, nothing raises
    monkeypatch.setattr(M, "write_private", real)
    assert M.keyboard_due(5) and M.keyboard_due(6) and M.keyboard_due(7)


def test_state_without_a_layout_stamp_is_void_so_a_card_with_a_changed_flag_is_not_kept(monkeypatch):
    """A signature, data key or mark list saved before layout ids says nothing about the keyboard flags
    (one_time_keyboard, is_persistent, ...), so it is never taken as current: the card is drawn once more."""
    monkeypatch.setattr(I, "pane_for_session", lambda s: "")
    bot = FakeBot()
    c = cfg("he")
    topics = TopicMap(state_dir() / "topics.json", bot, OWNER)
    thread = topics.ensure(SID, "Sample topic")
    now = time.time()
    assert M.refresh_card(c, bot, SID, topics, create=True, force=True, now=now)
    base, key = state_dir(), str(thread)
    sigs = json.loads((base / "status-labels.json").read_text(encoding="utf-8"))
    sigs[key] = re.sub(r"^[0-9a-f]{8}:", "", sigs[key])  # same labels, no stamp: the flags are unknown
    write_private(base / "status-labels.json", json.dumps(sigs, ensure_ascii=False))
    data = json.loads((base / M.CARD_DATA_FILE).read_text(encoding="utf-8"))
    data[key] = re.sub(r"~k[0-9a-f]{8}$", "", data[key])
    write_private(base / M.CARD_DATA_FILE, json.dumps(data, ensure_ascii=False))
    write_private(base / MARKS_FILE, json.dumps([thread]))
    bot.calls.clear()

    assert M.refresh_card(c, bot, SID, topics, turn_end=True, now=now + 2) is True
    assert len(bot.methods("sendMessage")) == 1 and M.keyboard_due(thread)


def test_a_rate_limited_first_try_keeps_the_keyboard_on_the_retry(rig, monkeypatch):
    bot, c, topics, thread = rig
    from ctb.telegram import TelegramError
    monkeypatch.setattr(M.time, "sleep", lambda s: None)
    real, tries = bot.call, []

    def limited(method, params=None, timeout=15):
        if method == "sendMessage":
            tries.append(dict(params))
            if len(tries) == 1:
                raise TelegramError("Too Many Requests: retry after 1", 429, 1)
        return real(method, params, timeout)
    bot.call = limited
    M.Mirror(c, bot, topics).send_reply("Sample topic", SID, "Hello.", None, "")
    assert len(tries) == 2 and all("keyboard" in t["reply_markup"] for t in tries)
    assert not M.keyboard_due(thread)  # marked once the send went through, not before


def test_a_dropped_first_reply_leaves_the_topic_owed_its_keyboard(rig):
    bot, c, topics, thread = rig
    mirror = M.Mirror(c, bot, topics)
    from ctb import reply_outbox
    from ctb.telegram import TelegramError
    bot.fail["sendMessage"] = TelegramError("Bad Request: chat not found", 400)
    mirror.send_reply("Sample topic", SID, "Lost.", None, "")
    assert M.keyboard_due(thread)
    bot.calls.clear()
    mirror.send_reply("Sample topic", SID, "Next.", None, "")
    assert bot.methods("sendMessage") == [] and M.keyboard_due(thread)
    assert reply_outbox.drain(c, bot) == 2
    assert len(reply_keyboards(bot)) == 1 and not M.keyboard_due(thread)
    assert "Lost." in bot.methods("sendMessage")[0]["text"]
    assert "Next." in bot.methods("sendMessage")[1]["text"]
    assert reply_outbox.drain(c, bot) == 0


# --- 5. review round 2: a marker is only as good as its layout, a failed save never costs the send, one claim ----

def test_a_marker_of_an_older_layout_is_void_in_a_topic_without_a_card(rig, monkeypatch):
    bot, c, topics, thread = rig
    mirror = M.Mirror(c, bot, topics)
    mirror.send_reply("Sample topic", SID, "One.", None, "")
    assert len(reply_keyboards(bot)) == 1
    for old in (json.dumps([thread]),  # a bare list from a build that knew no layouts
                json.dumps({"layout": "an-older-one", "topics": [thread]})):
        write_private(state_dir() / MARKS_FILE, old)
        bot.calls.clear()
        mirror.send_reply("Sample topic", SID, "Again.", None, "")
        assert len(reply_keyboards(bot)) == 1, old
    # and a real change of the layout voids the marker the same way
    monkeypatch.setattr(M, "fixed_keyboard", lambda lang: dict(sentinel_layout(lang), keyboard=[[{"text": "NEW"}]]))
    bot.calls.clear()
    mirror.send_reply("Sample topic", SID, "After the change.", None, "")
    assert [b["text"] for b in reply_keyboards(bot)[0][1]["keyboard"][-1]] == ["NEW"]


def test_a_marker_that_cannot_be_saved_does_not_cost_the_send(rig, monkeypatch):
    bot, c, topics, thread = rig
    real = M.write_private

    def full_disk(path, text):
        if Path(path).name == MARKS_FILE:
            raise OSError(28, "No space left on device")
        return real(path, text)
    monkeypatch.setattr(M, "write_private", full_disk)
    sent = M.Mirror(c, bot, topics).send_reply("Sample topic", SID, "Hello.", None, "")
    assert sent == 1 and len(reply_keyboards(bot)) == 1


def test_two_replies_at_the_same_moment_may_both_carry_the_keyboard(rig):
    """The accepted trade-off of marking only after a confirmed send: no claim is held while a send is on its
    way, so two replies sent at the very same moment may both carry the keyboard. A duplicate keyboard is
    harmless, a lost one is not. Once either is confirmed, later replies carry none."""
    import threading
    bot, c, topics, thread = rig
    real, gate = bot.call, threading.Event()

    def slow(method, params=None, timeout=15):
        if method == "sendMessage":
            gate.wait(2)
        return real(method, params, timeout)
    bot.call = slow
    first = threading.Thread(target=lambda: M.Mirror(c, bot, topics).send_reply("Sample topic", SID, "A.", None, ""))
    second = threading.Thread(target=lambda: M.Mirror(c, bot, topics).send_reply("Sample topic", SID, "B.", None, ""))
    first.start()
    time.sleep(0.2)
    second.start()
    time.sleep(0.2)
    gate.set()
    first.join(5)
    second.join(5)
    assert len(bot.methods("sendMessage")) == 2 and 1 <= len(reply_keyboards(bot)) <= 2
    bot.calls.clear()
    bot.call = real
    M.Mirror(c, bot, topics).send_reply("Sample topic", SID, "C.", None, "")
    assert not reply_keyboards(bot)


def test_a_crash_before_the_send_confirmed_leaves_no_mark_so_the_next_reply_has_the_keyboard(rig):
    bot, c, topics, thread = rig
    real = bot.call

    def dies(method, params=None, timeout=15):
        if method == "sendMessage":
            raise RuntimeError("process killed mid-send")
        return real(method, params, timeout)
    bot.call = dies
    with pytest.raises(RuntimeError):
        M.Mirror(c, bot, topics).send_reply("Sample topic", SID, "Lost.", None, "")
    assert M.keyboard_due(thread)  # no mark was written before the send
    bot.call = real
    bot.calls.clear()
    M.Mirror(c, bot, topics).send_reply("Sample topic", SID, "Next.", None, "")
    assert len(reply_keyboards(bot)) == 1 and not M.keyboard_due(thread)


@pytest.mark.parametrize("error", [OSError("no state folder"), ValueError("embedded null byte")])
def test_the_mark_helpers_never_raise_and_due_fails_open(monkeypatch, error):
    def broken(path):
        raise error
    monkeypatch.setattr(M, "private_dir", broken)
    assert M.keyboard_due(5) is True  # the keyboard is sent, the reply is not blocked
    M.keyboard_sent(5)
    monkeypatch.undo()
    monkeypatch.setattr(M, "_keyboard_mark", broken)  # the mark itself cannot be worked out
    M.keyboard_sent(5)
    assert M.keyboard_due(5) is True


def test_a_card_whose_signature_cannot_be_saved_is_never_posted_again_and_again(monkeypatch):
    monkeypatch.setattr(I, "pane_for_session", lambda s: "")
    bot = FakeBot()
    c = cfg("he")
    topics = TopicMap(state_dir() / "topics.json", bot, OWNER)
    thread = topics.ensure(SID, "Sample topic")
    key, base = str(thread), state_dir()
    M.status_message(c, thread, "", M.get_keyboard("he"), bot=bot)
    first = json.loads((base / "status-footer.json").read_text(encoding="utf-8"))[key]
    sigs = json.loads((base / "status-labels.json").read_text(encoding="utf-8"))
    sigs[key] = "an-old-signature"  # the stored signature is stale: the next card is a genuine replacement
    write_private(base / "status-labels.json", json.dumps(sigs))
    real = M.write_private

    def sig_file_broken(path, text):
        if Path(path).name == "status-labels.json":
            raise OSError(5, "Input/output error")
        return real(path, text)
    monkeypatch.setattr(M, "write_private", sig_file_broken)
    bot.calls.clear()
    M.status_message(c, thread, "", M.get_keyboard("he"), bot=bot)  # replaced, signature not saved: no raise
    second = json.loads((base / "status-footer.json").read_text(encoding="utf-8"))[key]
    assert len(bot.methods("sendMessage")) == 1 and second != first
    assert any(m == "deleteMessage" and p["message_id"] == first for m, p in bot.calls), "the old card stayed"
    bot.calls.clear()
    for _ in range(3):  # the turn-end refreshes that follow
        M.status_message(c, thread, "", M.get_keyboard("he"), bot=bot)
    assert not bot.methods("sendMessage"), "a signature that cannot be saved made every refresh post a card"
    # once the signature can be saved again, the card is replaced as usual and the guard is cleared
    monkeypatch.setattr(M, "write_private", real)
    M.status_message(c, thread, "", M.get_keyboard("he"), bot=bot, repost=True)
    assert len(bot.methods("sendMessage")) == 1
    M.status_message(c, thread, "", M.get_keyboard("he"), bot=bot)
    assert len(bot.methods("sendMessage")) == 1
