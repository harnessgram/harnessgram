import json
import sys
import threading
import time

import pytest

from ctb import cli, command_topic as CT, config as C, inbox as I
from ctb.topics import TopicMap

CHAT, THREAD, OWNER = -123, 13, 7


@pytest.fixture
def rig(tmp_path, bot):
    cfg = C.from_dict({"owner_chat_id": OWNER, "chat_id": CHAT})
    script = tmp_path / "fake.py"
    script.write_text('import json,sys\nm=json.load(sys.stdin)["message"]\nprint(json.dumps({"reply":m,"action":None,"target":None,"result":None}))\n')
    binding = {"chat": CHAT, "thread": THREAD, "name": "test", "argv": [sys.executable, str(script)],
               "cwd": str(tmp_path), "timeout_s": 2}
    C.write_private(CT._path(), json.dumps({"test": binding}))
    topics = TopicMap(C.state_dir() / "topics.json", bot, CHAT, cfg=cfg)
    return cfg, bot, topics, I.Inbox(C.state_dir() / "inbox"), binding, script


def message(text="hello", **extra):
    m = {"message_id": 42, "date": 2000000000, "text": text, "chat": {"id": CHAT, "type": "supergroup"},
         "message_thread_id": THREAD, "from": {"id": OWNER}}
    m.update(extra)
    return m


def send(rig, m, kind="message"):
    cfg, bot, topics, box, *_ = rig
    return I.handle({kind: m}, cfg, bot, topics, box, None, 0)


def wait_for(bot, count=1):
    end = time.monotonic() + 3
    while len(bot.methods("sendMessage")) < count and time.monotonic() < end:
        threading.Event().wait(.01)
    assert len(bot.methods("sendMessage")) >= count


def test_text_and_yes_never_reach_approval(rig, monkeypatch):
    cfg, bot, topics, box, *_ = rig
    monkeypatch.setattr(I, "_queue_topic_input", lambda *a, **k: pytest.fail("session routing"))
    text = "כן"
    assert send(rig, message(text)) == "command-topic"
    wait_for(bot)
    sent = bot.methods("sendMessage")[0]
    assert sent["text"] == text and "parse_mode" not in sent
    assert box.take("test") == []


@pytest.mark.parametrize("changes,kind", [({"from": {"id": 9}}, "message"), ({"voice": {"file_id": "x"}}, "message"),
    ({"forward_origin": {"type": "user"}}, "message"), ({"forward_date": 4}, "message"),
    ])
def test_refused(rig, changes, kind):
    assert send(rig, message(**changes), kind) == "command-topic-refused"
    assert rig[1].methods("sendMessage")[-1]["text"] == CT.REFUSED


@pytest.mark.parametrize("changes,kind", [({"from": {"id": OWNER, "is_bot": True}}, "message"), ({}, "edited_message")])
def test_bots_and_edits_are_dropped_silently(rig, changes, kind):
    assert send(rig, message(**changes), kind) == "command-topic-refused"
    assert not rig[1].methods("sendMessage")


def test_one_at_a_time(rig):
    rig[5].write_text('import time,json,sys\nx=json.load(sys.stdin)\ntime.sleep(.4)\nprint(json.dumps({"reply":x["message"]}))\n')
    assert send(rig, message()) == "command-topic"
    assert send(rig, message("second")) == "command-topic-busy"
    wait_for(rig[1], 2)
    assert rig[1].methods("sendMessage")[0]["text"] == CT.BUSY


@pytest.mark.parametrize("script,timeout", [('import time; time.sleep(1)', .05), ('print("bad")', 2), ('raise SystemExit(3)', 2)])
def test_errors(rig, script, timeout):
    rig[5].write_text(script)
    rig[4]["timeout_s"] = timeout
    C.write_private(CT._path(), json.dumps({"test": rig[4]}))
    assert send(rig, message()) == "command-topic"
    wait_for(rig[1])
    assert rig[1].methods("sendMessage")[0]["text"] == CT.ERROR


def test_cli_binding(tmp_path, bot, monkeypatch, capsys):
    from ctb.telegram import Bot
    monkeypatch.setattr(C, "load", lambda: C.from_dict({"owner_chat_id": OWNER, "chat_id": CHAT}))
    monkeypatch.setattr(Bot, "from_config", lambda cfg: bot)
    assert cli.main(["command-topic", "add", "ועדת הסינון", "--cwd", str(tmp_path), "--", "python", "-m", "fake"]) == 0
    b = CT.bindings()["ועדת הסינון"]
    assert b["argv"] == ["python", "-m", "fake"] and b["cwd"] == str(tmp_path) and b["timeout_s"] == 60
    assert cli.main(["command-topic", "list"]) == 0
    assert "ועדת הסינון" in capsys.readouterr().out
    assert cli.main(["command-topic", "remove", "ועדת הסינון"]) == 0
    assert not CT.bindings()


