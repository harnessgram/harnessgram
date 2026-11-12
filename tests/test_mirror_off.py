"""`"mirror": false` turns the bridge off (policy.py): checked at every entry point and every round of the
long-running services, with one backstop in the Telegram client. Off: nothing is sent, nothing is taken from a
queue, no state changes, the update cursor stays. On again: everything resumes without a restart."""
import argparse
import io
import json
import os
import time
import urllib.error
from types import SimpleNamespace

import pytest

from conftest import GOOD_TOKEN, FakeBot
from test_hotfixes import short_relay_state  # noqa: F401  (the fixture)
from ctb import cli, policy, telegram
from ctb import config as C
from ctb.config import state_dir

OWNER, SID = 7, "0f3c1d2e-1111-4222-8333-944455556666"


def write(**raw):
    C.config_path().parent.mkdir(parents=True, exist_ok=True)
    C.config_path().write_text(json.dumps(dict({"bot_token": GOOD_TOKEN, "owner_chat_id": OWNER}, **raw)))


@pytest.fixture(autouse=True)
def _no_thread_left_behind():
    """Every test here stops what it starts: no thread it created may still run after it (a later test such as
    the auto-compact command waits for every other thread to end, and hung on one left here)."""
    import threading
    before = set(threading.enumerate())
    yield
    for t in [t for t in threading.enumerate() if t not in before]:
        t.join(5)  # join, not sleep: a test may have replaced time.sleep
    left = [t.name for t in threading.enumerate() if t not in before and t.is_alive()]
    assert left == []


@pytest.fixture
def switch(real_policy):
    write(mirror=True)
    return SimpleNamespace(on=lambda: write(mirror=True), off=lambda: write(mirror=False))


# --- the switch ----------------------------------------------------------------------------------------------

def test_missing_or_true_is_on_anything_else_is_off(real_policy):
    write()
    assert policy.on() is True  # missing: on, the historical default
    write(mirror=True)
    assert policy.on() is True
    for value in (False, None, "false", "yes", 0, 1):
        write(mirror=value)
        assert policy.on() is False, value
    C.config_path().write_text("{broken")
    assert policy.on() is False  # there but cannot be read as settings: off
    C.config_path().write_text("[]")
    assert policy.on() is False
    write(mirror=True)
    C.config_path().chmod(0)
    try:
        assert policy.on() is False  # there but unreadable: off
    finally:
        C.config_path().chmod(0o600)
    C.config_path().unlink()
    assert policy.on() is True  # no config file at all: a fresh install, on (it has no token to send with)
    assert policy.explicitly_off() is False


def test_the_file_is_read_at_every_call(real_policy):
    import os
    write(mirror=True)
    st = C.config_path().stat()
    on = C.config_path().read_text()
    off = on.replace('"mirror": true', '"mirror":false')  # same length
    C.config_path().write_text(off)
    os.utime(C.config_path(), ns=(st.st_atime_ns, st.st_mtime_ns))
    assert policy.on() is False


# --- the CLI entry -------------------------------------------------------------------------------------------

def _subcommands():
    for action in cli._parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    return set()


def test_every_subcommand_is_local_only_a_service_or_guarded():
    groups = (cli.LOCAL_ONLY, cli.SERVICES, cli.GUARDED)
    assert _subcommands() == set().union(*groups)
    assert all(not (a & b) for i, a in enumerate(groups) for b in groups[i + 1:])


@pytest.mark.parametrize("cmd", sorted(cli.GUARDED))
def test_a_guarded_command_does_nothing_while_off(switch, cmd, capsys):
    switch.off()
    code = cli._off(SimpleNamespace(cmd=cmd))
    err = capsys.readouterr().err
    if cmd.startswith("hook-"):
        assert code == 0 and err == ""  # a hook: silent, nothing taken, nothing changed
    else:
        assert code == 1 and "mirroring is off" in err
    switch.on()
    assert cli._off(SimpleNamespace(cmd=cmd)) is None


