"""Claude Code permission prompts answered from the phone through the PermissionRequest hook (permission_request.py).

Rule under test: the hook prints a decision only for the owner's explicit answer to that one request, and nothing
otherwise (the terminal prompt stays). No pane is read and no key is pressed. The card has only a No button; a Yes
is a typed yes sent as a reply to that very card (after it, once) or a Cockpit answer, and only for a request shown
exactly and in full (the whole tool_input, every field). A widget never answers.
"""
import io
import json
import threading
import time
from types import SimpleNamespace

import pytest

from ctb import decisions, install
from ctb import inbox as I
from ctb import permission_request as PQ
from ctb.config import Config, state_dir
from ctb.topics import TopicMap

OWNER = 7


@pytest.fixture
def pq(tmp_path, bot, monkeypatch):
    monkeypatch.setattr(PQ, "POLL", 0.01)
    monkeypatch.setattr(I, "react", lambda *a, **k: True)
    cfg = Config(bot_token="t", owner_chat_id=OWNER, ui_language="en")
    topics = TopicMap(state_dir() / "topics.json", bot, OWNER)
    thread = topics.ensure("s", "test")
    return SimpleNamespace(cfg=cfg, bot=bot, topics=topics, thread=thread, box=I.Inbox(tmp_path / "box"))


def payload(command="ls -la docs", tool="Bash", mode="default", event="PermissionRequest", session="s",
            tool_input=None, **extra):
    if tool_input is None:
        tool_input = {"command": command, "description": "List the docs"}
    return json.dumps({"session_id": session, "hook_event_name": event, "tool_name": tool, "tool_input": tool_input,
                       "permission_mode": mode, "cwd": "/tmp/x", **extra})


class Hook:
    """The hook running as Claude Code runs it, in a thread; `out` is what it printed."""

    def __init__(self, pq, stdin, wait=5.0):
        self.out, self.rc = io.StringIO(), None
        self.thread = threading.Thread(target=self._run, args=(pq, stdin, wait), daemon=True)
        self.thread.start()

    def _run(self, pq, stdin, wait):
        self.rc = PQ.run_hook(stdin, cfg=pq.cfg, bot=pq.bot, wait=wait, out=self.out)

    def ident(self, skip=(), timeout=3.0):
        end = time.time() + timeout
        while time.time() < end:
            for k, r in PQ._all().items():
                if k not in skip and r.get("state") == "open" and isinstance(r.get("sent_at"), float):
                    return k
            time.sleep(0.01)
        raise AssertionError("no open request")

    def result(self):
        self.thread.join(10)
        assert not self.thread.is_alive() and self.rc == 0
        text = self.out.getvalue()
        return json.loads(text) if text else None


def card(pq):
    return [p for p in pq.bot.methods("sendMessage") if "reply_markup" in p and "pq:" in json.dumps(p["reply_markup"])][-1]


def datas(c):
    return [b["callback_data"] for row in c["reply_markup"]["inline_keyboard"] for b in row]


def tap(pq, data, user=OWNER, message_id=None, thread=None, ident=None):
    mid = message_id if message_id is not None else PQ.get(ident).get("message_id")
    m = {"chat": {"id": OWNER, "type": "private"}, "message_thread_id": thread or pq.thread, "message_id": mid}
    return I.handle_button({"id": "q", "from": {"id": user}, "message": m, "data": data}, pq.cfg, pq.bot, pq.topics, pq.box)


_next_mid = [10_000]


def say(pq, text, reply_to=None, mid=None, date=None, **extra):
    _next_mid[0] += 1
    m = {"chat": {"id": OWNER, "type": "private"}, "from": {"id": OWNER}, "message_thread_id": pq.thread,
         "message_id": mid if mid is not None else _next_mid[0], "date": date if date is not None else int(time.time()),
         "text": text, **extra}
    if reply_to is not None:
        m["reply_to_message"] = {"message_id": reply_to, "chat": {"id": OWNER}}
    return I.handle({"message": m}, pq.cfg, pq.bot, pq.topics, pq.box, SimpleNamespace(content_of=lambda mm: text), 0)


def reply_yes(pq, ident, word="yes", **kw):
    return say(pq, word, reply_to=PQ.get(ident)["message_id"], **kw)


