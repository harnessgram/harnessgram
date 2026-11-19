"""Button QA harness: every inline button the bridge builds is found in the source, and each one is checked.

How it works:
- The source of src/ctb is read (ast) for every `callback_data` the bridge puts on a button: a fixed string, an
  f-string or a joined string (its fixed start is its *family*), or a value computed elsewhere (DYNAMIC names
  the families it can take).
- Every family must be listed in FAMILIES with a stale sample (a tap on an old card) and its longest real shape.
  A new button whose family is not listed fails here: add it to FAMILIES (and route it in inbox.handle_button).
- Each fixed value and each family sample is tapped by the owner, in a session's topic of the home group, through
  the real dispatcher (inbox.handle_button). The tap must reach a handler (never inbox._unknown_tap) and the handler
  must answer the callback query itself (the 4 s fallback timer is off in tests). The samples carry tokens no
  card has, so this is also the stale-card check: an old button still gets an answer, never silence.
- Every value must fit Telegram's 64 bytes, with the 3-byte mark sensitive buttons get (sensitive_buttons.py).

Fakes only: FakeBot, a private state directory (conftest), and the actions a tap would start (typing into a
terminal, a background thread) replaced by recorders. Nothing reaches tmux, claude, launchctl or the network.
"""
import ast
import threading
import time
from pathlib import Path

import pytest

from conftest import GOOD_TOKEN, FakeBot
from ctb import config as C
from ctb import inbox as I
from ctb.config import state_dir
from ctb.topics import TopicMap

SRC = Path(I.__file__).parent
OWNER, HOME = 7, -1001234567890
SID = "5e1c0a77-0000-4000-8000-0000000000aa"
LIMIT = 64
MARK = 3  # "hg!" (sensitive_buttons.CALLBACK_MARK)


class DeliveryBox:
    def __init__(self, found=True):
        self.found = found

    def set_hold(self, session, mid, hold):
        return self.found

    def delivery_choice(self, session, mid):
        return False if self.found else None


def test_stop_and_send_answers_before_starting_interrupt_work(monkeypatch):
    """The button has an effect beyond answering: it schedules the interrupt worker with the saved queue."""
    events = []

    class ImmediateThread:
        def __init__(self, target, args=(), daemon=None):
            self.target, self.args = target, args

        def start(self):
            # The callback answer must already have happened when slow terminal work is allowed to start.
            assert any(method == "answerCallbackQuery" for method, _ in bot.calls)
            events.append((self.target, self.args))

    monkeypatch.setattr(I.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(I, "_reply_chat", lambda *_a, **_k: HOME)
    monkeypatch.setattr(I, "log_delivery_tap", lambda session, mode, outcome: events.append((mode, outcome)))
    bot = FakeBot()
    cfg = C.Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER)
    box = DeliveryBox()
    q = {"id": "stop-query", "data": "dl:stop:77"}

    assert I.delivery_button(q, cfg, bot, {"message_id": 90}, SID, box) == "delivery"
    assert ("stop", "accepted") in events
    assert any(callable(event[0]) for event in events if isinstance(event, tuple) and len(event) == 2)


@pytest.mark.parametrize("pane,outcome,logged,cleanup", [
    (None, None, "session-not-running", False),
    ("%7", "stopped", "stopped", True),
    ("%7", "idle", "already-idle", True),
    ("%7", "running", "still-running", False),
    ("%7", "failed", "interrupt-failed", False),
    ("%7", "unknown", "interrupt-unconfirmed", False),
    ("%7", "raises", "interrupt-unconfirmed", False),
])
def test_stop_and_send_worker_has_a_visible_safe_outcome(monkeypatch, pane, outcome, logged, cleanup):
    events = []
    monkeypatch.setattr(I, "stop_pane", lambda _session: pane)
    def stop(_pane, _session):
        if outcome == "raises":
            raise OSError("fake tmux failure")
        return (outcome, "") if outcome else pytest.fail("stop called")
    monkeypatch.setattr(I, "stop_turn", stop)
    monkeypatch.setattr(I, "forget_replay", lambda session: events.append(("forget", session)))
    monkeypatch.setattr(I, "_interrupted_idle", lambda cfg, bot, session, box=None: events.append(("deliver", session)))
    monkeypatch.setattr(I, "_delivery_stop_status", lambda cfg, bot, m, mid, text, **kwargs: events.append(("status", text)))
    monkeypatch.setattr(I, "log_delivery_tap", lambda session, mode, result: events.append(("log", mode, result)))
    cfg = C.Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER)

    I._delivery_stop_worker(cfg, FakeBot(), {"message_id": 90}, SID, DeliveryBox())

    assert ("log", "stop", logged) in events
    assert bool([event for event in events if event[0] == "deliver"]) is cleanup
    assert len([event for event in events if event[0] == "status"]) == 1


