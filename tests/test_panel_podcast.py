"""The Cockpit's podcast endpoint (/api/podcast): owner only with the fresh login, strict bodies, an episode only
from the owner's own feed or a pasted https link, the suitability gate's refusals, and the state the page shows.
No network: the feed fetch is replaced."""
import json
import threading
import time

import pytest

from conftest import GOOD_TOKEN
from ctb import panel, radio_podcast as P
from ctb.config import Config
from test_panel import OWNER, call, signed
from test_radio_podcast import FEED, rss, shared_checker  # noqa: F401 (the autouse checker stand-in)

PATH = "/api/podcast"
DATA = rss([("First", "ok"), ("Second", "ok")])


@pytest.fixture
def srv(monkeypatch):
    monkeypatch.setattr(P, "fetch", lambda url, *a, **k: ("application/rss+xml", DATA) if "audio_stops" not in k else ("application/rss+xml", DATA))
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he", radio={"every_minutes": 20},
                 radio_podcasts=[{"name": "Science Hour", "url": FEED}])
    s = panel.PanelServer(("127.0.0.1", 0), cfg)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield s
    s.shutdown()
    s.server_close()


def act(srv, body, init=None):
    code, _h, data = call(srv, path=PATH, init=signed() if init is None else init, body=json.dumps(body).encode())
    return code, (json.loads(data) if data else {})


def test_the_route_is_registered_and_owner_only(srv):
    assert PATH in panel.ROUTES and panel.ACTIONS[PATH] == "_podcast"
    body = json.dumps({"action": "control", "op": "pause"}).encode()
    assert call(srv, path=PATH, body=body)[0] == 403                                   # no login
    assert call(srv, path=PATH, init=signed(user_id=OWNER + 1), body=body)[0] == 403   # not the owner
    assert call(srv, path=PATH, init=signed(tamper=True), body=body)[0] == 403
    assert call(srv, path=PATH, init=signed(auth_date=int(time.time()) - panel.DECIDE_MAX_AGE - 60), body=body)[0] == 403  # not fresh
    assert call(srv, method="GET", path=PATH, init=signed())[0] in (404, 405)
    assert P.view(srv.cfg)["paused"] is False
    assert act(srv, {"action": "control", "op": "pause"})[0] == 200 and P.view(srv.cfg)["paused"] is True


def test_the_body_limit_is_enforced(srv):
    big = json.dumps({"action": "add", "link": "https://a.example.org/" + "x" * 2000}).encode()
    assert call(srv, path=PATH, init=signed(), body=big)[0] in (400, 413)


