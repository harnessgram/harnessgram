"""The heartbeat round must not walk the projects folder once per transcript lookup.

A round asks for every topic's transcript several times (the session list is built for the waiting count, the
cards, the day view and the auto-cards scan). Each lookup used to glob every project folder, which made the
round's directory walks grow with topics x project folders and kept the heartbeat at a sizable share of a core.
"""
import glob
import os
import threading

from conftest import GOOD_TOKEN
from ctb import auto_cards, config, status
from ctb import heartbeat as H


def _tree(root, folders=30, per_folder=4):
    """root/<folder>/<sid>.jsonl, the layout of Claude Code's projects folder."""
    sids = []
    for f in range(folders):
        folder = root / f"-Users-me-project-{f}"
        folder.mkdir(parents=True)
        for s in range(per_folder):
            sid = f"{f:04d}{s:04d}-0000-4000-8000-000000000000"
            (folder / f"{sid}.jsonl").write_text("{}\n")
            sids.append(sid)
    return sids


def _projects(monkeypatch, tmp_path):
    root = tmp_path / "claude" / "projects"
    root.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    return root


def _count_scandir(monkeypatch):
    calls = []
    real = os.scandir

    def counting(path="."):
        calls.append(str(path))
        return real(path)
    monkeypatch.setattr(os, "scandir", counting)
    return calls


def test_round_answers_match_the_glob_for_every_session(monkeypatch, tmp_path):
    root = _projects(monkeypatch, tmp_path)
    sids = _tree(root, folders=5, per_folder=3)
    (root / ".hidden").mkdir()
    (root / ".hidden" / "hidden-only.jsonl").write_text("{}\n")  # glob's "*" skips dot folders
    linked = tmp_path / "elsewhere"
    linked.mkdir()
    (linked / "linked-only.jsonl").write_text("{}\n")
    (root / "-linked").symlink_to(linked, target_is_directory=True)  # glob follows folder symlinks
    (root / "stray.jsonl").write_text("{}\n")  # a file in the root is not a project folder
    asked = sids + ["hidden-only", "linked-only", "stray", "missing-session", "../escape", "a/b", ""]
    expected = {sid: status.transcript_of(sid) for sid in asked}
    assert expected["linked-only"] and expected["hidden-only"] is None and expected["stray"] is None
    with status.transcript_round():
        got = {sid: status.transcript_of(sid) for sid in asked}
    assert got == expected


def test_round_lists_the_projects_folder_once_for_any_number_of_lookups(monkeypatch, tmp_path):
    root = _projects(monkeypatch, tmp_path)
    sids = _tree(root, folders=30, per_folder=4)
    globbed = []
    monkeypatch.setattr(glob, "glob", lambda *a, **k: globbed.append(a) or [])
    walks = _count_scandir(monkeypatch)
    with status.transcript_round():
        for _ in range(5):  # the round builds the session list several times
            found = [status.transcript_of(sid) for sid in sids]
    assert all(found) and not globbed
    # One listing of the root and one of each project folder, whatever the number of lookups.
    assert walks.count(str(root)) == 1
    assert len(walks) == 1 + 30


def test_round_is_scoped_nested_and_per_thread(monkeypatch, tmp_path):
    root = _projects(monkeypatch, tmp_path)
    (root / "-p").mkdir()
    builds = []
    real = status._transcript_index
    monkeypatch.setattr(status, "_transcript_index", lambda r: builds.append(r) or real(r))
    with status.transcript_round():
        assert status.transcript_of("late") is None
        (root / "-p" / "late.jsonl").write_text("{}\n")
        with status.transcript_round():  # a nested block shares the outer listing
            assert status.transcript_of("late") is None
        other = []
        worker = threading.Thread(target=lambda: other.append(status.transcript_of("late")))
        worker.start()
        worker.join()
        assert other == [str(root / "-p" / "late.jsonl")]  # another thread is not inside the block
    assert len(builds) == 1
    # Outside a block every call looks again: a transcript written meanwhile is found at once.
    assert status.transcript_of("late") == str(root / "-p" / "late.jsonl")
    with status.transcript_round():  # the next round sees it too
        assert status.transcript_of("late") == str(root / "-p" / "late.jsonl")


def test_heartbeat_round_walks_the_projects_folder_at_most_once(monkeypatch, tmp_path):
    root = _projects(monkeypatch, tmp_path)
    sids = _tree(root, folders=20, per_folder=3)
    globbed = []
    real_glob = glob.glob
    monkeypatch.setattr(glob, "glob", lambda *a, **k: globbed.append(a) or real_glob(*a, **k))
    seen = []

    def fake_round(cfg, bot, now, find, listener_check):
        for _ in range(4):  # waiting count, cards, day view, ...
            seen.append([find(sid) for sid in sids])
        return 0
    monkeypatch.setattr(H, "_round", fake_round)
    walks = _count_scandir(monkeypatch)
    assert H.run_once(config.Config(), now=100) == 0
    assert all(all(row) for row in seen) and not globbed
    assert walks.count(str(root)) == 1 and len(walks) == 1 + 20
    # The block ends with the round: the next round lists again.
    assert H.run_once(config.Config(), now=130) == 0
    assert walks.count(str(root)) == 2


def test_auto_cards_scan_runs_inside_one_round(monkeypatch):
    inside = []
    monkeypatch.setattr(auto_cards, "_scan", lambda cfg, now: inside.append(
        getattr(status._ROUND, "index", None) is not None))

    class Thread:
        def __init__(self, target, **kw):
            self.target = target

        def start(self):
            self.target()
    monkeypatch.setattr(auto_cards.threading, "Thread", Thread)
    monkeypatch.setattr(auto_cards, "_scan_guard", threading.Lock())
    monkeypatch.setattr(auto_cards, "_scan_at", None)
    auto_cards.start(config.Config(), 1000)
    assert inside == [True]
    assert not auto_cards._scan_guard.locked()  # released after the scan
    assert getattr(status._ROUND, "index", None) is None


def test_run_sleeps_every_between_rounds(monkeypatch):
    from ctb import buildstamp, revive
    rounds, sleeps = [], []
    monkeypatch.setattr(H, "run_once", lambda cfg, bot=None: rounds.append(1))
    monkeypatch.setattr(H.Bot, "from_config", classmethod(lambda cls, c: object()))
    monkeypatch.setattr(buildstamp, "record_running", lambda name: None)
    monkeypatch.setattr(buildstamp, "clear_running", lambda name: None)
    monkeypatch.setattr(revive, "revived", threading.Event())
    monkeypatch.setattr(revive, "snapshot", lambda: None)  # the window record needs tmux

    class Stop(Exception):
        pass

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise Stop

    cfg = config.Config(bot_token=GOOD_TOKEN, owner_chat_id=1, session_auto_revive=False)
    try:
        H.run(cfg, sleep_fn=sleep)
    except Stop:
        pass
    assert rounds == [1, 1, 1] and sleeps == [H.EVERY] * 3  # one round per sleep, never a busy loop
    assert H.EVERY >= 30