def test_stale_stop_tap_does_not_start_an_interrupt(monkeypatch):
    started = []
    monkeypatch.setattr(I.threading, "Thread", lambda *a, **k: started.append((a, k)))
    monkeypatch.setattr(I, "_reply_chat", lambda *_a, **_k: HOME)
    monkeypatch.setattr(I, "log_delivery_tap", lambda *_a: None)
    bot = FakeBot()
    cfg = C.Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER)

    assert I.delivery_button({"id": "stale", "data": "dl:stop:77"}, cfg, bot,
                             {"message_id": 90}, SID, DeliveryBox(False)) == "ignored"
    assert not started
    assert any(method == "answerCallbackQuery" for method, _ in bot.calls)

# family (the fixed start of the data) -> (a stale sample to tap, the longest data the bridge really builds)
FAMILIES = {
    "agr:": ("agr:999:0", "agr:999999:9"),
    "agclose:": ("agclose:999", "agclose:999999"),
    "al:": ("al:x:y", "al:" + "a" * 12 + ":" + "b" * 12),
    "alerts:use:": ("alerts:use:-1009999999999", "alerts:use:-1009999999999"),
    "bd:": ("bd:" + "0" * 12 + ":0", "bd:" + "0" * 16 + ":99"),
    "bk:": ("bk:" + "0" * 12 + ":0", "bk:" + "0" * 16 + ":99"),
    "cr:": ("cr:" + "0" * 16 + ":yes", "cr:" + "0" * 16 + ":yes"),
    "cx:": ("cx:" + "0" * 20 + ":1", "cx:" + "0" * 20 + ":99"),
    "dl:now:": ("dl:now:999", "dl:now:9999999999"),
    "dl:hold:": ("dl:hold:999", "dl:hold:9999999999"),
    "dl:cancel:": ("dl:cancel:999", "dl:cancel:9999999999"),
    "dl:stop:": ("dl:stop:999", "dl:stop:9999999999"),
    "dg:e:": ("dg:e:" + "0" * 12, "dg:e:" + "0" * 16),
    "dg:b:": ("dg:b:" + "0" * 12, "dg:b:" + "0" * 16),
    "dg:z:": ("dg:z:" + "0" * 12, "dg:z:" + "0" * 16),
    "dg:d:": ("dg:d:" + "0" * 12, "dg:d:" + "0" * 16),
    "dg:k:": ("dg:k:Escape:" + "0" * 12, "dg:k:Escape:" + "0" * 16),
    "dg:x:": ("dg:x:1:" + "0" * 12, "dg:x:99:" + "0" * 16),
    "dg:t:": ("dg:t:1:" + "0" * 12, "dg:t:99:" + "0" * 16),
    "dg:o:": ("dg:o:1:" + "0" * 12, "dg:o:99:" + "0" * 16),
    "nm:": ("nm:" + "0" * 16 + ":d", "nm:" + "0" * 16 + ":opus"),
    "n:": ("n:" + "0" * 16 + ":0", "n:" + "0" * 16 + ":new"),
    "h:f:": ("h:f:0", "h:f:99"),
    "h:p:": ("h:p:0", "h:p:999"),
    "h:s:": ("h:s:" + "0" * 12, "h:s:" + "0" * 16),
    "g:": ("g:" + "0" * 16 + ":x", "g:" + "0" * 16 + ":s99"),
    "sk:n:": ("sk:n:" + "0" * 24, "sk:n:" + "0" * 24),
    "sk:p:": ("sk:p:0", "sk:p:999"),
    "more:pl:": ("more:pl:999", "more:pl:999"),
    "more:/effort ": ("more:/effort high", "more:/effort medium"),
    "mdef:": ("mdef:menu", "mdef:sonnet"),
    "k:": ("k:1:" + "0" * 12, "k:99:" + "0" * 16),
    "model:": ("model:default", "model:sonnet"),
    "rs:": ("rs:00000000", "rs:00000000"),
    "more:/account": ("more:/account", "more:/account add " + "a" * 20),
    "more:/account remove ": ("more:/account remove ghost", "more:/account remove " + "a" * 20 + " yes"),
    "more:/account move ": ("more:/account move ghost", "more:/account move " + "a" * 20 + " now"),
    "more:/account use ": ("more:/account use ghost", "more:/account use " + "a" * 20),
    "vd:send:": ("vd:send:999", "vd:send:9999999999"),
    "vd:edit:": ("vd:edit:999", "vd:edit:9999999999"),
    "vd:cancel:": ("vd:cancel:999", "vd:cancel:9999999999"),
    "mode:": ("mode:default", "mode:bypassPermissions"),
    "ib:s:": ("ib:s:" + "0" * 12, "ib:s:" + "0" * 16),
    "ib:c:": ("ib:c:" + "0" * 12, "ib:c:" + "0" * 16),
    "cold:": ("cold:" + "0" * 16, "cold:" + "0" * 16),
    "cw:end:": ("cw:end:x", "cw:end:" + "x" * 57),  # inbox only builds it when "cw:now:"+line fits 64 bytes
    "cw:now:": ("cw:now:x", "cw:now:" + "x" * 57),
    "ls:": ("ls:" + "0" * 10, "ls:" + "0" * 10),
    "mb:a:": ("mb:a:99:" + "0" * 8, "mb:a:9999999999:" + "0" * 16),
    "mb:i:": ("mb:i:99:" + "0" * 8, "mb:i:9999999999:" + "0" * 16),
    "mb:d:": ("mb:d:99:" + "0" * 8, "mb:d:9999999999:" + "0" * 16),
    "mb:r:": ("mb:r:99", "mb:r:9999999999"),
    "mv:": ("mv:" + "0" * 16 + ":0", "mv:" + "0" * 16 + ":99"),
    "nt:": ("nt:1:" + "0" * 12, "nt:9:" + "0" * 16),
    "pq:": ("pq:y:" + "0" * 16 + ":0", "pq:y:" + "0" * 16 + ":" + "0" * 16),
    "pq:x:": ("pq:x:" + "0" * 16, "pq:x:" + "0" * 16),
    "pq:n:": ("pq:n:" + "0" * 16, "pq:n:" + "0" * 16),
    "pq:d:": ("pq:d:" + "0" * 16 + ":0", "pq:d:" + "0" * 16 + ":" + "0" * 16),
    "pq:p:a:": ("pq:p:a:" + "0" * 16, "pq:p:a:" + "0" * 16),
    "pq:p:r:": ("pq:p:r:" + "0" * 16, "pq:p:r:" + "0" * 16),
    "pq:p:y:": ("pq:p:y:" + "0" * 16, "pq:p:y:" + "0" * 16),
    "rq:": ("rq:" + "0" * 10, "rq:" + "0" * 10),
    "fork:go:": ("fork:go:" + "0" * 16, "fork:go:" + "0" * 32),
    "more:tmute:": ("more:tmute:ghost", "more:tmute:" + "x" * 53),  # scheduled.menu_rows drops a longer one
    "more:tunmute:": ("more:tunmute:ghost", "more:tunmute:" + "x" * 51),
    "sm:": ("sm:d:000000000000:0o", "sm:x1:" + "0" * 12 + ":99o"),
}

