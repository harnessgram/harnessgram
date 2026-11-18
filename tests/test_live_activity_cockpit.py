import json
from types import SimpleNamespace

from conftest import GOOD_TOKEN
from ctb import panel, progress, topics
from ctb.config import Config, set_live_activity_in_cockpit


class Bot:
    def __init__(self):
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, dict(params)))
        return {"message_id": 100}


def _thirty_steps(monkeypatch, root, cockpit):
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=-1001, translate=False)
    set_live_activity_in_cockpit(cockpit)
    bot = Bot()
    monkeypatch.setattr(progress, "place", lambda _cfg, _sid: SimpleNamespace(chat=-1001, thread=7))
    monkeypatch.setattr(topics, "set_state", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(progress.flood, "optional_call", lambda b, method, params, **_kw: b.call(method, params))
    clock = [100]
    def now():
        clock[0] += 10
        return clock[0]
    rows = []
    for n in range(30):
        rows.append({"kind": "tool", "t": n + 1, "l": f"step {n}", "tool": "Read"})
        (root / "s.jsonl").write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
        progress.run_send_progress(cfg, "s", root=root, bot=bot, throttle=0, now_fn=now)
    return bot


def test_cockpit_activity_reduces_telegram_calls_and_is_in_panel_state(monkeypatch, tmp_path):
    monkeypatch.delenv("CTB_LIVE_ACTIVITY_COCKPIT")
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    before = _thirty_steps(monkeypatch, legacy, cockpit=False)
    live = tmp_path / "live"
    live.mkdir()
    after = _thirty_steps(monkeypatch, live, cockpit=True)

    assert len(after.calls) <= len(before.calls) * 0.2
    saved = json.loads((live / "s.msg").read_text(encoding="utf-8"))
    assert saved["activity"] and "29" in saved["activity"][-1]

    monkeypatch.setattr(panel, "state_dir", lambda: tmp_path)
    (tmp_path / "progress").mkdir()
    (tmp_path / "progress" / "s.msg").write_text(json.dumps(saved), encoding="utf-8")
    assert panel._live_activity("s", 999)["steps"]


def test_live_activity_setting_defaults_off_and_can_be_enabled(monkeypatch):
    monkeypatch.delenv("CTB_LIVE_ACTIVITY_COCKPIT")
    set_live_activity_in_cockpit(False)
    from ctb.config import live_activity_in_cockpit
    assert live_activity_in_cockpit() is False
    set_live_activity_in_cockpit(True)
    assert live_activity_in_cockpit() is True


def test_second_turn_starts_at_row_zero_without_stale_cards_or_ended(monkeypatch, tmp_path):
    monkeypatch.setenv("CTB_LIVE_ACTIVITY_COCKPIT", "1")
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=-1001, translate=False)
    set_live_activity_in_cockpit(True)
    bot = Bot()
    monkeypatch.setattr(progress, "place", lambda _cfg, _sid: SimpleNamespace(chat=-1001, thread=7))
    monkeypatch.setattr(topics, "set_state", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(progress.flood, "optional_call", lambda b, method, params, **_kw: b.call(method, params))
    first = [{"kind": "tool", "t": n + 1, "l": f"old {n}", "tool": "Read"} for n in range(3)]
    (tmp_path / "s.jsonl").write_text("\n".join(map(json.dumps, first)), encoding="utf-8")
    progress.run_send_progress(cfg, "s", root=tmp_path, bot=bot, throttle=0, now_fn=lambda: 100)
    progress.run_end_progress(cfg, "s", root=tmp_path, bot=bot)
    assert json.loads((tmp_path / "s.msg").read_text())["ended"] is True

    second = [{"kind": "tool", "t": 10, "l": "new first row", "tool": "Write"}]
    (tmp_path / "s.jsonl").write_text(json.dumps(second[0]), encoding="utf-8")
    progress.run_send_progress(cfg, "s", root=tmp_path, bot=bot, throttle=0, now_fn=lambda: 200)
    saved = json.loads((tmp_path / "s.msg").read_text())
    assert saved["cursor"] == 1 and saved.get("ended") is None
    assert saved["cards"][0]["row"]["l"] == "new first row"
    assert all("old" not in line for line in saved["activity"])
