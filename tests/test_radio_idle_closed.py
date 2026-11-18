import json

import pytest

from ctb import radio, radio_listeners, radio_play
from ctb.config import Config
from ctb.telegram import Bot


def setup_station(tmp_path, monkeypatch, **settings):
    monkeypatch.setattr(radio_play, 'mirror_in_use', lambda cfg: False)
    monkeypatch.setattr(radio, 'folder', lambda: tmp_path)
    monkeypatch.setattr(radio, '_wanted_path', lambda: tmp_path / radio.WANTED)
    monkeypatch.setattr(radio, 'group_live_on', lambda cfg: False)
    monkeypatch.setattr(radio.time, 'time', lambda: 1700)
    radio._write(tmp_path / 'state.json', {'on': True, 'group_live': False})
    radio._write(tmp_path / 'status.json', {'started': 123})
    radio.set_wanted(True)
    for name in ('_unload', '_kill_leftover_encoder', 'clear_hls'):
        monkeypatch.setattr(radio, name, lambda: None)
    monkeypatch.setattr(radio, '_end_room', lambda *args: None)
    monkeypatch.setattr(radio, '_sync_copy', lambda *args: None)
    monkeypatch.setattr(radio, '_end_call', lambda s: True)
    messages = []
    monkeypatch.setattr(Bot, 'from_config', lambda cfg: type('B', (), {'call': lambda self, method, params: messages.append(params)})())
    return Config(radio=settings, owner_chat_id=7, chat_id=-1001234), messages


def test_private_idle_stop_clears_intent_and_notifies_once(tmp_path, monkeypatch):
    cfg, messages = setup_station(tmp_path, monkeypatch)
    assert radio.stop_private_idle(cfg, 1100, 123)
    assert not radio.wanted()
    assert len(messages) == 1 and messages[0]['chat_id'] == 7
    assert 'שידורי הקוקפיט נעצרו' in messages[0]['text']
    assert 'כפתור ההפעלה' in messages[0]['text']
    assert not radio.stop_private_idle(cfg, 1100, 123)
    assert len(messages) == 1


@pytest.mark.parametrize('case', ['recent', 'unavailable', 'group', 'new_run', 'too_soon'])
def test_private_idle_fails_safe(tmp_path, monkeypatch, case):
    cfg, messages = setup_station(tmp_path, monkeypatch)
    if case == 'recent':
        radio_listeners.player_fetch(tmp_path, now=1600)
    if case == 'unavailable':
        monkeypatch.setattr(radio_listeners, 'snapshot', lambda *a, **k: {'listening_detection': False, 'listening_source': 'unavailable'})
    if case == 'group':
        radio._write(tmp_path / 'state.json', {'on': True, 'group_live': True})
    assert not radio.stop_private_idle(cfg, 1101 if case == 'too_soon' else 1100, 999 if case == 'new_run' else 123)
    assert radio.wanted() and not messages


def test_configurable_interval(tmp_path, monkeypatch):
    cfg, messages = setup_station(tmp_path, monkeypatch, private_idle_minutes=11)
    assert not radio.stop_private_idle(cfg, 1100, 123)
    assert radio.wanted() and not messages


def test_watcher_requires_continuous_absence_and_resets_on_unknown(tmp_path, monkeypatch):
    cfg, _ = setup_station(tmp_path, monkeypatch)
    samples = iter([(1000, 'none'), (1599, 'none'), (1600, 'unknown'), (1700, 'none'),
                    (2299, 'none'), (2300, 'none')])
    current = {}
    class Event:
        def wait(self, seconds):
            try:
                current['at'], current['source'] = next(samples)
                return False
            except StopIteration:
                return True
    monkeypatch.setattr(radio_listeners, 'snapshot', lambda *args, **kw: {
        'listening_detection': True, 'listening_source': current['source']})
    requests = []
    radio.watch_private_idle(cfg, Event(), 123, clock=lambda: current['at'],
                             spawn_idle=lambda since, run: requests.append((since, run)))
    assert requests == [(1700, 123)]


@pytest.mark.parametrize("mirror", [True, None, "error"])
def test_mirror_presence_or_unknown_prevents_countdown_and_stop(tmp_path, monkeypatch, mirror):
    cfg, messages = setup_station(tmp_path, monkeypatch)
    def state(cfg):
        if mirror == "error":
            raise RuntimeError("unknown")
        return mirror
    monkeypatch.setattr(radio_play, "mirror_in_use", state)
    samples = iter([1000, 1600, 2200])
    current = {}
    class Event:
        def wait(self, seconds):
            try:
                current['at'] = next(samples)
                return False
            except StopIteration:
                return True
    requests = []
    radio.watch_private_idle(cfg, Event(), 123, clock=lambda: current['at'],
                             spawn_idle=lambda *args: requests.append(args))
    assert requests == []
    assert not radio.stop_private_idle(cfg, 1100, 123)
    assert radio.wanted() and not messages


@pytest.mark.parametrize("lang,expected", [("he", "שידורי הקוקפיט נעצרו"),
                                          ("en", "Team radio stopped")])
def test_idle_notification_uses_owner_language(tmp_path, monkeypatch, lang, expected):
    cfg, messages = setup_station(tmp_path, monkeypatch)
    cfg.ui_language = lang
    assert radio.stop_private_idle(cfg, 1100, 123)
    assert expected in messages[0]['text']