def decision_of(ident):
    return next(r for r in decisions._load(state_dir() / decisions.STATE_FILE).values() if r.get("perm") == ident)


ALLOW = {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}


@pytest.fixture(autouse=True)
def _fast_mids(pq, monkeypatch):
    """Telegram gives later messages higher ids: the fake bot's ids are its call counts, below the test's own."""
    real = pq.bot.call

    def call(method, params=None, timeout=15):
        out = real(method, params, timeout)
        if method == "sendMessage" and isinstance(out, dict):
            out["message_id"] += 1000
        return out
    pq.bot.call = call


# --- the hook --------------------------------------------------------------------------------------------------

def test_the_card_has_only_no_and_shows_every_field_exactly(pq):
    hook = Hook(pq, payload(tool_input={"command": "ls  -la   docs", "description": "List", "timeout": 5000,
                                        "dangerouslyDisableSandbox": True}))
    ident = hook.ident()
    c = card(pq)
    assert datas(c) == [f"pq:n:{ident}"]  # no Yes button at all
    for field in ('"dangerouslyDisableSandbox": true', '"command": "ls  -la   docs"', '"timeout": 5000', "Bash"):
        assert field in c["text"]
    request = decision_of(ident)["request"]  # the Cockpit gets the same text, never cleaned
    assert '"command": "ls  -la   docs"' in request and '"dangerouslyDisableSandbox": true' in request
    assert decision_of(ident)["decline_only"] is False
    tap(pq, f"pq:n:{ident}", ident=ident)
    hook.result()


def test_a_yes_replied_to_the_card_allows_exactly_this_request(pq):
    hook = Hook(pq, payload())
    ident = hook.ident()
    assert reply_yes(pq, ident, "כן") == "permission-typed-ok"
    assert hook.result() == ALLOW  # no updatedPermissions: never a lasting rule
    assert decision_of(ident)["state"] == "answered" and decision_of(ident)["choice"] == "yes"


def test_the_no_button_denies_with_a_message_and_a_second_tap_does_nothing(pq):
    hook = Hook(pq, payload())
    ident = hook.ident()
    assert tap(pq, f"pq:n:{ident}", ident=ident) == "permission-ok"
    out = hook.result()["hookSpecificOutput"]["decision"]
    assert out["behavior"] == "deny" and "declined" in out["message"]
    assert tap(pq, f"pq:n:{ident}", ident=ident) == "permission-closed"


@pytest.mark.parametrize("data", ["pq:y:0123456789abcdef", "hg!pq:y:0123456789abcdef"])
def test_a_forged_yes_callback_is_refused(pq, data):
    hook = Hook(pq, payload(), wait=0.5)
    hook.ident()
    tap(pq, data, message_id=1)
    assert hook.result() is None


def test_no_answer_prints_nothing_and_the_terminal_prompt_stays(pq):
    hook = Hook(pq, payload(), wait=0.3)
    ident = hook.ident()
    assert hook.result() is None
    assert PQ.get(ident)["state"] == "expired" and decision_of(ident)["state"] == "cancelled"
    assert "still waits at the computer" in pq.bot.methods("editMessageText")[-1]["text"]


@pytest.mark.parametrize("stdin", [
    payload(event="PreToolUse"), payload(mode="dontAsk"), payload(mode="auto"), payload(session="no-topic"),
    "not json", "[]", json.dumps({"hook_event_name": "PermissionRequest"}),
])
def test_other_events_modes_and_sessions_get_no_card_and_no_output(pq, stdin):
    out = io.StringIO()
    assert PQ.run_hook(stdin, cfg=pq.cfg, bot=pq.bot, wait=0.2, out=out) == 0
    assert out.getvalue() == "" and not pq.bot.methods("sendMessage")


def test_a_card_that_cannot_be_sent_never_waits(pq):
    pq.bot.fail["sendMessage"] = "Bad Request"
    out = io.StringIO()
    start = time.time()
    assert PQ.run_hook(payload(), cfg=pq.cfg, bot=pq.bot, wait=5, out=out) == 0
    assert out.getvalue() == "" and time.time() - start < 2