def test_a_hook_takes_nothing_and_changes_nothing_while_off(switch, monkeypatch, capsys):
    from ctb import inbox, topics
    inbox.default_inbox().put(SID, {"text": "private words", "at": 1, "id": 5})
    data = json.dumps({"session_id": SID, "prompt": "go", "cwd": "/tmp", "transcript_path": "/nonexistent"})
    switch.off()
    for hook in ("hook-prompt", "hook-post", "hook-stop", "hook-wake"):
        monkeypatch.setattr("sys.stdin", io.StringIO(data))
        assert cli.main([hook]) == 0
    assert capsys.readouterr().out == ""
    assert inbox.default_inbox().pending(SID)
    assert topics.session_state(SID) == {}


def test_a_detached_sender_does_nothing_while_off(switch, monkeypatch, capsys):
    sent = []
    monkeypatch.setattr("ctb.mirror.run_reply", lambda *a, **k: sent.append(a) or 0)
    monkeypatch.setattr("sys.stdin", io.StringIO("hello"))
    switch.off()
    assert cli.main(["mirror", "--title", "t", "--session", SID]) == 1 and sent == []
    switch.on()
    monkeypatch.setattr("sys.stdin", io.StringIO("hello"))
    cli.main(["mirror", "--title", "t", "--session", SID])
    assert sent


def test_pairing_is_refused_while_the_config_says_off(real_policy, capsys):
    pairs = [SimpleNamespace(cmd="pair"), SimpleNamespace(cmd="pair", group=True),
             SimpleNamespace(cmd="project", project_cmd="pair")]
    write(mirror=False)
    for args in pairs:
        assert cli._off(args) == 1  # no Telegram call: pairing would consume the waiting messages
    assert "turn mirror on first" in capsys.readouterr().err
    write()  # a fresh install: no "mirror" yet
    assert all(cli._off(args) is None for args in pairs)
    C.config_path().unlink()  # no config at all
    assert all(cli._off(args) is None for args in pairs)
    write(mirror=True)
    assert cli._off(SimpleNamespace(cmd="project", project_cmd="list")) is None


def test_a_local_command_still_works_while_off(switch, capsys):
    switch.off()
    assert cli._off(SimpleNamespace(cmd="doctor")) is None
    assert cli._off(SimpleNamespace(cmd="radio", radio_cmd="stop")) is None
    assert cli._off(SimpleNamespace(cmd="radio", radio_cmd="start")) == 1


# --- the long-running services -------------------------------------------------------------------------------

class Stop(BaseException):
    pass


T0 = 1_800_000_000.0  # the listener starts here (its own clock: each round adds 10 s)


class Telegram:
    """Telegram's side of the bot: the updates waiting for it, served in order from the requested offset."""

    def __init__(self):
        self.pending, self.next_id = [], 500

    def send(self, text: str, date: float) -> None:
        self.next_id += 1
        self.pending.append({"update_id": self.next_id, "message": {
            "message_id": self.next_id, "date": int(date), "text": text, "from": {"id": OWNER},
            "chat": {"id": OWNER, "type": "private"}}})

    def member(self, date: float) -> None:
        self.next_id += 1
        self.pending.append({"update_id": self.next_id, "my_chat_member": {
            "date": int(date), "chat": {"id": -1009, "type": "channel"}, "from": {"id": OWNER}}})

    def edit(self, text: str, date: float) -> None:
        self.next_id += 1
        self.pending.append({"update_id": self.next_id, "edited_message": {
            "message_id": 1, "date": int(date) - 100, "edit_date": int(date), "text": text, "from": {"id": OWNER},
            "chat": {"id": OWNER, "type": "private"}}})

    def get(self, params: dict) -> list:
        offset = params.get("offset", 0)
        return [u for u in self.pending if u["update_id"] >= offset][:params.get("limit", 100)]


