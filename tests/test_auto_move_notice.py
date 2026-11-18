from types import SimpleNamespace

from ctb import inbox as I


def _base(tmp_path, monkeypatch):
    monkeypatch.setattr(I, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(I.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(I.accounts, "origin_account", lambda _session: "main")
    monkeypatch.setattr(I, "place", lambda *_args: None)


def test_no_target_notice_is_once_and_translated(tmp_path, monkeypatch):
    _base(tmp_path, monkeypatch)
    monkeypatch.setattr(I, "_current_move_request", lambda *_args: None)
    monkeypatch.setattr(I.accounts, "fallback_plan",
                        lambda _session: {"here": "main", "fresh": []})
    messages = []
    monkeypatch.setattr(I, "_terminal_say",
                        lambda _bot, _cfg, _where, text, **_kw: messages.append(text))
    cfg = SimpleNamespace(ui_language="he")
    assert not I.auto_move(cfg, object(), "session", 4, "spare")
    assert not I.auto_move(cfg, object(), "session", 4, "spare")
    assert len(messages) == 1
    assert messages[0] == I.t("account_auto_move_no_target", lang="he")


def test_successful_move_clears_notice(tmp_path, monkeypatch):
    _base(tmp_path, monkeypatch)
    notice = I.private_dir(tmp_path / "move-notices") / I.safe_name("session")
    I.write_private(notice, "no eligible target")
    monkeypatch.setattr(I, "_current_move_request", lambda *_args: ("spare", {}))
    monkeypatch.setattr(I, "account_move", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(I, "_limit_stopped_work", lambda *_args: False)
    monkeypatch.setattr(I, "_limit_cleared_by_move", lambda *_args: None)
    monkeypatch.setattr(I, "poll_log", lambda *_args: None)
    assert I.auto_move(SimpleNamespace(ui_language="he"), object(), "session", 4,
                       "spare", idle=True)
    assert not notice.exists()
