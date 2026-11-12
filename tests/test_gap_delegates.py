"""Configurable delegated helpers: a dashboard row for running helper processes and a vendor label on the
live progress card. Off unless the `delegates` setting lists something."""
import pytest

from conftest import GOOD_TOKEN
from ctb import config as C, dashboard, progress
from ctb.config import Config

SPECS = [{"match": "helper-a", "label": "Helper A"}, {"match": "review-bot", "label": "Review <bot>"}]
PS = """\
    03:05 python3 /opt/tools/helper-a --write job.txt
 1-02:00:10 node review-bot --pr 4
       00:10 grep helper-a
    00:20 /bin/zsh -c ps -axo etime=,command= | grep review-bot
    10:00 vim notes.txt
"""


@pytest.fixture(autouse=True)
def english():
    from ctb import i18n
    before = i18n.get_lang()
    i18n.set_lang("en")
    yield
    i18n.set_lang(before)


def cfg(**kw):
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=7, ui_language=kw.pop("ui_language", "en"), **kw)


def test_the_setting_is_validated():
    assert C.from_dict({"delegates": SPECS}).delegates == SPECS
    for bad in ("x", [{"match": "a"}], [{"match": " ", "label": "x"}], [1]):
        with pytest.raises(C.ConfigError):
            C.from_dict({"delegates": bad})
    assert C.from_dict({}).delegates == []


def test_running_helpers_become_rows_with_their_run_time():
    rows = dashboard.team_jobs(cfg(delegates=SPECS), ps_output=PS)
    assert rows == ["🤖 <b>Helper A</b> · 3 min", "🤖 <b>Review &lt;bot&gt;</b> · 1560 min"]


def test_hebrew_rows_are_right_to_left():
    rows = dashboard.team_jobs(cfg(delegates=SPECS, ui_language="he"), ps_output=PS)
    assert rows[0] == "‏🤖 <b>⁨Helper A⁩</b> · 3 דק׳"


def test_nothing_is_scanned_unless_configured(monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("ps must not run"))
    assert dashboard.team_jobs(cfg()) == [] and dashboard.jobs_block(cfg(), "en") == []
    assert "Background jobs" not in dashboard.text(cfg(), {})


def test_the_dashboard_lists_helpers_or_says_none(monkeypatch):
    block = dashboard.jobs_block(cfg(delegates=SPECS), "en", ps_output=PS)
    assert block[0] == "👷 <b>Background jobs now</b>" and block[2].startswith("🤖 <b>Helper A</b>")
    assert dashboard.jobs_block(cfg(delegates=SPECS), "en", ps_output="")[-1] == "No helper is running right now"


def test_progress_label_names_the_delegate_vendor():
    call = {"tool_name": "Bash", "tool_input": {"command": "run helper-a --fast", "description": "Review it"}}
    assert progress.label(call) .startswith("⚙️")
    assert progress.label(call, SPECS) == "🤖 Review it [Helper A · background]"
    codex = {"tool_name": "Bash", "tool_input": {"command": "codex exec -m big-model x", "description": "Go"}}
    assert progress.label(codex) == "🤖 Go [Codex big-model · background]"


def test_progress_label_shows_the_subagent_kind():
    call = {"tool_name": "Agent", "tool_input": {"description": "Audit", "model": "sonnet", "subagent_type": "reviewer"}}
    assert progress.label(call) == "🤖 Audit [Claude sonnet · reviewer · background]"