def _run_listener(switch, monkeypatch, script, telegram_side=None, start=T0):
    """Run the real listener loop, deterministically: its clock and its off-pause are its own hooks
    (inbox._clock, inbox._off_pause), so nothing else in the process (a subprocess waiting for its child calls
    time.sleep) can move the test along. One `script` step runs per round (an off-pause, or a long poll while
    on): "off" turns mirroring off; ("on", t) turns it on with the config file's time t (the edit's time);
    ("send", text, date), ("edit", text, date) and ("member", date) put an update into Telegram's queue; a list runs several steps in
    the same round. A long poll with nothing left to do ends the run before it returns (a crash at that point).
    Returns ([(text, delivered)] for the messages and edits handle() got, with the real freshness and off_until
    checks, plus ("member", None) for chat-member updates; the long polls)."""
    import os
    from ctb import inbox as I
    bot, tg, clock = FakeBot(), telegram_side or Telegram(), [start]
    steps, decided, polls = list(script), [], []

    def step():
        if not steps:
            raise Stop()
        todo = steps.pop(0)
        for one in (todo if isinstance(todo, list) else [todo]):
            if one == "off":
                switch.off()
            elif one[0] == "on":
                switch.on()
                os.utime(C.config_path(), (one[1], one[1]))
            elif one[0] == "member":
                tg.member(one[1])
            elif one[0] == "edit":
                tg.edit(one[1], one[2])
            else:
                tg.send(one[1], one[2])
        clock[0] += 10

    def call(method, params=None, timeout=15):
        if method != "getUpdates":
            return FakeBot.call(bot, method, params, timeout)
        polls.append(params or {})
        out = tg.get(params or {})
        step()  # the long poll ends this round
        return out

    def handle(update, cfg, bot, topics, inbox, media, started, team=None, off_until=0.0):
        if "message" in update:
            decided.append((update["message"]["text"], I.from_owner(update["message"], cfg, started)))
        elif "edited_message" in update:
            decided.append((update["edited_message"]["text"], update["edited_message"]["edit_date"] > off_until))
        else:
            decided.append(("member", None))
    bot.call = call
    monkeypatch.setattr(I.Bot, "from_config", classmethod(lambda cls, cfg, strict=False: bot))
    monkeypatch.setattr(I, "relay_serve", lambda cfg, sock_path=None, stop_event=None, bot=None: None)
    monkeypatch.setattr("ctb.sweeper.Sweeper.run", lambda self, stop, tick=0: None)  # tested on its own below
    monkeypatch.setattr(I, "_clock", lambda: clock[0])
    monkeypatch.setattr(I, "_off_pause", lambda seconds: step())
    monkeypatch.setattr(I, "handle", handle)
    with pytest.raises(Stop):
        I.serve(C.load())
    return decided, polls


def _boundary():
    return json.loads((state_dir() / "listener.resume.json").read_text())["boundary"]


def test_the_listener_stops_polling_keeps_its_cursor_and_resumes(switch, monkeypatch):
    cursor = state_dir() / "updates-cursor.json"
    state_dir().mkdir(parents=True, exist_ok=True)
    cursor.write_text(json.dumps({"next_offset": 41}))
    switch.off()
    decided, polls = _run_listener(switch, monkeypatch, ["off", "off", ("on", T0 + 25)])
    assert len(polls) == 1 and polls[0]["offset"] == 41  # nothing was polled (or skipped) while off
    assert json.loads((state_dir() / "listener.status.json").read_text())["state"] == "off"  # its last state


# The off period below: off from the first round (seen at T0+10), switched on at T0+15.7 (the config file's
# time), noticed by the listener at T0+20.
OFF_THEN_ON = ["off", [("send", "before", T0 + 12), ("send", "same second", T0 + 15), ("on", T0 + 15.7),
                       ("send", "after, not yet noticed", T0 + 16)], []]