# A `callback_data` whose value is computed away from the button: (file, the expression) -> its families.
DYNAMIC = {
    ("dialogs.py", "data"): ["dg:t:", "dg:o:"],
    ("scheduled.py", "data"): ["more:tmute:", "more:tunmute:"],
    ("menus.py", "CLOSE"): ["more:dismiss"],
    ("inbox.py", "button['callback_data'] + ':' + stamp"): ["k:"],
    ("sessions_menu.py", "yes_cb"): ["sm:"],
    ("sessions_menu.py", "back_cb"): ["sm:"],
}
# Not a button of its own: rewrites the data of a button built (and checked) elsewhere.
NOT_BUILDERS = {("sensitive_buttons.py", "marked")}


def _shapes(node, name):
    """(fixed values, families) of one callback_data expression; None when it cannot be read from the source."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}, set()
    if isinstance(node, ast.JoinedStr):
        first = node.values[0] if node.values else None
        if isinstance(first, ast.Constant) and len(node.values) > 1:
            return set(), {first.value}
        return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = node.left
        while isinstance(left, ast.BinOp):
            left = left.left
        if isinstance(left, ast.Constant) and isinstance(left.value, str):
            return set(), {left.value}
        return None
    if isinstance(node, ast.IfExp):
        a, b = _shapes(node.body, name), _shapes(node.orelse, name)
        if a is None or b is None:
            return None
        return a[0] | b[0], a[1] | b[1]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id == "_cb":  # sessions_menu: "sm:<op>:..."
            return set(), {"sm:"}
        if node.func.id == "finish_login":  # inbox.account_command
            return {"more:/account"}, {"more:/account"}
    return None


def sites():
    """Every callback_data construction site: [(file, line, expression, fixed values, families)]."""
    found = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            values = []
            if isinstance(node, ast.Dict):
                values = [v for k, v in zip(node.keys, node.values)
                          if isinstance(k, ast.Constant) and k.value == "callback_data"]
            elif isinstance(node, ast.keyword) and node.arg == "callback_data":
                values = [node.value]
            for value in values:
                expr = ast.unparse(value)
                if (rel, expr) in NOT_BUILDERS:
                    continue
                if (rel, expr) in DYNAMIC:
                    fams = DYNAMIC[(rel, expr)]
                    found.append((rel, value.lineno, expr, {f for f in fams if f not in FAMILIES},
                                  {f for f in fams if f in FAMILIES}))
                    continue
                shape = _shapes(value, rel)
                if shape is None:
                    found.append((rel, value.lineno, expr, None, None))
                else:
                    found.append((rel, value.lineno, expr, *shape))
    return found


SITES = sites()
UNKNOWN = "unknown-button"
FIXED = sorted({v for *_, fixed, _ in SITES if fixed for v in fixed} - {""})
USED = sorted({f for *_, _, fams in SITES if fams for f in fams})


def test_the_inventory_found_the_buttons():
    assert len(SITES) > 150 and "more:actionexpand" in FIXED and "more:actioncollapse" in FIXED


def test_every_button_is_known_to_the_harness():
    unread = [f"{f}:{line} {expr}" for f, line, expr, fixed, _ in SITES if fixed is None]
    assert not unread, "callback_data the harness cannot read; add it to DYNAMIC:\n" + "\n".join(unread)
    unlisted = sorted({f"{fam!r} ({f}:{line})" for f, line, _, _, fams in SITES for fam in fams
                       if fam not in FAMILIES})
    assert not unlisted, "button families missing from FAMILIES (add a stale sample and the longest shape):\n" \
        + "\n".join(unlisted)
    unused = sorted(set(FAMILIES) - set(USED))
    assert not unused, f"FAMILIES lists buttons the bridge no longer builds: {unused}"


@pytest.mark.parametrize("data", FIXED + [longest for _, longest in FAMILIES.values()])
def test_every_button_fits_telegram_callback_limit(data):
    from ctb.sensitive_buttons import callback_sensitive
    marked = MARK if callback_sensitive(data, "") else 0
    assert len(data.encode("utf-8")) + marked <= LIMIT, data


@pytest.fixture
def rig(monkeypatch):
    bot = FakeBot()
    cfg = C.from_dict({"bot_token": GOOD_TOKEN, "owner_chat_id": OWNER, "chat_id": HOME, "target_language": "en",
                       "ui_language": "en"})
    topics = TopicMap(state_dir() / "topics.json", bot, HOME, cfg=cfg)
    thread = topics.ensure(SID, "work")
    started = []
    # What a tap starts runs at once, inside the test (it is what answers the tap), against a terminal that is
    # never there; typing a command into a session is only recorded.
    monkeypatch.setattr(I, "_terminal_background", lambda key, action: started.append(key) or action() or True)
    monkeypatch.setattr(I, "cli_command", lambda *a, **k: started.append("cli") or "cli")
    monkeypatch.setattr(I, "_tmux", lambda *a, **k: (_ for _ in ()).throw(OSError("no tmux in tests")))
    monkeypatch.setattr(I, "Timer", _NoTimer)
    monkeypatch.setattr(I.threading, "Thread", _NoThread)
    real_unknown = I._unknown_tap
    monkeypatch.setattr(I, "_unknown_tap", lambda *a: started.append(UNKNOWN) or real_unknown(*a))
    monkeypatch.setattr(I, "session_pid", lambda session: None)  # no Claude is running
    monkeypatch.setattr(I, "resume_session", lambda *a, **k: started.append("resume") or False)
    monkeypatch.setattr(I, "wait_for_session", lambda *a, **k: False)
    monkeypatch.setattr(I, "plugin_list", lambda session: [("alpha@example", True)])  # never the real claude
    return cfg, bot, topics, thread, started


class _NoThread:
    """A thread a tap starts runs in the test itself: nothing outlives the test."""

    def __init__(self, target=None, args=(), kwargs=None, **_):
        self._run = lambda: target(*args, **(kwargs or {})) if target else None

    def start(self):
        self._run()

    def join(self, *a, **k):
        pass


class _NoTimer:
    """A central-feedback timer that stays dormant in the all-buttons fake harness."""

    def __init__(self, interval, function, args=(), kwargs=None):
        self.function = function
        self.daemon = False

    def start(self):
        pass

    def cancel(self):
        pass


def tap(rig, data, thread=None):
    cfg, bot, topics, own, started = rig
    started.clear()
    thread = own if thread is None else thread
    bot.calls.clear()
    q = {"id": "qa", "from": {"id": OWNER, "is_bot": False}, "data": data,
         "message": {"message_id": 4242, "date": 2_000_000_000, "text": "⚙️ menu",
                     "chat": {"id": HOME, "type": "supergroup"}, "message_thread_id": thread}}
    watch = I._TapWatch(bot)
    result = I.handle_button(q, cfg, watch, topics, I.Inbox(state_dir() / "inbox"))
    return (UNKNOWN if UNKNOWN in started else result), watch.answered


TAPS = FIXED + sorted({sample for pair in FAMILIES.values() for sample in pair})
GONE_THREAD = 999  # a topic whose session the bridge no longer knows: every card in it is stale


@pytest.mark.parametrize("data", [sample for sample, _ in FAMILIES.values()])
def test_every_callback_family_gets_central_feedback(data):
    """Fast outer callback routes answer once without spending Telegram edits."""
    bot = FakeBot()
    q = {"id": "qa", "data": data,
         "message": {"message_id": 4242, "chat": {"id": HOME}, "reply_markup": {
             "inline_keyboard": [[{"text": "Action", "callback_data": data}]]}}}
    feedback = I._ButtonFeedback(bot, q)
    feedback.done()
    assert bot.calls[0][0] == "answerCallbackQuery"
    assert bot.calls[0][1]["text"]
    assert len(bot.methods("answerCallbackQuery")) == 1
    assert not bot.methods("editMessageReplyMarkup")


def _feedback(data="go", keyboard=True):
    bot = FakeBot()
    message = {"message_id": 4242, "chat": {"id": HOME}}
    if keyboard:
        message["reply_markup"] = {"inline_keyboard": [[
            {"text": "Other", "callback_data": "other"}, {"text": "Action", "callback_data": data}]]}
    return bot, I._ButtonFeedback(bot, {"id": "qa", "data": data, "_ui_language": "en", "message": message})


def test_handler_answer_wins_and_exactly_one_answer_is_sent():
    bot, feedback = _feedback()
    feedback.call("answerCallbackQuery", {"callback_query_id": "qa", "text": "Already sent"})
    feedback._answer_while_running()
    feedback.done()
    assert bot.methods("answerCallbackQuery") == [{"callback_query_id": "qa", "text": "Already sent"}]


def test_background_handler_keeps_ownership_of_callback_answer():
    """A real worker that starts after dispatch returns still supplies the one useful toast."""
    bot, feedback = _feedback()
    release = threading.Event()
    finished = threading.Event()

    def handler():
        release.wait(2)
        feedback.call("answerCallbackQuery", {"callback_query_id": "qa", "text": "Queued"})
        finished.set()

    worker = threading.Thread(target=feedback.defer(handler))
    worker.start()
    feedback.done()
    assert not bot.methods("answerCallbackQuery")
    release.set()
    assert finished.wait(2)
    worker.join(2)
    assert bot.methods("answerCallbackQuery") == [{"callback_query_id": "qa", "text": "Queued"}]


def test_slow_handler_answer_is_not_replaced_by_working_toast(monkeypatch):
    """Elapsed wall time past the former answer deadline cannot consume the handler's answer."""
    monkeypatch.setattr(I, "BUTTON_ANSWER_DELAY", 0.02)
    bot, feedback = _feedback()
    time.sleep(0.06)
    feedback.call("answerCallbackQuery", {"callback_query_id": "qa", "text": "Specific result"})
    feedback.done()
    assert bot.methods("answerCallbackQuery") == [{"callback_query_id": "qa", "text": "Specific result"}]


