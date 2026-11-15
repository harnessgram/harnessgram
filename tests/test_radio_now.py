"""The radio's live title (radio_now.py): what goes on the lock screen, when it changes, and who may read it."""
import json
import threading
import time

import pytest

from ctb import listen_link, radio, radio_now, radio_suitability
from test_listen_link import make
from test_radio_player import call, hls, server  # noqa: F401  (fixtures)


@pytest.fixture
def suitable(monkeypatch):
    seen = []
    monkeypatch.setattr(radio_suitability, "suitable", lambda text: seen.append(text) or True)
    return seen


def test_a_title_is_the_opening_line_without_the_greeting(suitable):
    assert radio_now.title_for("Team radio, 10:30. The build is green. Two reviews wait.") == \
        "The build is green. Two reviews wait."
    assert suitable == ["The build is green. Two reviews wait."]


def test_a_title_is_cut_cleaned_and_filtered(suitable):
    long = "word " * 60
    title = radio_now.title_for(long)
    assert len(title) <= radio_now.TITLE_CHARS and title.endswith("…")
    assert "‮" not in radio_now.title_for("abc‮ def​ ghi")
    assert radio_now.title_for("") == "" and radio_now.title_for("... !!!") == ""
    leaked = radio_now.title_for("The key is sk-ant-api03-" + "A" * 40 + " now.")
    assert "A" * 40 not in leaked


@pytest.mark.parametrize("hidden", ["\u00ad", "\u200b", "\u2060", "\u200e", "\ufeff"])
def test_a_key_split_by_an_invisible_character_is_filtered_whole(suitable, hidden):
    key = "sk-ant-api03-" + "Ab3" * 14
    split = key[:10] + hidden + key[10:25] + hidden + key[25:]
    title = radio_now.title_for("The key is " + split + " now.")
    assert key not in title and key[13:] not in title and "Ab3Ab3Ab3" not in title
    assert hidden not in title
    assert suitable[-1] == title  # the suitability check sees exactly the text that is shown


def test_an_unsuitable_title_is_never_shown(monkeypatch):
    monkeypatch.setattr(radio_suitability, "suitable", lambda text: False)
    assert radio_now.title_for("Something unfit to show.") == ""

    def broken(text):
        raise RuntimeError("checker down")
    monkeypatch.setattr(radio_suitability, "suitable", broken)
    assert radio_now.title_for("A fine sentence.") == ""  # fails closed


def test_publish_read_and_clear(tmp_path):
    assert radio_now.read(tmp_path) == {}
    radio_now.publish(tmp_path, "The build is green.", now=123)
    assert radio_now.read(tmp_path) == {"title": "The build is green.", "updated": 123}
    radio_now.publish(tmp_path, "")  # nothing fit to show: the previous title stays
    assert radio_now.read(tmp_path)["title"] == "The build is green."
    (tmp_path / radio_now.FILE).write_text('{"title": 5}')
    assert radio_now.read(tmp_path) == {}
    radio_now.clear(tmp_path)
    radio_now.clear(tmp_path)
    assert not (tmp_path / radio_now.FILE).exists()


def test_the_title_changes_when_a_new_clip_goes_on_the_air(tmp_path):
    station = object.__new__(radio.Station)
    waits = iter([False, False, True])

    class Quit:
        def wait(self, _):
            return next(waits)

    class Mixer:
        tags = iter([(4, 0), (5, 0)])

        def progress(self):
            return next(self.tags)

    station.quit, station.mixer, station.dir, station.snap = Quit(), Mixer(), tmp_path, None
    station.caption_lock, station.talks = threading.Lock(), {}
    station.captions = {4: (0, 10, ["first"]), 5: (0, 10, ["second"])}
    station.now_titles = {4: "First line.", 5: ""}
    station.caption_loop()
    assert radio_now.read(tmp_path)["title"] == "First line."  # an empty title (unfit) keeps the previous one
    assert station.now_titles == {}


def test_now_json_needs_the_listening_token(server, hls):
    path = make(server)
    radio_now.publish(radio.folder(), "The build is green.")
    status, headers, body = call(server, "GET", path + "/now.json")
    data = json.loads(body)
    assert status == 200 and data["title"] == "The build is green." and data["updated"] > time.time() - 60
    assert set(data) == {"title", "updated"} and headers["Cache-Control"] == "no-store"
    for bad in ("A" * 43, "short", ".."):
        assert call(server, "GET", "/listen/" + bad + "/now.json")[0] == 403
    assert call(server, "GET", "/radio/now.json")[0] == 403
    listen_link.revoke()
    assert call(server, "GET", path + "/now.json")[0] == 403


def test_now_json_names_the_station_while_the_radio_is_off(server, hls):
    path = make(server)
    radio_now.publish(radio.folder(), "An old line.")
    (hls / "live.m3u8").unlink()
    status, _, body = call(server, "GET", path + "/now.json")
    assert status == 200 and json.loads(body) == {"title": "רדיו הצוות", "updated": 0}