def test_the_boundary_is_the_second_mirroring_was_switched_on(switch, monkeypatch):
    """Sent before the boundary's second: not delivered; in the same second: not either; one second after
    (before the listener even noticed): delivered."""
    decided, _ = _run_listener(switch, monkeypatch, OFF_THEN_ON)
    assert decided == [("before", False), ("same second", False), ("after, not yet noticed", True)]
    assert _boundary() == T0 + 15.7


def test_a_restart_between_switch_on_and_the_first_poll_keeps_the_boundary(switch, monkeypatch):
    tg = Telegram()
    _run_listener(switch, monkeypatch, OFF_THEN_ON[:2], telegram_side=tg)  # it ends at its first poll after "on"
    assert _boundary() == T0 + 15.7
    # restarted right away, while on: the start rule alone would let "same second" through (five seconds)
    decided, _ = _run_listener(switch, monkeypatch, [[]], telegram_side=tg, start=T0 + 20)
    assert decided == [("before", False), ("same second", False), ("after, not yet noticed", True)]


def test_a_boundary_that_cannot_be_saved_is_retried_and_nothing_is_delivered_meanwhile(switch, monkeypatch):
    from ctb import inbox as I
    real, failures = I.write_private, []

    def write_private(path, text):
        if str(path).endswith("listener.resume.json") and len(failures) < 2:
            failures.append(path)
            raise OSError("disk full")
        return real(path, text)
    monkeypatch.setattr(I, "write_private", write_private)
    decided, polls = _run_listener(switch, monkeypatch, OFF_THEN_ON[:2] + [[], [], []])
    assert len(failures) == 2  # two rounds without a poll, then saved
    assert decided == [("before", False), ("same second", False), ("after, not yet noticed", True)]
    assert len(polls) == 1 + 2  # the poll before "off", and the two after it was saved


def test_a_config_time_in_the_future_falls_back_to_the_last_look_while_off(switch, monkeypatch):
    decided, _ = _run_listener(switch, monkeypatch, [
        "off", [("send", "before the last look", T0 + 9), ("on", T0 + 99999), ("send", "later", T0 + 12)], []])
    assert _boundary() == T0 + 10  # the listener's last look while off
    assert decided == [("before the last look", False), ("later", True)]


def test_a_chat_member_update_in_the_window_is_still_handled(switch, monkeypatch):
    decided, _ = _run_listener(switch, monkeypatch, [
        "off", [("member", T0 + 12), ("send", "before", T0 + 13), ("on", T0 + 15.7)], []])
    assert decided == [("member", None), ("before", False)]


def test_an_edit_made_while_off_is_not_delivered_either(switch, monkeypatch):
    decided, _ = _run_listener(switch, monkeypatch, [
        "off", [("edit", "edited while off", T0 + 15), ("on", T0 + 15.7), ("edit", "edited after", T0 + 16)], []])
    assert decided == [("edited while off", False), ("edited after", True)]


@pytest.mark.parametrize("text", ['{"boundary": Infinity}', '{"boundary": NaN}', '{"boundary": true}',
                                  '{"boundary": "1"}', '[1]', 'not json', '{"boundary": -5}'])
def test_a_resume_file_without_a_time_in_it_is_ignored(text, monkeypatch):
    from ctb import inbox as I
    state_dir().mkdir(parents=True, exist_ok=True)
    (state_dir() / I.RESUME_FILE).write_text(text)
    assert I.load_boundary(state_dir()) == 0.0
    assert I.effective_start(T0, I.load_boundary(state_dir())) == T0


def test_other_waits_in_the_process_do_not_move_the_listener(switch, monkeypatch):
    """What failed on the Linux runner: the start-up stamp runs `ps` and waits for it with time.sleep (as
    subprocess does while the child is not yet reaped). The listener's own pause is its own hook, so those
    waits cannot be taken for listener rounds."""
    from ctb import buildstamp
    import time as clock_module
    monkeypatch.setattr(buildstamp, "_proc_start", lambda pid: [clock_module.sleep(0) for _ in range(6)] and "x")
    decided, _ = _run_listener(switch, monkeypatch, OFF_THEN_ON)
    assert decided == [("before", False), ("same second", False), ("after, not yet noticed", True)]