# --- review round 2: a yes is bound to its own card, after it, once ---------------------------------------------

def test_a_yes_without_a_reply_approves_nothing(pq):
    hook = Hook(pq, payload(), wait=0.6)
    hook.ident()
    assert say(pq, "yes") == "permission-typed-refused"
    assert "reply yes to its card" in pq.bot.methods("sendMessage")[-1]["text"]
    assert hook.result() is None


def test_an_old_yes_never_approves_a_new_request(pq):
    first = Hook(pq, payload(command="ls a"), wait=5)
    old = first.ident()
    tap(pq, f"pq:n:{old}", ident=old)
    first.result()
    second = Hook(pq, payload(command="ls b"), wait=0.6)
    new = second.ident(skip={old})
    assert reply_yes(pq, old) is not None  # a reply to the old card: that request is closed
    assert say(pq, "yes", reply_to=PQ.get(new)["message_id"], mid=PQ.get(new)["message_id"] - 1) != "permission-typed-ok"
    assert say(pq, "yes", reply_to=PQ.get(new)["message_id"], date=0) != "permission-typed-ok"  # written before it
    assert second.result() is None and PQ.get(new)["state"] == "expired"


def test_one_message_answers_one_request_once(pq):
    hook = Hook(pq, payload(), wait=5)
    ident = hook.ident()
    assert reply_yes(pq, ident, mid=50_000) == "permission-typed-ok"
    assert hook.result() == ALLOW
    other = Hook(pq, payload(command="ls b"), wait=0.6)
    second = other.ident(skip={ident})
    # the same Telegram message delivered again, now as a reply to the new card, approves nothing
    assert say(pq, "yes", reply_to=PQ.get(second)["message_id"], mid=50_000) != "permission-typed-ok"
    assert other.result() is None


def test_a_no_counts_without_a_reply_only_while_one_request_is_open(pq):
    first = Hook(pq, payload(command="ls a"), wait=1.0)
    a = first.ident()
    second = Hook(pq, payload(command="ls b"), wait=1.0)
    b = second.ident(skip={a})
    assert say(pq, "no") == "permission-typed-refused"  # two open: a word cannot say which
    assert say(pq, "no", reply_to=PQ.get(b)["message_id"]) == "permission-typed-ok"
    assert second.result()["hookSpecificOutput"]["decision"]["behavior"] == "deny"
    assert say(pq, "no") == "permission-typed-ok"
    assert first.result()["hookSpecificOutput"]["decision"]["behavior"] == "deny"


def test_forwarded_and_edited_words_are_not_answers(pq):
    hook = Hook(pq, payload(), wait=0.6)
    ident = hook.ident()
    assert reply_yes(pq, ident, _forwarded=True) != "permission-typed-ok"
    assert reply_yes(pq, ident, edit_date=1) != "permission-typed-ok"
    assert hook.result() is None


# --- review round 2: what the phone can show exactly ----------------------------------------------------------

@pytest.mark.parametrize("command", [
    "echo " + "x" * 1490 + ";rm x",       # the tail would fall past what the Cockpit shows
    "echo " + "<" * 1000,                  # fits as text, not as an escaped Telegram message
    "ls\rrm -rf /important", "ls\tdocs", "ls /tmp/‮evil", "ls docs", "ls​docs",
    "cat ~/.ssh/id_rsa && echo sk-ant-api03-" + "a" * 90,
])
def test_a_request_the_phone_cannot_show_exactly_can_only_be_declined(pq, command):
    hook = Hook(pq, payload(command=command), wait=2.0)
    ident = hook.ident()
    c = card(pq)
    assert "can only decline" in c["text"] and datas(c) == [f"pq:n:{ident}"]
    assert "‮" not in c["text"] and "\r" not in c["text"] and "\t" not in c["text"]
    assert decision_of(ident)["decline_only"] is True
    assert reply_yes(pq, ident) == "permission-typed-refused"
    assert PQ.decide(pq.cfg, pq.bot, decision_of(ident)["id"], "yes") == "refused"
    assert PQ.get(ident)["state"] == "open"
    assert tap(pq, f"pq:n:{ident}", ident=ident) == "permission-ok"
    assert hook.result()["hookSpecificOutput"]["decision"]["behavior"] == "deny"


