"""A new session opened from Telegram starts on the model the owner picks (exact id, or the default), and
the /model menu can make a model the default for new sessions (Claude Code's settings.json `model`)."""
import json
import os
from types import SimpleNamespace

import pytest

from ctb import inbox as I
from ctb import models
from ctb.config import state_dir
from test_inbound_places import ORBIT, msg, press, rig, send  # noqa: F401 (rig is a fixture)


@pytest.fixture
def started(rig, monkeypatch):
    calls = []
    monkeypatch.setattr(I, "projects", lambda *a, **k: ["/tmp/general"])
    monkeypatch.setattr(I.threading, "Thread", lambda *a, **k: SimpleNamespace(start=lambda: None))
    monkeypatch.setattr(I, "_tmux", lambda *a: calls.append(a) or SimpleNamespace(returncode=0, stdout=""))
    return calls


def pending(thread=77):
    return json.loads((state_dir() / "projects-pending.json").read_text())[f"{ORBIT}:{thread}"]


def picker(rig):
    return [p for p in rig.bot.methods("sendMessage") if "inline_keyboard" in str(p.get("reply_markup"))][-1]


def buttons(markup):
    return [b for row in markup["inline_keyboard"] for b in row]


def settings_path():
    return models.settings_file()


def test_picker_offers_every_model_by_name_and_the_default(rig, started):
    settings_path().parent.mkdir(parents=True, exist_ok=True)
    settings_path().write_text(json.dumps({"model": "claude-sonnet-5-5"}))
    assert send(rig, msg("a new idea", thread=77)) == "picker"
    texts = [b["text"] for b in buttons(picker(rig)["reply_markup"])]
    for label in ("Fable 5.2", "Opus 5.5", "Sonnet 5.5", "Haiku 5.5"):
        assert any(label in x for x in texts), label
    assert any("Default (⁨Sonnet 5.5⁩)" in x for x in texts)  # the current default, by name
    assert any(x.startswith("✓ Default") for x in texts)  # nothing chosen yet: the default is marked


@pytest.mark.parametrize("key,model_id", [("fable", "claude-fable-5-2"), ("opus", "claude-opus-5-5"),
                                          ("sonnet", "claude-sonnet-5-5"), ("haiku", "claude-haiku-5-5")])
def test_the_session_starts_on_the_exact_model_chosen(rig, started, key, model_id):
    send(rig, msg("a new idea", thread=77))
    token = pending()["token"]
    assert press(rig, f"nm:{token}:{key}", thread=77) == "model-chosen"
    assert pending()["model"] == key
    marked = rig.bot.methods("editMessageReplyMarkup")[-1]["reply_markup"]
    assert any(b["text"] == "✓ " + models.LABELS[key] for b in buttons(marked))  # one tap, marked in place
    assert not started
    press(rig, f"n:{token}:0", thread=77)
    (cmd,) = [a[-1] for a in started if a[0] == "new-session"]
    assert f"--model {model_id} " in cmd and cmd.index("--model") < cmd.index("--remote-control")
    opened = [p["text"] for p in rig.bot.methods("editMessageText") if "\U0001F9E0" in p["text"]]
    assert opened and models.LABELS[key] in opened[-1]


def test_default_starts_without_a_model_flag(rig, started):
    send(rig, msg("a new idea", thread=77))
    token = pending()["token"]
    press(rig, f"nm:{token}:opus", thread=77)
    press(rig, f"nm:{token}:d", thread=77)  # changed back to the default
    press(rig, f"n:{token}:0", thread=77)
    (cmd,) = [a[-1] for a in started if a[0] == "new-session"]
    assert "--model" not in cmd


def test_a_stale_or_foreign_model_tap_does_nothing(rig, started):
    send(rig, msg("a new idea", thread=77))
    assert press(rig, "nm:0123456789abcdef:opus", thread=77) == "ignored"
    assert press(rig, f"nm:{pending()['token']}:gpt", thread=77) == "ignored"
    assert press(rig, f"nm:{pending()['token']}:opus", thread=77, sender=99) == "ignored"
    assert pending()["model"] == ""


# --- "⭐ Set as default for new sessions" --------------------------------------------------------------

def test_model_menu_has_the_set_default_choice(rig, monkeypatch):
    from test_inbound_places import THREAD
    monkeypatch.setattr(I, "pane_for_session", lambda session: "%9")
    send(rig, msg("/model", thread=THREAD))
    menus = [p for p in rig.bot.methods("sendMessage") if "model:opus" in str(p.get("reply_markup"))]
    assert menus, rig.bot.calls
    data = [b["callback_data"] for b in buttons(menus[-1]["reply_markup"])]
    assert "mdef:menu" in data
    labels = [b["text"] for b in buttons(menus[-1]["reply_markup"])]
    assert I.t("model_set_default_btn") in labels  # in the menu's language, like its other buttons
    assert I.t("model_set_default_btn", lang="en") == "⭐ Set as default for new sessions"
    assert I.t("model_set_default_btn", lang="he") == "\u200f⭐ לקבוע כברירת מחדל לסשנים חדשים"


def test_set_default_writes_only_the_model_key_atomically():
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    original = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x"}]}]}, "theme": "dark",
                "model": "claude-haiku-5-5", "permissions": {"allow": ["Bash(ls)"]}}
    path.write_text(json.dumps(original))
    os.chmod(path, 0o600)
    assert models.set_default("opus") == "Opus 5.5"
    data = json.loads(path.read_text())
    assert data == dict(original, model="claude-opus-5-5")
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert not [p for p in path.parent.iterdir() if p.name.endswith(".tmp")]  # no leftover temp file
    assert models.default_label() == "Opus 5.5"


def test_set_default_writes_through_a_symlink(tmp_path):
    real = tmp_path / "dotfiles" / "settings.json"
    real.parent.mkdir()
    real.write_text(json.dumps({"theme": "light"}))
    settings_path().parent.mkdir(parents=True, exist_ok=True)
    settings_path().unlink(missing_ok=True)
    settings_path().symlink_to(real)
    models.set_default("sonnet")
    assert settings_path().is_symlink() and json.loads(real.read_text()) == {"theme": "light", "model": "claude-sonnet-5-5"}


def test_a_broken_settings_file_is_never_overwritten():
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    with pytest.raises(ValueError):
        models.set_default("opus")
    assert path.read_text() == "{not json"
    with pytest.raises(ValueError):
        models.set_default("gpt")


def test_the_star_button_opens_the_models_then_one_tap_saves_and_confirms_by_name(rig):
    from test_inbound_places import THREAD
    assert press(rig, "mdef:menu", thread=THREAD) == "model-default-menu"
    menu = rig.bot.methods("editMessageText")[-1]
    keys = [b["callback_data"] for b in buttons(menu["reply_markup"])]
    assert keys == ["mdef:" + k for k in models.CHOICES] + ["more:dismiss"]
    assert press(rig, "mdef:haiku", thread=THREAD) == "model-default"
    assert json.loads(settings_path().read_text())["model"] == "claude-haiku-5-5"
    done = rig.bot.methods("editMessageText")[-1]["text"]
    assert "Haiku 5.5" in done
    assert press(rig, "mdef:menu", thread=THREAD) == "model-default-menu"
    marked = [b["text"] for b in buttons(rig.bot.methods("editMessageText")[-1]["reply_markup"])]
    assert "✓ ⭐ Haiku 5.5" in marked


def test_only_the_owner_can_set_the_default(rig):
    assert press(rig, "mdef:opus", sender=99) == "ignored"
    assert not settings_path().exists()
