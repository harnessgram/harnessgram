"""Each reply summary is copied silently to the alerts channel, linked to the reply."""
from ctb import alerts, mirror
from ctb.projects import Place


def test_summary_goes_to_alerts_channel(monkeypatch):
    sent = []
    monkeypatch.setattr(alerts, "alert", lambda cfg, text, session="", bot=None, silent=False, url="", kind="":
                        sent.append((text, silent, url)) or True)
    monkeypatch.setattr(mirror.pins, "update_index", lambda *a, **k: None)
    m = mirror.Mirror.__new__(mirror.Mirror)
    m.cfg = type("C", (), {"chat_id": -1001234567890, "owner_chat_id": 42})()
    m.bot = None
    m.topics = type("T", (), {"place_of": lambda self, s: Place(-1001234567890, 21)})()
    monkeypatch.setattr(mirror, "home_chat", lambda cfg: -1001234567890)
    m._index_reply("s1", 99, "גוף התשובה\n\nהכל עובד עכשיו", True)
    assert sent == [("הכל עובד עכשיו", True, "https://t.me/c/1234567890/21/99")]
    sent.clear()
    m._index_reply("s1", 100, "תשובה בלי סיכום", False)
    assert sent == []


def test_summary_copy_is_plain_and_redacted(monkeypatch):
    sent = []
    monkeypatch.setattr(alerts, "alert", lambda cfg, text, session="", bot=None, silent=False, url="", kind="":
                        sent.append(text) or True)
    monkeypatch.setattr(mirror.pins, "update_index", lambda *a, **k: None)
    monkeypatch.setattr(mirror, "redact", lambda t: t.replace("SECRET", "[redacted]"))
    m = mirror.Mirror.__new__(mirror.Mirror)
    m.cfg = type("C", (), {"chat_id": 42, "owner_chat_id": 42})()
    m.bot = None
    m.topics = type("T", (), {"place_of": lambda self, s: None})()
    monkeypatch.setattr(mirror, "home_chat", lambda cfg: 42)
    m._index_reply("s1", 7, "body\n\n**Done**, see [the PR](https://x.y/1) SECRET", True)
    assert sent == ["Done, see the PR [redacted]"]