def test_a_restart_while_on_keeps_the_old_rule(switch, monkeypatch):
    """No off period: a listener started while on keeps the start rule (five seconds of allowance)."""
    tg = Telegram()
    tg.send("stale", T0 - 30)
    tg.send("just before the start", T0 - 2)
    decided, _ = _run_listener(switch, monkeypatch, [[]], telegram_side=tg)
    assert decided == [("stale", False), ("just before the start", True)]


def test_the_delivery_sweeper_takes_nothing_while_off(switch):
    from ctb import inbox as I
    from ctb.sweeper import Sweeper
    I.default_inbox().put(SID, {"text": "private words", "at": 1, "id": 5})
    sw = Sweeper(C.load(), FakeBot(), I.default_inbox())
    switch.off()
    sw.sweep()
    assert I.default_inbox().pending(SID)


def test_the_heartbeat_skips_its_rounds_while_off(switch, monkeypatch):
    from ctb import heartbeat
    rounds = []
    monkeypatch.setattr(heartbeat, "run_once", lambda cfg, bot=None: rounds.append(policy.on()))
    monkeypatch.setattr(heartbeat.revive, "revive_in_background", lambda: None)
    monkeypatch.setattr(heartbeat.Bot, "from_config", classmethod(lambda cls, cfg, strict=False: FakeBot()))
    steps = iter([switch.off, switch.on, None])

    def sleep(seconds):
        step = next(steps)
        if step is None:
            raise Stop()
        step()
    with pytest.raises(Stop):
        heartbeat.run(C.load(), every=1, sleep_fn=sleep)
    assert rounds == [True, True]  # the round while off did nothing


def test_the_panel_answers_every_request_with_the_fixed_off_state(switch, monkeypatch):
    from ctb import panel
    monkeypatch.setattr(panel, "say_to", lambda *a, **k: pytest.fail("an action ran while off"))
    out = []
    handler = panel.PanelHandler.__new__(panel.PanelHandler)
    handler._json = lambda code, obj: out.append((code, obj))
    handler.headers = {}
    switch.off()
    for path in ("/api/state", "/api/say", "/api/screen", "/"):
        handler.path = path
        (handler.do_GET if path == "/" else handler.do_POST)()
    assert out == [(503, panel.MIRROR_OFF)] * 4
    switch.on()
    handler.path = "/nowhere"
    handler.do_POST()
    assert out[-1] == (404, {"error": "not found"})  # on again: requests are routed as before


def test_a_shared_file_stops_mid_download(switch, monkeypatch):
    from ctb import share
    chunks = [b"a" * share.CHUNK, b"b" * share.CHUNK, b"c" * 10]
    stream = SimpleNamespace(read=lambda n: chunks.pop(0) if chunks else b"")
    out = []

    class Out:
        def write(self, data):
            out.append(data)
            if len(out) == 1:
                switch.off()  # switched off after the first chunk went out
    assert share._copy_while_on(stream, Out()) is False
    assert out == [b"a" * share.CHUNK]


def test_the_share_server_refuses_while_off(switch):
    from ctb import share
    errors = []
    handler = share.ShareHandler.__new__(share.ShareHandler)
    handler.path = "/abc/file.txt"
    handler.send_error = errors.append
    switch.off()
    handler.do_GET()
    assert errors == [503]


