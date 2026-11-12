import threading
import time
from types import SimpleNamespace

import pytest

from ctb import panel, radio_room, radio_voice
from ctb.config import Config


def test_reserve_refuses_dates_older_than_the_ledger(tmp_path):
    path = tmp_path / 'usage.json'
    usage = radio_voice.Usage(path)
    day = time.mktime((2025, 1, 1, 12, 0, 0, 0, 0, -1))
    usage.add(850, now=day)
    for offset in range(1, 15):
        usage.add(10, now=day + offset * 86400)
    before = path.read_bytes()
    for instance in (usage, usage, radio_voice.Usage(path), radio_voice.Usage(path)):
        assert instance.reserve(1, 15, now=day) is None
        assert path.read_bytes() == before


@pytest.fixture
def room(monkeypatch, tmp_path):
    monkeypatch.setattr(radio_room, 'folder', lambda: tmp_path)
    radio_room._save({'id': 'room', 'status': 'done', 'topic': 'Sample',
                      'summary': {'tasks': [{'id': 'task', 'text': 'Sample task', 'sent': None}]}})
    entry = SimpleNamespace(session='12345678-session', closed=False, state='working', chat=None, name='Sample')
    monkeypatch.setattr(panel, 'TopicMap', lambda *args, **kwargs: None)
    monkeypatch.setattr(panel, 'collect', lambda *args, **kwargs: [entry])
    return Config(bot_token='sample', owner_chat_id=123, target_language='en', ui_language='en')


class Box:
    def __init__(self):
        self.items = []

    def put(self, session, item, until=None):
        self.items.append(item)


def send(cfg, box, now):
    return panel.room_send(cfg, box, '12345678', 'room', 'task', now=now)


def test_abandoned_claim_expires_after_sixty_seconds(room):
    box = Box()
    radio_room.claim('room', 'task', 'Sample', now=100, claim_id='crashed')
    assert radio_room.load()['summary']['tasks'][0]['state'] == 'claimed'
    for now in (159, 160):
        with pytest.raises(radio_room.RoomError):
            send(room, box, now)
    assert send(room, box, 161) == 'Sample'
    with pytest.raises(radio_room.RoomError):
        send(room, box, 222)
    assert len(box.items) == 1
    assert radio_room.load()['summary']['tasks'][0]['state'] == 'sent'


def test_parallel_taps_have_one_winner(room):
    box, results, barrier = Box(), [], threading.Barrier(2)
    def tap():
        barrier.wait()
        try:
            results.append(send(room, box, 100))
        except radio_room.RoomError as exc:
            results.append(exc.kind)
    threads = [threading.Thread(target=tap) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == ['Sample', 'closed']
    assert len(box.items) == 1


def test_building_body_failure_releases_claim(room, monkeypatch):
    box = Box()
    original = radio_room.proposal_item
    def fail(*args):
        raise ValueError('sample failure')
    monkeypatch.setattr(radio_room, 'proposal_item', fail)
    with pytest.raises(ValueError):
        send(room, box, 100)
    assert not box.items
    monkeypatch.setattr(radio_room, 'proposal_item', original)
    assert send(room, box, 100) == 'Sample'
    assert len(box.items) == 1


def test_old_claim_cannot_release_new_claim(room):
    radio_room.claim('room', 'task', 'Sample', now=100, claim_id='old')
    radio_room.claim('room', 'task', 'Sample', now=161, claim_id='new')
    radio_room.unclaim('room', 'task', 'old')
    assert radio_room.load()['summary']['tasks'][0]['claim_id'] == 'new'