def test_params_keyword_tracks_answers_and_same_message_edits():
    bot, feedback = _feedback()
    feedback.call("editMessageText", params={"chat_id": HOME, "message_id": 4242, "text": "Changed"})
    feedback.call("answerCallbackQuery", params={"callback_query_id": "qa", "text": "Saved"})
    feedback.done()
    assert bot.methods("answerCallbackQuery") == [{"callback_query_id": "qa", "text": "Saved"}]
    assert not bot.methods("editMessageReplyMarkup")


def test_slow_handler_marks_only_tapped_button_then_restores_label():
    bot, feedback = _feedback()
    feedback._mark_while_running()
    feedback.done()
    edits = bot.methods("editMessageReplyMarkup")
    assert [[button["text"] for button in row] for row in edits[0]["reply_markup"]["inline_keyboard"]] == \
        [["Other", "⏳ Action"]]
    assert [[button["text"] for button in row] for row in edits[1]["reply_markup"]["inline_keyboard"]] == \
        [["Other", "Action"]]


def test_voice_preview_send_edit_is_never_overwritten():
    bot, feedback = _feedback("vd:send:1")
    feedback._mark_while_running()
    feedback.call("editMessageText", {"chat_id": HOME, "message_id": 4242, "text": "Sent",
                                      "reply_markup": {"inline_keyboard": []}})
    feedback.done()
    assert len(bot.methods("editMessageReplyMarkup")) == 1
    assert bot.methods("editMessageText")[-1]["reply_markup"] == {"inline_keyboard": []}