def test_the_radio_starts_nothing_and_drops_what_it_prepared_while_off(switch, monkeypatch, tmp_path):
    import threading
    from ctb import radio
    st = radio.Station.__new__(radio.Station)
    st.cfg, st.card, st.status = C.load(), tmp_path / "card.png", {}
    st._status = lambda **kw: st.status.update(kw)
    st.mixer = radio.Mixer(bytes(radio.RATE * 2))
    st.mixer.add(b"\x00\x01" * 100)
    st.caption_lock, st.captions, st.headline = threading.Lock(), {1: (0, 0, ["words"])}, ("words", "10:00")
    monkeypatch.setattr(radio.radio_card, "plain", lambda path: None)
    monkeypatch.setattr(radio, "_secret", lambda command: "x")
    monkeypatch.setattr(radio, "pick_encoder", lambda wanted: "libx264")
    monkeypatch.setattr(radio.threading.Thread, "start", lambda self: None)
    monkeypatch.setattr(radio, "Pipeline", lambda *a, **k: pytest.fail("a stream was started while off"))
    st.s = {"rtmp_url_command": ["u"], "rtmp_key_command": ["k"], "video_encoder": "auto", "lang": "en"}
    st.draw, st.gather = (lambda snap: None), (lambda now: {})
    waits = []
    st.quit = SimpleNamespace(is_set=lambda: len(waits) >= 2, wait=lambda s: waits.append(s))
    switch.off()
    assert st._run() == 0
    assert st.mixer.pending() == 0 and st.captions == {} and st.headline == ("", "")

    def prepared(text, cfg, s, cache, usage):
        return b"\x00\x01" * 100, "cache"
    monkeypatch.setattr(radio.radio_voice, "speak", prepared)
    st.cache, st.usage, st.tag = tmp_path, None, 0
    assert st.speak("Work: done") is False and st.mixer.pending() == 0


def test_an_open_radio_stream_stops_once_off(switch, monkeypatch, tmp_path):
    from ctb import radio
    pipe = radio.Pipeline("rtmp://x/", "k", tmp_path / "card.png", "libx264", radio.Mixer(bytes(4)), lambda pid: None)
    monkeypatch.setattr("ctb.radio_rtmp.Publisher", lambda url, key: SimpleNamespace(
        connect=lambda: None, send_flv=lambda out, stop: stop.wait(5), close=lambda: None))
    monkeypatch.setattr(radio.subprocess, "Popen", lambda *a, **k: SimpleNamespace(
        pid=1, stdin=io.BytesIO(), stdout=io.BytesIO(), poll=lambda: None, terminate=lambda: None,
        wait=lambda timeout=None: 0, kill=lambda: None))
    monkeypatch.setattr(radio, "encoder_args", lambda encoder, fd: ["ffmpeg"])
    monkeypatch.setattr(radio, "folder", lambda: tmp_path)
    switch.off()
    assert pipe.run() == radio.MIRROR_OFF


def test_the_wake_waiter_takes_nothing_while_off_and_delivers_once_on(switch, tmp_path):
    from ctb import inbox as I
    from ctb import wake
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("")
    I.default_inbox().put(SID, {"text": "private words", "at": 1, "id": 5})
    data = {"session_id": SID, "transcript_path": str(transcript), "source": "compact"}
    switch.off()
    assert wake.wait_for_message(data, poll=0.01, settle=0, max_wait=0.2) == (0, "")
    assert I.default_inbox().pending(SID)
    switch.on()
    code, text = wake.wait_for_message(data, poll=0.01, settle=0, max_wait=2)
    assert code == 2 and "private words" in text


# --- the backstop --------------------------------------------------------------------------------------------

def test_the_telegram_client_refuses_content_while_off_and_on_every_retry(switch, monkeypatch):
    attempts = []

    class Opener:
        def open(self, req, timeout=0):
            attempts.append(req.full_url.rsplit("/", 1)[-1])
            if attempts[-1] == "getUpdates":
                return io.BytesIO(b'{"ok": true, "result": []}')
            raise urllib.error.HTTPError(req.full_url, 429, "too many", {},
                                         io.BytesIO(b'{"ok":false,"parameters":{"retry_after":1}}'))
    monkeypatch.setattr(telegram, "_opener_for", lambda url: Opener())
    monkeypatch.setattr(telegram.time, "sleep", lambda s: switch.off())
    bot = telegram.Bot(GOOD_TOKEN)
    with pytest.raises(telegram.TelegramError, match="mirroring is off"):
        bot.call("sendMessage", {"chat_id": OWNER, "text": "x"})  # a 429, then the retry after the switch
    assert attempts == ["sendMessage"]
    assert bot.call("getUpdates", {}) == []  # ids only: still allowed
    switch.on()
    with pytest.raises(telegram.TelegramError):
        bot.call("sendMessage", {"chat_id": OWNER, "text": "x"})
    assert attempts.count("sendMessage") >= 2  # on again: it is sent (and Telegram answers)


