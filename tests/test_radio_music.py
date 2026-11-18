import json
import time
from types import SimpleNamespace

import pytest

from ctb import cli, radio_music as M, radio_suitability
from ctb.config import Config


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path / "state"))
    checker = tmp_path / "check.py"
    checker.write_text('import sys\nsys.exit(1 if "forbidden" in open(sys.argv[1]).read() else 0)\n')
    monkeypatch.setattr(radio_suitability, "SHARED_CHECKER", checker)
    monkeypatch.setattr(M, "_probe", lambda path: {})


def test_both_suitability_checks_and_missing_checker_fail_closed(monkeypatch):
    rejected = M.add(b"audio", "forbidden", "Artist")
    assert not rejected["checked_ok"] and not (M.folder() / (rejected["id"] + ".audio")).exists()
    rejected = M.add(b"audio", "murder", "Artist")
    assert not rejected["checked_ok"]
    monkeypatch.setattr(radio_suitability, "SHARED_CHECKER", M.folder() / "missing")
    rejected = M.add(b"audio", "Song", "Artist")
    assert not rejected["checked_ok"] and "אינו זמין" in rejected["reason"]


def test_video_or_unknown_rejected(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(M.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=json.dumps({
        "streams": [{"codec_type": "audio"}, {"codec_type": "video"}]}).encode()))
    with pytest.raises(ValueError):
        M._probe("dummy")


def test_pick_prefers_likes_and_avoids_skips_and_all_daily_repeats(monkeypatch):
    items = [M.add(b"audio", "Song", "Artist", now=n) for n in range(12)]
    M.like(items[10]["id"], now=20)
    M.skip(items[11]["id"], now=20)
    for n, item in enumerate(items[:10]):
        with M.locked_map(M.folder() / "index.json", keep=100000) as index:
            index[item["id"]]["play_times"] = [n + 1]
    class Choice:
        def choices(self, candidates, weights, k):
            assert [i["id"] for i in candidates] == [items[10]["id"]]
            return [candidates[0]]
    assert M.pick(100, rng=Choice())["id"] == items[10]["id"]
    assert M.listing()[10]["plays"] == 1


def test_changed_checker_invalidates_persisted_verdict_and_queues_recheck(tmp_path):
    item = M.add(b"audio", "Quiet song", "Artist", cfg=Config())
    assert item["checked_ok"] and item["verdict_revision"]

    # A verdict persisted by the previous checker must never reach attachment.
    radio_suitability.SHARED_CHECKER.write_text("import sys\nsys.exit(1)\n")
    assert M.pick(100, cfg=Config()) is None
    deadline = time.time() + 2
    while M.listing()[0]["checked_ok"] and time.time() < deadline:
        time.sleep(0.01)
    saved = M.listing()[0]
    assert saved["checked_ok"] is False
    assert M.approved_for_attach(item, Config()) is False


def test_cli(monkeypatch, capsys):
    monkeypatch.setattr(cli.C, "load", lambda: Config())
    assert cli.main(["radio", "music", "add", "missing"]) == 1
    monkeypatch.setattr(M, "add", lambda *a, **k: {"id": "abc", "checked_ok": True})
    assert cli.main(["radio", "music", "add", "track", "--title", "Song", "--artist", "Artist"]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == "abc"
    assert cli.main(["radio", "music", "list"]) == 0


def test_private_owner_only_handler(monkeypatch):
    from ctb import inbox
    cfg = Config(owner_chat_id=7)
    received = []
    class Topics:
        def session_at(self, *args):
            return None
    class Bot:
        def download_to(self, file_id, out, limit):
            received.append((file_id, limit))
            out.write(b"audio")
    monkeypatch.setattr(inbox, "_terminal_say", lambda *args, **kwargs: None)
    msg = {"date": 100, "chat": {"id": 7, "type": "private"}, "from": {"id": 7},
           "caption": "לרדיו", "audio": {"file_id": "file", "title": "Song", "performer": "Artist"}}
    assert inbox.handle({"message": msg}, cfg, Bot(), Topics(), None, None, 100) == "music"
    assert len(received) == 1 and M.listing()[0]["checked_ok"]
    msg["from"] = {"id": 8}
    assert inbox.handle({"message": msg}, cfg, Bot(), Topics(), None, None, 100) == "ignored"
    assert len(received) == 1