def test_long_reply_is_literal_and_chunked(rig, monkeypatch):
    monkeypatch.setattr(CT.time, "sleep", lambda _: None)
    text = "😀<b>" * 2000
    assert send(rig, message(text)) == "command-topic"
    wait_for(rig[1], 2)
    parts = rig[1].methods("sendMessage")
    assert "".join(part["text"] for part in parts) == text
    assert all("parse_mode" not in part for part in parts)


def test_service_messages_edits_and_taps_in_the_topic_stay_silent(monkeypatch):
    sent = []
    monkeypatch.setattr(CT, "bindings", lambda: {"c": {"chat": -1001, "thread": 7, "argv": ["x"], "cwd": "/"}})
    monkeypatch.setattr(CT, "_send", lambda bot, binding, text: sent.append(text))
    deny = lambda m, cfg, started: False
    for update in ({"message": {"chat": {"id": -1001}, "message_thread_id": 7, "pinned_message": {}}},
                   {"edited_message": {"chat": {"id": -1001}, "message_thread_id": 7, "text": "x"}},
                   {"callback_query": {"message": {"chat": {"id": -1001}, "message_thread_id": 7}}}):
        assert CT.handle(update, None, None, 0, deny) == "command-topic-refused"
    assert sent == []


@pytest.mark.parametrize("busy", [False, True])
def test_failed_notice_does_not_escape_listener(rig, monkeypatch, busy, caplog):
    from ctb.telegram import TelegramError
    if busy:
        rig[5].write_text('import time; time.sleep(.4)')
        assert send(rig, message()) == "command-topic"
    def fail(*args, **kwargs):
        raise TelegramError("private detail", 400)
    monkeypatch.setattr(rig[1], "call", fail)
    assert send(rig, message() if busy else message(voice={"file_id": "x"})) == (
        "command-topic-busy" if busy else "command-topic-refused")
    assert "400" in caplog.text and "private detail" not in caplog.text


def test_timeout_kills_descendant_before_unlock(rig, tmp_path):
    marker = tmp_path / "survived"
    rig[5].write_text("import subprocess,sys,time\n"
                      "subprocess.Popen([sys.executable, '-c', sys.argv[1]])\n"
                      "time.sleep(10)\n")
    rig[4]["argv"].append(f'import time,pathlib; time.sleep(.5); pathlib.Path({str(marker)!r}).touch()')
    rig[4]["timeout_s"] = .15
    C.write_private(CT._path(), json.dumps({"test": rig[4]}))
    assert send(rig, message()) == "command-topic"
    wait_for(rig[1])
    time.sleep(.6)
    assert not marker.exists()
    assert rig[1].methods("sendMessage")[0]["text"] == CT.ERROR


@pytest.mark.parametrize("stream,limit", [("stdout", 256 * 1024), ("stderr", 16 * 1024)])
def test_output_limit(rig, stream, limit):
    rig[5].write_text(f'import sys; sys.{stream}.write("x" * {limit + 1}); sys.{stream}.flush()')
    assert send(rig, message()) == "command-topic"
    wait_for(rig[1])
    assert rig[1].methods("sendMessage")[0]["text"] == CT.ERROR


def test_reply_limit_pacing_and_retry(monkeypatch):
    from ctb.telegram import TelegramError
    sent, sleeps, attempts = [], [], []
    monkeypatch.setattr(CT.time, "sleep", sleeps.append)
    monkeypatch.setattr(CT.flood, "remaining", lambda chat: 0)
    def call(bot, method, params, budget):
        attempts.append(params["text"])
        if len(attempts) == 2:
            raise TelegramError("private detail", 429, 7)
        sent.append(params["text"])
    monkeypatch.setattr(CT.flood, "optional_call", call)
    CT._send(None, {"chat": CHAT, "thread": THREAD}, "x" * 65536)
    assert len(sent) == 4 and all(len(part) <= 4000 for part in sent)
    assert sent[-1].endswith(CT.CUT)
    assert sleeps == [3, 7, 3, 3]
    assert attempts[1] == attempts[2]