def test_the_relay_is_covered_by_the_listeners_backstop(switch, monkeypatch, short_relay_state):
    """A session without the token sends through the listener's relay: the listener's own client refuses."""
    from ctb import creds
    from test_hotfixes import OWNER as RELAY_OWNER, running_relay
    sent = []

    class Opener:
        def open(self, req, timeout=0):
            sent.append(req.full_url)
            return io.BytesIO(b'{"ok": true, "result": {"message_id": 1}}')
    monkeypatch.setattr(telegram, "_opener_for", lambda url: Opener())
    switch.off()
    with running_relay(telegram.Bot(GOOD_TOKEN)):
        with pytest.raises(telegram.TelegramError, match="mirroring is off"):
            creds.relay_call({"kind": "api", "method": "sendMessage",
                              "params": {"chat_id": RELAY_OWNER, "text": "x"}})
        switch.on()
        creds.relay_call({"kind": "api", "method": "sendMessage", "params": {"chat_id": RELAY_OWNER, "text": "x"}})
    assert len(sent) == 1


# --- send paths added later: they meet the same backstop ---------------------------------------------------

class _Wire:
    """A real telegram.Bot whose HTTP layer is recorded (so the backstop in Bot._request is what decides)."""

    def __init__(self, monkeypatch):
        self.sent = []

        class Opener:
            def open(opener, req, timeout=0):
                self.sent.append(req.full_url.rsplit("/", 1)[-1])
                return io.BytesIO(b'{"ok": true, "result": {"message_id": 1}}')
        monkeypatch.setattr(telegram, "_opener_for", lambda url: Opener())
        self.bot = telegram.Bot(GOOD_TOKEN)


def test_an_essential_notice_meets_the_backstop_while_off(switch, monkeypatch):
    from ctb import mirror
    from ctb.topics import TopicMap
    wire = _Wire(monkeypatch)
    TopicMap(state_dir() / "topics.json", None, OWNER, cfg=C.load()).ensure(SID, "Work", thread=5)
    switch.off()
    with pytest.raises(telegram.TelegramError, match="mirroring is off"):
        mirror.send_essential(C.load(), wire.bot, SID, {"text": "Claude needs your permission"})
    assert "sendMessage" not in wire.sent
    switch.on()
    mirror.send_essential(C.load(), wire.bot, SID, {"text": "Claude needs your permission"})
    assert "sendMessage" in wire.sent


def test_the_radio_channel_post_meets_the_backstop_while_off(switch, monkeypatch):
    from ctb import radio_post
    from test_alerts_settings import register
    wire = _Wire(monkeypatch)
    register()
    rep = {"done": [{"text": "the login page"}], "asking": [], "decisions": [], "ledger": [], "working": 1,
           "idle": 0}
    switch.off()
    assert radio_post.post(C.load(), rep, "en", {}, bot=wire.bot, clock="10:00") is False
    assert "sendMessage" not in wire.sent
    switch.on()
    assert radio_post.post(C.load(), rep, "en", {}, bot=wire.bot, clock="10:00") is True
    assert "sendMessage" in wire.sent


