"""The Studio desk never claims a production agent is working when the adapter reports no real progress."""
from pathlib import Path

from ctb.studio import strings

APP = Path(__file__).resolve().parents[1] / "src" / "ctb" / "studio" / "app.js"


def test_idle_label_exists_in_both_languages():
    text = Path(strings.__file__).read_text()
    assert text.count('"workerIdle"') == 2
    assert "אף סוכן לא עובד על זה כרגע" in text and "no agent is on it right now" in text


def test_agent_claimed_only_with_real_progress():
    js = APP.read_text()
    assert 'var active = pct > 0 || Number(j.eta_s) > 0;' in js
    assert '!active ? t("home.workerIdle")' in js
    assert 'bot(active ? "sm working" : "sm")' in js