@pytest.mark.parametrize("command", ["r\\m -rf /important", "s\\udo ls", "cat ~/.e\\nv", "git push origin main"])
def test_shell_spellings_get_no_button_only_an_explicit_reply_approves(pq, command):
    hook = Hook(pq, payload(command=command), wait=2.0)
    ident = hook.ident()
    c = card(pq)
    assert datas(c) == [f"pq:n:{ident}"] and json.dumps(command)[1:-1] in c["text"]
    assert reply_yes(pq, ident) == "permission-typed-ok"
    assert hook.result() == ALLOW


# --- other answer paths ----------------------------------------------------------------------------------------

def test_only_the_owner_on_this_card_in_this_topic_can_tap(pq):
    hook = Hook(pq, payload(), wait=1.0)
    ident = hook.ident()
    assert tap(pq, f"pq:n:{ident}", user=OWNER + 1, ident=ident) == "ignored"
    assert tap(pq, f"pq:n:{ident}", thread=pq.thread + 50, ident=ident) == "ignored"
    assert tap(pq, f"pq:n:{ident}", message_id=12345) == "ignored"
    assert tap(pq, f"pq:n:{'0' * 16}", ident=ident) == "ignored"
    assert hook.result() is None


def test_the_cockpit_answers_through_the_hook_and_a_widget_never_does(pq):
    hook = Hook(pq, payload(command="sudo ls"), wait=2.0)
    ident = hook.ident()
    rec = decision_of(ident)
    with pytest.raises(decisions.DecisionError):
        decisions.answer(pq.cfg, pq.box, rec["id"], "yes")  # never delivered to the session as a message
    from ctb import panel, widgets
    real = pq.bot.call
    pq.bot.call = lambda method, params=None, timeout=15: ({"message_id": 5} if method == "sendWidget"
                                                            else real(method, params, timeout))
    widgets.send_decision(pq.cfg, pq.bot, rec)  # the decision sent as a private-chat widget
    stamp = next(iter(widgets.read_map(widgets._path())))
    query = {"id": "w", "from": {"id": OWNER}, "chat": {"id": OWNER, "type": "private"}, "message_id": 5,
             "type": {"type": "data"}, "data": f"0:{stamp}"}
    reached = []
    real_answer = decisions.answer
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(panel, "stale_reasons_now", lambda cfg: {})  # a fresh decision of a live session
        mp.setattr(decisions, "answer", lambda *a, **k: reached.append(a) or real_answer(*a, **k))
        mp.setattr(PQ, "answer", lambda *a, **k: reached.append(a) or "ok")
        assert widgets.handle_query(query, pq.cfg, pq.bot, pq.box) == "ignored"
    assert reached == [] and PQ.get(ident)["state"] == "open"
    assert PQ.decide(pq.cfg, pq.bot, rec["id"], "yes") == "ok"
    assert hook.result() == ALLOW and decision_of(ident)["state"] == "answered"


def test_the_cockpit_shows_the_exact_request_and_disables_yes_when_it_cannot(pq, monkeypatch):
    from ctb import panel
    hook = Hook(pq, payload(command="ls\tdocs"), wait=1.0)
    ident = hook.ident()
    asked, _ = panel.waiting_for_owner(pq.topics, time.time(), states={"s": "working"})
    (d,) = [x for x in asked if x["id"] == decision_of(ident)["id"]]
    assert d["decline_only"] is True and '"command": "ls\\tdocs"' in d["request"]
    tap(pq, f"pq:n:{ident}", ident=ident)
    hook.result()


# --- closing ---------------------------------------------------------------------------------------------------

def test_a_request_whose_hook_is_gone_takes_no_answer_and_its_card_closes(pq, monkeypatch):
    hook = Hook(pq, payload(), wait=1.0)
    ident = hook.ident()
    monkeypatch.setattr(PQ, "_alive", lambda pid: False)  # answered at the computer: Claude Code ended the hook
    assert reply_yes(pq, ident) == "permission-typed-closed"
    assert PQ.sweep(pq.cfg, pq.bot) == 1
    assert PQ.get(ident)["state"] == "gone" and decision_of(ident)["state"] == "cancelled"
    assert "answered at the computer" in pq.bot.methods("editMessageText")[-1]["text"]
    assert hook.result() is None


