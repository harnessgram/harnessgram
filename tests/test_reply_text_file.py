from types import SimpleNamespace

import pytest

from ctb import cli


def args(path):
    return SimpleNamespace(session="s", title="title", file=None, translate=False,
                           reply_text_file=str(path), delete_reply_text_file=True,
                           reply_chat=1, reply_to=2, reply_source="owner", transcript="")


@pytest.mark.parametrize("contents", [None, b"\xff"])
def test_unreadable_reply_context_does_not_drop_reply(tmp_path, monkeypatch, contents):
    path = tmp_path / "context"
    if contents is not None:
        path.write_bytes(contents)
    cfg = SimpleNamespace(ready=True, translate=False)
    monkeypatch.setattr(cli.C, "load", lambda: cfg)
    monkeypatch.setattr(cli, "_read_text", lambda _args: "reply")
    from ctb import mirror
    calls = []
    monkeypatch.setattr(mirror, "run_reply", lambda *a, **k: calls.append((a, k)) or 0)
    assert cli._cmd_mirror(args(path)) == 0
    assert calls[0][1]["answer_to"]["text"] == ""
    assert not path.exists()
