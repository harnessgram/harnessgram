"""Team radio lifecycle guards: the radio only ever takes over or ends the live stream it opened itself, a failed
start or a crashing worker never leaves a stream or a job behind, and a failed end is retried later.

The account helper (radio_call.py) runs here against a fake Telethon, so no account, network or secret is used."""
import asyncio
import json
import sys
import types
from types import SimpleNamespace

import pytest

from conftest import GOOD_TOKEN
from ctb import radio, radio_call
from ctb.config import Config

OWNER = 111
GROUP = -1001234


# --- a fake Telethon for radio_call._run --------------------------------------------------------------

class World:
    def __init__(self, active=None, rtmp=True):
        self.active, self.rtmp = active, rtmp
        self.created, self.discarded = [], []


def _fake_telethon(monkeypatch, world: World):
    def request(name):
        def init(self, *a, **k):
            self.a, self.k = a, k
        return type(name, (), {"__init__": init})

    functions = SimpleNamespace(
        channels=SimpleNamespace(GetFullChannelRequest=request("GetFullChannelRequest")),
        phone=SimpleNamespace(GetGroupCallRequest=request("GetGroupCallRequest"),
                              GetGroupCallStreamChannelsRequest=request("GetGroupCallStreamChannelsRequest"),
                              CreateGroupCallRequest=request("CreateGroupCallRequest"),
                              DiscardGroupCallRequest=request("DiscardGroupCallRequest")))

    class Client:
        def __init__(self, *a, **k):
            pass

        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def is_user_authorized(self):
            return True

        async def get_input_entity(self, peer):
            return peer

        async def __call__(self, r):
            name = type(r).__name__
            if name == "GetFullChannelRequest":
                return SimpleNamespace(full_chat=SimpleNamespace(call=world.active))
            if name == "GetGroupCallRequest":
                c = r.k["call"]
                return SimpleNamespace(call=SimpleNamespace(id=c.id, access_hash=c.access_hash, rtmp_stream=world.rtmp,
                                                            participants_count=0, title="", stream_dc_id=None))
            if name == "GetGroupCallStreamChannelsRequest":
                return SimpleNamespace(channels=[])
            if name == "CreateGroupCallRequest":
                world.created.append(r.k)
                world.active = SimpleNamespace(id=777, access_hash=7)
                return None
            if name == "DiscardGroupCallRequest":
                world.discarded.append(r.k["call"].id)
                world.active = None
                return None
            raise AssertionError(name)

    tl = types.ModuleType("telethon")
    tl.TelegramClient, tl.functions = Client, functions
    sessions = types.ModuleType("telethon.sessions")
    sessions.StringSession = lambda s: s
    tltypes = types.ModuleType("telethon.tl.types")
    tltypes.InputGroupCall = lambda id, access_hash: SimpleNamespace(id=id, access_hash=access_hash)
    tltypes.PeerChannel = lambda i: ("channel", i)
    tlpkg = types.ModuleType("telethon.tl")
    for name, mod in (("telethon", tl), ("telethon.sessions", sessions), ("telethon.tl", tlpkg),
                      ("telethon.tl.types", tltypes)):
        monkeypatch.setitem(sys.modules, name, mod)


def _req(action, call=None):
    return {"action": action, "chat": GROUP, "allowed": [GROUP], "title": "radio", "call": call or {},
            "api_command": ["echo", "1:abc"], "session_command": ["echo", "session"]}


def _helper(action, call=None):
    return asyncio.run(radio_call._run(_req(action, call)))


def test_ensure_refuses_someone_elses_live_stream(monkeypatch):
    world = World(active=SimpleNamespace(id=500, access_hash=5))
    _fake_telethon(monkeypatch, world)
    res = _helper("ensure")
    assert res == {"ok": False, "error": radio_call.BUSY} and not world.created
    res = _helper("ensure", call={"id": 499, "access_hash": 4})  # an old call of ours, but another one is live
    assert res["ok"] is False and not world.created


def test_ensure_takes_back_only_the_radios_own_stream(monkeypatch):
    world = World(active=SimpleNamespace(id=500, access_hash=5))
    _fake_telethon(monkeypatch, world)
    res = _helper("ensure", call={"id": 500, "access_hash": 5})
    assert res["ok"] and res["owned"] and res["created"] is False and res["call"]["id"] == 500


def test_ensure_creates_a_stream_it_owns(monkeypatch):
    world = World()
    _fake_telethon(monkeypatch, world)
    res = _helper("ensure")
    assert res["ok"] and res["owned"] and res["created"] and len(world.created) == 1


