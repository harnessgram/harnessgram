"""Owner report #339: a slow Cockpit state answer leaves one data-free timing line in the panel log."""
from ctb import panel


def test_a_fast_request_writes_nothing(capsys):
    panel.log_slow("state", 100.0, built=100.5, sent=101.0)
    assert capsys.readouterr().err == ""


def test_a_slow_request_names_each_step_and_counts_only(capsys):
    panel.log_slow("state", 100.0, built=126.0, youtube=126.2, sent=127.5, pending=3)
    err = capsys.readouterr().err
    assert "panel: slow state 27.5s: built=26.0s youtube=26.2s sent=27.5s pending=3" in err


def test_a_broken_mark_never_raises(capsys):
    panel.log_slow("state", 100.0, sent="later")
    assert capsys.readouterr().err == ""
