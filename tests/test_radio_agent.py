"""Agent media tests use only sandbox files, fake suitability, and in-memory mixers."""
import os
import threading
import time
from pathlib import Path

import pytest

from conftest import GOOD_TOKEN
from ctb import radio, radio_agent as A, radio_podcast as P
from ctb.config import Config


def cfg():
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=7, target_language="he", ui_language="en",
                  radio={"every_minutes": 20})


def media(name="item.mp3", content=b"fake audio"):
    path = Path.home() / name
    path.write_bytes(content)
    return path


@pytest.fixture(autouse=True)
def suitable(monkeypatch):
    monkeypatch.setattr("ctb.radio_suitability.suitable", lambda text: "blocked" not in text)


def test_path_is_resolved_and_must_stay_under_home(tmp_path):
    target = media()
    link = Path.home() / "alias.mp3"
    link.symlink_to(target)
    assert A.check_file(cfg(), link) == target.resolve()
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"audio")
    with pytest.raises(A.AgentMediaError, match="outside"):
        A.check_file(cfg(), outside)


def test_suitability_or_owner_approval_is_required():
    path = media()
    with pytest.raises(A.AgentMediaError, match="notext"):
        A.queue_file(cfg(), path, "Report", "s")
    with pytest.raises(A.AgentMediaError, match="unsuitable"):
        A.queue_file(cfg(), path, "blocked report", "s", "captions")
    item = A.queue_file(cfg(), path, "Report", "s", approved=True)
    assert item["agent"] is True and item["approved"] is True


def test_fingerprint_detects_same_size_replacement():
    path = media(content=b"first")
    item = A.queue_file(cfg(), path, "Report", "s", "safe transcript")
    stamp = path.stat().st_mtime_ns
    path.write_bytes(b"other")
    os.utime(path, ns=(stamp, stamp))
    assert A.unchanged(item) is False


def test_final_guard_rejects_a_changed_file_before_decoder_is_made():
    path = media(content=b"first")
    A.queue_file(cfg(), path, "Report", "s", "safe transcript")
    path.write_bytes(b"other")
    made = []
    mixer = radio.Mixer(bytes(4))
    player = P.Player(mixer, mixer.lock, cfg=cfg(), make=lambda value: made.append(value))
    player.step()
    player.step()
    assert made == [] and P._read()["items"] == []


def test_agent_media_is_attached_only_to_private_mixer_never_public_hls_source():
    """The public HLS encoder reads SpeechMixer; the local item is attached only to PrivateMixer.show."""
    path = media()
    A.queue_file(cfg(), path, "Report", "s", "safe transcript")
    public = radio.SpeechMixer(bytes(4))
    private = radio.PrivateMixer(bytes(4), public)
    feed = type("Feed", (), {"paused": False, "played": 0, "seconds": lambda self: 0,
                              "stop": lambda self: None, "read": lambda self, n: bytes(n)})()
    player = P.Player(private, private.lock, cfg=cfg(), make=lambda value: feed)
    player.step()
    player.step()
    assert private.show is feed
    assert public.show is None and public.show_playing() is False


def test_announcement_precedes_decode():
    A.queue_file(cfg(), media(), "Report", "s", "safe transcript", project="Lingoline")
    said, made, now = [], [], [10.0]
    mixer = radio.Mixer(bytes(4))
    feed = type("Feed", (), {"paused": False, "played": 0, "seconds": lambda self: 0,
                              "stop": lambda self: None})()
    player = P.Player(mixer, mixer.lock, cfg=cfg(), clock=lambda: now[0], announce=said.append,
                      make=lambda value: made.append(value) or feed)
    player.step()
    player.step()
    assert said == ["Next: Report, from Lingoline"] and made == []
    now[0] += 1.1
    player.step()
    assert made == [str(media())]


def test_owner_token_is_single_use_expires_and_is_bound_to_content(monkeypatch):
    path = media(content=b"first")
    token = A.remember("s", path, "Report")
    path.write_bytes(b"other")
    finished = threading.Event()
    calls = []
    monkeypatch.setattr(A, "queue_file", lambda *args, **kwargs: calls.append(args))

    class Bot:
        def call(self, method, fields):
            if method == "answerCallbackQuery":
                finished.set()

    query = {"id": "tap", "data": "rq:" + token}
    assert A.press(query, cfg(), Bot(), "s", True) == "radio-queued"
    assert finished.wait(2) and calls == []
    assert A.press(query, cfg(), Bot(), "s", True) == "ignored"

    expired = A.remember("s", path, "Report")
    now = time.time()
    monkeypatch.setattr(A.time, "time", lambda: now + A.TOKEN_SECONDS + 1)
    assert A.press({"id": "tap2", "data": "rq:" + expired}, cfg(), Bot(), "s", True) == "ignored"


def test_owner_card_attaches_the_media(monkeypatch):
    path = media()
    sent = []
    monkeypatch.setattr("ctb.mirror.send_essential_media",
                        lambda config, bot, session, fields, real: sent.append((fields, real)) or {})
    assert A.ask_owner(cfg(), "s", path, "English title", bot=object()) is True
    assert sent[0][1] == path.resolve()
    assert sent[0][0]["caption"].startswith("🎧")


def test_converted_duplicate_does_not_replace_or_delete_queued_copy(monkeypatch):
    path = media("item.avi")
    conversions = []

    def convert(src, item_id):
        conversions.append(item_id)
        (P.cache_dir() / f"{item_id}.audio").write_bytes(b"converted")

    monkeypatch.setattr(A, "_private_copy", convert)
    first = A.queue_file(cfg(), path, "Report", "s", "safe transcript")
    cached = P.cache_dir() / f"{first['id']}.audio"
    with pytest.raises(A.AgentMediaError, match="dup"):
        A.queue_file(cfg(), path, "Report", "s", "safe transcript")
    assert conversions == [first["id"]] and cached.read_bytes() == b"converted"


def test_conversion_is_restricted_and_button_work_runs_on_worker(monkeypatch):
    path = media("item.avi")
    part_seen = []

    def run(command, **kwargs):
        part_seen.append(command)
        Path(command[-1]).write_bytes(b"converted")
        return type("Done", (), {"returncode": 0})()

    monkeypatch.setattr(A.subprocess, "run", run)
    A._private_copy(path, "0123456789abcdef")
    command = part_seen[0]
    assert command[command.index("-protocol_whitelist") + 1] == "file"
    assert "-format_whitelist" in command

    token = A.remember("s", path, "Report")
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(A, "fingerprint", lambda real: entered.set() or release.wait(2) or {})
    started = time.monotonic()
    assert A.press({"id": "tap", "data": "rq:" + token}, cfg(), type("Bot", (), {"call": lambda *a: None})(),
                   "s", True) == "radio-queued"
    assert time.monotonic() - started < .5 and entered.wait(1)
    release.set()


def test_stop_and_pruning_keep_a_queued_converted_agent_copy():
    item = P.add_local(cfg(), {"id": "abcdefabcdefabcd", "title": "Report", "approved": True})
    cached = P.cache_dir() / (item["id"] + ".audio")
    cached.write_bytes(b"converted")
    old = time.time() - 10 * 86400
    os.utime(cached, (old, old))
    mixer = radio.Mixer(bytes(4))
    player = P.Player(mixer, mixer.lock, cfg=cfg())
    assert cached.exists()  # constructor pruning must respect the queue
    player.step()
    player.stop_all()
    assert cached.exists() and P._read()["items"][0]["id"] == item["id"]