def test_nothing_works_when_the_radio_is_not_configured():
    s = panel.PanelServer(("127.0.0.1", 0), Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he"))
    threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        assert act(s, {"action": "control", "op": "stop"})[0] == 404
        assert "podcast" not in json.loads(call(s, init=signed())[2])
    finally:
        s.shutdown()
        s.server_close()


@pytest.mark.parametrize("body", [
    {}, {"action": "nope"}, {"action": "episodes"}, {"action": "episodes", "feed": 5}, {"action": "episodes", "feed": "a", "x": "b"},
    {"action": "add"}, {"action": "add", "link": "https://a.example.org/f", "feed": "A"}, {"action": "add", "feed": "A"},
    {"action": "add", "link": 5}, {"action": "control"}, {"action": "control", "op": 3}, {"action": "remove"},
    {"action": "remove", "id": ["a"]}, {"action": "control", "op": "pause", "extra": "x"}])
def test_malformed_bodies_are_bad_requests(srv, body):
    assert act(srv, body)[0] == 400


def test_a_list_body_is_a_bad_request(srv):
    code, _h, _d = call(srv, path=PATH, init=signed(), body=b"[1]")
    assert code == 400


def test_episodes_come_only_from_a_configured_feed(srv):
    code, out = act(srv, {"action": "episodes", "feed": "science hour"})
    assert code == 200 and [e["title"] for e in out["episodes"]] == ["Second", "First"]
    assert set(out["episodes"][0]) == {"id", "title", "seconds", "published"}  # never the audio address
    assert act(srv, {"action": "episodes", "feed": "https://evil.example.org/f"})[0] == 404
    assert act(srv, {"action": "episodes", "feed": "Other"})[0] == 404


def test_pick_then_transport_then_remove(srv):
    eid = act(srv, {"action": "episodes", "feed": "Science Hour"})[1]["episodes"][0]["id"]
    code, out = act(srv, {"action": "add", "feed": "Science Hour", "episode": eid})
    assert code == 200 and [q["title"] for q in out["podcast"]["queue"]] == ["Second"]
    assert act(srv, {"action": "add", "feed": "Science Hour", "episode": eid})[0] == 409          # already there
    assert act(srv, {"action": "add", "feed": "Science Hour", "episode": "0" * 16})[0] == 404
    assert act(srv, {"action": "add", "feed": "Nope", "episode": eid})[0] == 404
    for op in ("pause", "play", "skip"):
        assert act(srv, {"action": "control", "op": op})[0] == 200
    assert act(srv, {"action": "control", "op": "rewind"})[0] == 400
    qid = out["podcast"]["queue"][0]["id"]
    assert act(srv, {"action": "remove", "id": "../etc"})[0] == 400
    code, out = act(srv, {"action": "remove", "id": qid})
    assert code == 200 and out["podcast"]["queue"] == []
    act(srv, {"action": "add", "feed": "Science Hour", "episode": eid})
    assert act(srv, {"action": "control", "op": "stop"})[1]["podcast"]["queue"] == []


def test_a_pasted_link_is_checked_and_refusals_have_their_own_codes(srv, monkeypatch, tmp_path):
    assert act(srv, {"action": "add", "link": "http://pods.example.org/f"})[0] == 400
    assert act(srv, {"action": "add", "link": "https://pods.example.org/feed"})[0] == 200
    P.control("stop")
    bad = rss([("a badword", "y")])
    monkeypatch.setattr(P, "fetch", lambda url, *a, **k: ("application/rss+xml", bad))
    assert act(srv, {"action": "add", "link": "https://pods.example.org/feed"})[0] == 422        # unsuitable
    monkeypatch.setattr(P, "fetch", lambda url, *a, **k: ("audio/mpeg", b""))
    assert act(srv, {"action": "add", "link": "https://cdn.example.org/a.mp3"})[0] == 422        # no text to judge
    monkeypatch.setattr(P, "fetch", lambda url, *a, **k: ("application/rss+xml", DATA))
    srv.cfg.radio = {"podcast_checker": str(tmp_path / "missing.py")}
    assert act(srv, {"action": "add", "link": "https://pods.example.org/feed"})[0] == 409        # no checker
    srv.cfg.radio = {"every_minutes": 20}
    explicit = rss([("X", "y")], extra="<itunes:explicit>yes</itunes:explicit>")
    monkeypatch.setattr(P, "fetch", lambda url, *a, **k: ("application/rss+xml", explicit))
    assert act(srv, {"action": "add", "link": "https://pods.example.org/feed"})[0] == 422
    assert [e for e in act(srv, {"action": "episodes", "feed": "Science Hour"})[1]["episodes"]] == []  # explicit ones are not offered


def test_fetch_failures_are_gateway_errors_and_unexpected_ones_are_500(srv, monkeypatch):
    def boom(url, *a, **k):
        raise P.PodcastError("fetch")
    monkeypatch.setattr(P, "fetch", boom)
    assert act(srv, {"action": "episodes", "feed": "Science Hour"})[0] == 502
    monkeypatch.setattr(P, "fetch", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("secret detail")))
    code, out = act(srv, {"action": "episodes", "feed": "Science Hour"})
    assert code == 500 and "secret" not in json.dumps(out)


def test_the_state_carries_the_podcast_block_when_the_radio_is_configured(srv):
    act(srv, {"action": "add", "link": "https://pods.example.org/feed"})
    pod = json.loads(call(srv, init=signed())[2])["podcast"]
    assert pod["feeds"] == ["Science Hour"] and [q["title"] for q in pod["queue"]] == ["Second"]
    assert pod["now"] is None and pod["paused"] is False and "url" not in json.dumps(pod)  # no audio address leaves the server