def test_the_screen_card_and_the_plain_ring_stay_away_while_the_request_card_is_out(pq, monkeypatch):
    from ctb import mirror
    hook = Hook(pq, payload(), wait=1.0)
    hook.ident()
    sent = len(pq.bot.calls)
    monkeypatch.setattr(mirror.time, "sleep", lambda s: None)
    assert mirror.run_notify(pq.cfg, "s", "permission_prompt", bot=pq.bot) == 0
    assert [m for m, _ in pq.bot.calls[sent:] if m == "sendMessage"] == []
    hook.result()


# --- installation ----------------------------------------------------------------------------------------------

def test_the_hook_is_installed_with_a_timeout_above_its_wait_and_never_in_assistant_sessions():
    entry = install.desired_hooks()["PermissionRequest"]
    (hook,) = entry[0]["hooks"]
    assert install.is_hook_command(hook["command"], "hook-permission") and not hook.get("async")
    assert hook["timeout"] == PQ.HOOK_TIMEOUT == install.PERMISSION_TIMEOUT and PQ.WAIT < PQ.HOOK_TIMEOUT
    from ctb import assistant
    assert "PermissionRequest" not in assistant.settings_dict()["hooks"]


def test_install_and_uninstall_keep_the_owners_other_hooks_in_the_same_group():
    mine = {"type": "command", "command": "/usr/local/bin/my-security-check"}
    ours = install.desired_hooks()["PermissionRequest"][0]["hooks"][0]
    old_stop = {"type": "command", "command": "/x/ctb hook-stop", "timeout": 1}
    settings = {"hooks": {"PermissionRequest": [{"matcher": "Bash", "hooks": [dict(ours, timeout=5), mine]}],
                          "Stop": [{"hooks": [old_stop, mine]}]}}
    new, _ = install.merge_settings(settings)
    groups = new["hooks"]["PermissionRequest"]
    assert {"matcher": "Bash", "hooks": [mine]} in groups and install.desired_hooks()["PermissionRequest"][0] in groups
    assert {"hooks": [mine]} in new["hooks"]["Stop"]
    again, changes = install.merge_settings(new)
    assert again == new and all("unchanged" in c for c in changes)  # stable on the next run
    from ctb import doctor
    assert doctor.hook_drift(new) == []
    gone, _ = install.remove_settings(new)
    assert gone["hooks"]["PermissionRequest"] == [{"matcher": "Bash", "hooks": [mine]}]
    assert gone["hooks"]["Stop"] == [{"hooks": [mine]}]


def test_hidden_letters_and_channel_redaction_make_a_request_decline_only():
    from ctb import permission_request as pr
    _, complete = pr.describe({"command": "touch /tmp/releaseᅠ.txt"})
    assert not complete
    _, complete = pr.describe({"command": "curl -u user:x;touch${IFS}/tmp/m https://example.invalid"})
    assert not complete
    _, complete = pr.describe({"command": "ls -la /tmp"})
    assert complete


def test_a_tool_name_that_redaction_would_hide_is_decline_only(monkeypatch):
    from ctb import creds, permission_request as pr
    monkeypatch.setattr(creds, "redact_channel", lambda text: text.replace("deleteAll", "[redacted]"))
    assert not pr.tool_exact("mcp__backup__deleteAllBackupsV2026")
    assert pr.tool_exact("Bash")
    assert not pr.tool_exact("Ba\u1160sh")


def test_redaction_of_the_joined_cockpit_string_makes_it_decline_only(monkeypatch):
    from ctb import creds, permission_request as pr
    monkeypatch.setattr(creds, "redact_channel", lambda x: x.replace("bearer\n", "[redacted]\n"))
    assert pr.tool_exact("mcp__backup__bearer") and pr.describe({"path": "/important"})[1]
    assert not pr.surfaces_exact("mcp__backup__bearer" + "\n" + pr.describe({"path": "/important"})[0])
    assert pr.surfaces_exact("Bash\n" + pr.describe({"command": "ls"})[0])