def test_end_never_ends_another_call(monkeypatch):
    world = World(active=SimpleNamespace(id=500, access_hash=5))
    _fake_telethon(monkeypatch, world)
    res = _helper("end", call={"id": 777, "access_hash": 7})
    assert res["ok"] and res["ended"] is False and world.discarded == []
    res = _helper("end")  # no call of ours at all
    assert res["ok"] and res["ended"] is False and world.discarded == []
    res = _helper("end", call={"id": 500, "access_hash": 5})
    assert res["ended"] and world.discarded == [500]


# --- radio.py: start and stop around the helper --------------------------------------------------------

def _cfg():
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, chat_id=GROUP,
                  radio={"telethon_python": "/usr/bin/python3", "account_api_command": ["x"],
                         "account_session_command": ["y"], "rtmp_url_command": ["u"], "rtmp_key_command": ["k"]})


def _state():
    return json.loads((radio.folder() / "state.json").read_text())


@pytest.fixture
def no_launchd(monkeypatch, tmp_path):
    loaded = {"on": False}
    monkeypatch.setattr(radio.sys, "platform", "darwin")
    monkeypatch.setattr(radio, "job_pid", lambda: None)
    monkeypatch.setattr(radio, "plist_path", lambda: tmp_path / "agents" / "radio.plist")
    monkeypatch.setattr(radio, "_job_loaded", lambda: loaded["on"])

    def launchctl(*args):
        if args[0] == "bootstrap":
            loaded["on"] = True
        elif args[0] == "bootout":
            loaded["on"] = False
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(radio, "_launchctl", launchctl)
    return loaded


def test_start_refuses_when_another_stream_is_live(monkeypatch, no_launchd):
    calls = []
    monkeypatch.setattr(radio, "call_helper", lambda s, action, call=None, **k: calls.append((action, call)) or
                        {"ok": False, "error": radio_call.BUSY})
    with pytest.raises(radio.RadioError) as e:
        radio.start(_cfg())
    assert e.value.key == "radio_call_busy" and calls == [("ensure", None)] and not no_launchd["on"]


def test_stop_never_ends_a_stream_the_radio_did_not_open(monkeypatch, no_launchd):
    """A state file from before ownership was tracked: an adopted (not created) call is never ended."""
    radio._write(radio.folder() / "state.json", {"on": True, "created": False, "call": {"id": 500, "access_hash": 5}})
    monkeypatch.setattr(radio, "call_helper", lambda *a, **k: pytest.fail("the helper was asked to end a foreign call"))
    assert radio.stop(_cfg()) == "radio_stopped"
    assert _state()["call"] == {}


def test_start_passes_the_owned_call_and_stop_ends_it(monkeypatch, no_launchd):
    radio._write(radio.folder() / "state.json", {"on": False, "owned": True, "call": {"id": 777, "access_hash": 7}})
    seen = []

    def helper(s, action, call=None, **k):
        seen.append((action, call))
        if action == "ensure":
            return {"ok": True, "owned": True, "created": False, "call": {"id": 777, "access_hash": 7}}
        return {"ok": True, "ended": True}
    monkeypatch.setattr(radio, "call_helper", helper)
    assert radio.start(_cfg()) == "radio_started"
    assert seen[0] == ("ensure", {"id": 777, "access_hash": 7}) and _state()["owned"] is True
    assert radio.stop(_cfg()) == "radio_stopped"
    assert seen[-1] == ("end", {"id": 777, "access_hash": 7})


# --- a start or a worker that fails part-way leaves nothing running ---------------------------------------

def test_a_start_that_fails_after_the_stream_opened_ends_it(monkeypatch, no_launchd):
    seen = []

    def helper(s, action, call=None, **k):
        seen.append((action, call))
        if action == "ensure":
            return {"ok": True, "owned": True, "created": True, "call": {"id": 777, "access_hash": 7}}
        return {"ok": True, "ended": True}
    monkeypatch.setattr(radio, "call_helper", helper)

    def broken(extra=None):
        raise OSError("disk full")
    monkeypatch.setattr(radio, "plist_bytes", broken)
    with pytest.raises(radio.RadioError) as e:
        radio.start(_cfg())
    assert e.value.key == "radio_job_failed"
    assert seen[-1] == ("end", {"id": 777, "access_hash": 7}) and not no_launchd["on"]
    assert _state()["call"] == {} and _state()["on"] is False


def test_a_start_whose_job_does_not_load_unloads_and_ends(monkeypatch, no_launchd):
    seen = []
    monkeypatch.setattr(radio, "call_helper", lambda s, action, call=None, **k: seen.append(action) or
                        {"ok": True, "owned": True, "created": True, "call": {"id": 777, "access_hash": 7}})
    monkeypatch.setattr(radio, "_launchctl", lambda *a: (_ for _ in ()).throw(OSError("no launchctl")))
    with pytest.raises(radio.RadioError):
        radio.start(_cfg())
    assert seen == ["ensure", "end"]