def test_no_keyboard_never_adds_or_edits_buttons():
    bot, feedback = _feedback(keyboard=False)
    feedback._mark_while_running()
    feedback.done()
    assert not bot.methods("editMessageReplyMarkup")


def test_fast_tap_makes_zero_extra_edits():
    bot, feedback = _feedback()
    feedback.done()
    assert not bot.methods("editMessageReplyMarkup")


@pytest.mark.parametrize("where", ["session-topic", "stale-topic"])
@pytest.mark.parametrize("data", TAPS)
def test_every_button_reaches_a_handler_that_answers(rig, data, where):
    if data.startswith("cx:"):
        pytest.skip("Codex app-server buttons are routed by inbox.handle to codex_bridge (tests/test_codex_*)")
    result, answered = tap(rig, data, thread=GONE_THREAD if where == "stale-topic" else None)
    assert result != UNKNOWN, f"{data!r}: no handler routes this button"
    assert answered, f"{data!r}: the handler returned {result!r} without answering the tap"
    answers = rig[1].methods("answerCallbackQuery")
    assert answers and all(answer.get("text") for answer in answers), f"{data!r}: empty toast"


def test_the_move_menu_parses_every_index_it_draws():
    from ctb import moves
    assert all(moves.CALLBACK.fullmatch(f"mv:{'0' * 16}:{i}") for i in range(100))
    assert not moves.CALLBACK.fullmatch(f"mv:{'0' * 16}:01")


def test_an_unrouted_button_is_caught(rig):
    result, answered = tap(rig, "zz:new-button-nobody-routed")
    assert result == UNKNOWN and answered
    result, answered = tap(rig, "more:new-button-nobody-routed")
    assert result == UNKNOWN and answered


def test_a_tap_in_a_command_topic_is_answered(monkeypatch):
    from ctb import command_topic as CT
    monkeypatch.setattr(CT, "bindings", lambda: {"c": {"chat": HOME, "thread": 7, "argv": ["x"], "cwd": "/"}})
    bot = FakeBot()
    update = {"callback_query": {"id": "qa", "data": "more:dismiss", "from": {"id": OWNER},
                                 "message": {"chat": {"id": HOME}, "message_thread_id": 7}}}
    assert CT.handle(update, C.Config(owner_chat_id=OWNER), bot, 0, lambda m, cfg, started: True) == \
        "command-topic-refused"
    assert [p["callback_query_id"] for p in bot.methods("answerCallbackQuery")] == ["qa"]
    assert not bot.methods("sendMessage")