def test_the_radio_poster_takes_nothing_while_off(switch, monkeypatch):
    from ctb import radio_post
    monkeypatch.setattr(radio_post, "OFF_POLL", 0.05)
    posted = []
    poster = radio_post.Poster(send=lambda cfg, body, **kw: posted.append(body) or True)
    rep = {"done": [{"text": "the login page"}], "asking": [], "decisions": [], "ledger": [], "working": 1,
           "idle": 0}
    switch.off()
    try:
        poster.put(C.load(), rep, "en")
        assert poster.drain(timeout=0.3) is False and posted == [] and len(poster.queue) == 1
        switch.on()
        assert poster.drain(timeout=3) is True and len(posted) == 1
    finally:
        poster.stop()
    assert not poster.thread.is_alive()


# --- Cockpit apps in the panel --------------------------------------------------------------------------------

@pytest.mark.parametrize("verb,path,headers", [
    ("GET", "/app/demo/", {}),
    ("GET", "/app/demo/media/clip.mp4", {"Range": "bytes=0-99"}),
    ("POST", "/app/demo/api", {"Content-Length": "2"}),
], ids=["listing", "media-range", "message-bridge"])
def test_the_cockpit_app_routes_get_the_fixed_off_answer_before_routing(switch, monkeypatch, verb, path, headers):
    from ctb import panel
    for name in ("_app_get", "_app_api", "_media"):
        monkeypatch.setattr(panel.PanelHandler, name, lambda self, *a: pytest.fail("an app route ran while off"))
    out = []
    handler = panel.PanelHandler.__new__(panel.PanelHandler)
    handler._json = lambda code, obj: out.append((code, obj))
    handler.path, handler.headers = path, headers
    switch.off()
    (handler.do_GET if verb == "GET" else handler.do_POST)()
    assert out == [(503, panel.MIRROR_OFF)]


def test_a_cockpit_media_stream_stops_between_blocks_once_off(switch, monkeypatch, tmp_path):
    from ctb import cockpit_apps, panel
    monkeypatch.setattr(cockpit_apps, "READ_BLOCK", 4)
    clip = tmp_path / "clip.bin"
    clip.write_bytes(b"0123456789abcdef")
    written = []

    class Out:
        def write(self, data):
            written.append(data)
            switch.off()  # switched off after the first block went out
    handler = panel.PanelHandler.__new__(panel.PanelHandler)
    handler.headers, handler.wfile, handler.request_version = {}, Out(), "HTTP/1.1"
    handler.send_response = handler.send_header = handler.end_headers = lambda *a, **k: None
    handler._extend = lambda seconds: None
    handler._media(os.open(clip, os.O_RDONLY), "application/octet-stream")
    assert written == [b"0123"]


# --- the running-copy stamp (#224) ----------------------------------------------------------------------------

def test_the_services_record_their_running_copy_while_off_and_send_nothing(switch, monkeypatch):
    """The stamp doctor reads is local bookkeeping: it is written at service start whatever the switch says,
    and writing it sends nothing."""
    from ctb import buildstamp, heartbeat
    recorded = []
    monkeypatch.setattr(buildstamp, "record_running", lambda name, *a, **k: recorded.append(name))
    monkeypatch.setattr(buildstamp, "clear_running", lambda name, *a, **k: recorded.append("-" + name))
    monkeypatch.setattr(heartbeat.revive, "revive_in_background", lambda: None)
    bot = FakeBot()
    monkeypatch.setattr(heartbeat.Bot, "from_config", classmethod(lambda cls, cfg, strict=False: bot))
    switch.off()

    def sleep(seconds):
        raise Stop()
    with pytest.raises(Stop):
        heartbeat.run(C.load(), every=1, sleep_fn=sleep)
    _run_listener(switch, monkeypatch, ["off"])
    assert recorded[:2] == ["heartbeat", "-heartbeat"] and "listener" in recorded
    assert bot.calls == []


def test_recording_the_running_copy_is_local_only(monkeypatch):
    from ctb import buildstamp
    monkeypatch.setattr(telegram.Bot, "call", lambda *a, **k: pytest.fail("the stamp sent something"))
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: pytest.fail("the stamp opened a connection"))
    buildstamp.record_running("heartbeat")
    buildstamp.clear_running("heartbeat")