def test_pipeline_never_raises_on_a_bad_url_or_a_missing_encoder(tmp_path, monkeypatch):
    card = tmp_path / "card.png"
    card.write_bytes(b"x")
    mixer = radio.Mixer(bytes(4))
    pipe = radio.Pipeline("not a url", "key", card, "libx264", mixer, lambda pid: None)
    assert pipe.run() == "publish failed: bad stream URL"

    closed = []

    class Pub:
        def __init__(self, url, key):
            pass

        def connect(self):
            pass

        def close(self):
            closed.append(True)
    from ctb import radio_rtmp
    monkeypatch.setattr(radio_rtmp, "Publisher", Pub)

    def no_ffmpeg(*a, **k):
        raise FileNotFoundError("ffmpeg")
    monkeypatch.setattr(radio.subprocess, "Popen", no_ffmpeg)
    pipe = radio.Pipeline("rtmps://example.org/s/", "key", card, "libx264", mixer, lambda pid: None)
    assert pipe.run() == "pipeline error: FileNotFoundError" and closed == [True]


def test_worker_crashes_count_across_restarts_and_end_in_giving_up(monkeypatch):
    cfg = _cfg()
    monkeypatch.setattr(radio, "make_bed", lambda kind, db: bytes(4))
    ended, booted = [], []
    monkeypatch.setattr(radio, "_end_call", lambda s, call=None: ended.append(True) or True)
    monkeypatch.setattr(radio, "_bootout_self", lambda: booted.append(True))

    def crash(self):
        raise ValueError("boom")
    monkeypatch.setattr(radio.Station, "_run", crash)
    codes = [radio.run_station(cfg) for _ in range(radio.MAX_FAILURES)]  # each one a fresh launchd start
    assert codes[:-1] == [1] * (radio.MAX_FAILURES - 1) and codes[-1] == 0
    assert ended == [True] and booted == [True]
    assert json.loads((radio.folder() / "status.json").read_text())["gave_up"] is True


def test_a_station_that_cannot_even_be_built_still_gives_up(monkeypatch):
    monkeypatch.setattr(radio, "make_bed", lambda kind, db: (_ for _ in ()).throw(RuntimeError("no audio")))
    ended = []
    monkeypatch.setattr(radio, "_end_call", lambda s, call=None: ended.append(True) or True)
    monkeypatch.setattr(radio, "_bootout_self", lambda: None)
    codes = [radio.run_station(_cfg()) for _ in range(radio.MAX_FAILURES)]
    assert codes[-1] == 0 and ended == [True]


# --- a stop that could not end the stream is retried --------------------------------------------------------

def test_a_failed_end_keeps_the_call_for_the_next_stop(monkeypatch, no_launchd):
    radio._write(radio.folder() / "state.json", {"on": True, "owned": True, "call": {"id": 777, "access_hash": 7}})
    answers = [{"ok": False, "error": "helper failed"}, {"ok": True, "ended": True}]
    seen = []
    monkeypatch.setattr(radio, "call_helper", lambda s, action, call=None, **k: seen.append(call) or answers.pop(0))
    assert radio.stop(_cfg()) == "radio_stopped_call_left"
    assert _state()["call"] == {"id": 777, "access_hash": 7} and _state()["cleanup_pending"] is True
    assert radio.status(_cfg())["cleanup_pending"] is True
    assert radio.stop(_cfg()) == "radio_stopped"  # the retry still knows the call
    assert seen == [{"id": 777, "access_hash": 7}] * 2
    assert _state()["call"] == {} and "cleanup_pending" not in _state()


def test_an_unconfigured_helper_keeps_the_call_too(monkeypatch, no_launchd):
    radio._write(radio.folder() / "state.json", {"on": True, "owned": True, "call": {"id": 777, "access_hash": 7}})

    def helper(*a, **k):
        raise radio.RadioError("radio_not_configured")
    monkeypatch.setattr(radio, "call_helper", helper)
    assert radio.stop(_cfg()) == "radio_stopped_call_left"
    assert _state()["call"]["id"] == 777


def test_radio_job_receives_the_diagnostic_setting(monkeypatch):
    """A diagnostic capture is opt-in through CTB_RADIO_DIAG_SECONDS; the launchd job must receive it."""
    import plistlib
    from ctb import radio
    monkeypatch.setenv("CTB_RADIO_DIAG_SECONDS", "60")
    env = plistlib.loads(radio.plist_bytes([]))["EnvironmentVariables"]
    assert env.get("CTB_RADIO_DIAG_SECONDS") == "60"
    monkeypatch.delenv("CTB_RADIO_DIAG_SECONDS")
    assert "CTB_RADIO_DIAG_SECONDS" not in plistlib.loads(radio.plist_bytes([]))["EnvironmentVariables"]
