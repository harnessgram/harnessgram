"""The radio's podcast queue (radio_podcast.py): safe feed reading, the queue and its caps, the radio's talk pausing
under an episode and resuming after it, the suitability gate, the private-stream-only rule, and the /podcast
command. Everything is mocked: no network, no ffmpeg, no live radio."""
import json
import re
import threading
import time
from pathlib import Path

import pytest

from conftest import GOOD_TOKEN
from ctb import radio, radio_podcast as P, radio_staff, radio_youtube
from ctb.config import Config, from_dict
from test_panel import OWNER

ITUNES_NS = "http:" + "//www." + "itunes.com/dtds/podcast-1.0.dtd"
FEED = "https://pods.example.org/feed.xml"
AUDIO = "https://cdn.example.org/a/{n}.mp3"


def rss(items, extra="", explicit=""):
    body = "".join(
        f"<item><title>{t}</title><guid>g{n}</guid><description>{d}</description>"
        f'<enclosure url="{AUDIO.format(n=n)}" type="audio/mpeg" length="1"/>'
        f"<pubDate>Mon, 0{n} Jan 2026 10:00:00 GMT</pubDate><itunes:duration>1:02:03</itunes:duration>{extra}</item>"
        for n, (t, d) in enumerate(items, 1))
    return (f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0" '
            f'xmlns:itunes="{ITUNES_NS}"><channel><title>Show</title>{explicit}{body}'
            f"</channel></rss>").encode()


CHECKER = """import re, sys
text = open(sys.argv[1], encoding="utf-8").read()
sys.exit(1 if re.search("badword|גרוע", text) else 0)
"""


@pytest.fixture(autouse=True)
def shared_checker(monkeypatch, tmp_path_factory):
    """A stand-in for the shared child-suitability checker: it rejects 'badword' and 'גרוע'."""
    from ctb import radio_suitability
    path = tmp_path_factory.mktemp("checker") / "check.py"
    path.write_text(CHECKER, encoding="utf-8")
    monkeypatch.setattr(radio_suitability, "SHARED_CHECKER", path)
    return path


def cfg(**kw):
    kw.setdefault("radio_podcasts", [{"name": "Science Hour", "url": FEED}])
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he", ui_language="en",
                  radio=kw.pop("radio", {"every_minutes": 20}), **kw)


def feed_fetcher(data):
    def fetcher(url, *a, **k):
        return "application/rss+xml", data
    return fetcher


# --- reading a feed ---------------------------------------------------------------------------------

def test_a_feed_lists_its_newest_episodes_first_with_plain_text():
    data = rss([("Old", "&lt;p&gt;first&lt;/p&gt;"), ("New &amp; &lt;b&gt;shiny&lt;/b&gt;", "&lt;p&gt;Hello &lt;i&gt;there&lt;/i&gt;&lt;/p&gt;")])
    eps = P.parse_feed(data, FEED, "Science Hour")
    assert [e["title"] for e in eps] == ["New & shiny", "Old"]
    assert eps[0]["description"] == "Hello there" and eps[0]["seconds"] == 3723
    assert eps[0]["audio_url"].endswith("/2.mp3") and eps[0]["feed"] == "Science Hour"
    assert re.fullmatch(r"[0-9a-f]{16}", eps[0]["id"]) and eps[0]["id"] != eps[1]["id"]


@pytest.mark.parametrize("data", [b"", b"not xml at all", b"<rss><channel><item>", b"<html><body/></html>",
                                  b'<?xml version="1.0"?><rss version="2.0"></rss>',
                                  rss([("a", "b")]).replace(b"<rss", b"<!DOCTYPE rss [<!ENTITY x 'y'>]><rss", 1),
                                  rss([("a", "b")]).replace(b"<channel>", b"<!DOCTYPE x><channel>", 1),
                                  "<rss><channel/></rss>".encode("utf-16"), b"<rss>\x00</rss>"])
def test_malformed_or_dangerous_feeds_are_refused(data):
    with pytest.raises(P.PodcastError) as e:
        P.parse_feed(data, FEED)
    assert e.value.kind == "feed"


def test_an_entity_bomb_is_never_expanded():
    bomb = (b'<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]>'
            b"<rss><channel><item><title>&b;</title></item></channel></rss>")
    with pytest.raises(P.PodcastError):
        P.parse_feed(bomb, FEED)


def test_only_https_audio_items_are_listed_and_the_list_is_capped():
    items = "".join(f'<item><title>t{n}</title><enclosure url="{AUDIO.format(n=n)}" type="audio/mpeg"/></item>' for n in range(30))
    bad = ('<item><title>plain</title><enclosure url="http://cdn.example.org/x.mp3" type="audio/mpeg"/></item>'
           '<item><title>video</title><enclosure url="https://cdn.example.org/x.mp4" type="video/mp4"/></item>'
           '<item><title>none</title></item>')
    data = f"<rss><channel>{bad}{items}</channel></rss>".encode()
    eps = P.parse_feed(data, FEED)
    assert len(eps) == P.MAX_EPISODES and all(e["audio_url"].startswith("https://") for e in eps)
    assert "plain" not in [e["title"] for e in eps] and "video" not in [e["title"] for e in eps]


def test_an_explicit_episode_is_marked():
    data = rss([("A", "b")], extra="<itunes:explicit>yes</itunes:explicit>")
    assert P.parse_feed(data, FEED)[0]["explicit"] is True
    assert P.parse_feed(rss([("A", "b")], explicit="<itunes:explicit>true</itunes:explicit>"), FEED)[0]["explicit"] is True
    assert P.parse_feed(rss([("A", "b")]), FEED)[0]["explicit"] is False


# --- fetching safely ---------------------------------------------------------------------------------------

class Resp:
    def __init__(self, body=b"", ctype="application/rss+xml", length=None, chunks=None):
        self.chunks = list(chunks) if chunks is not None else [body[i:i + 4096] for i in range(0, len(body), 4096)]
        self.headers = type("H", (), {"get_content_type": lambda s: ctype,
                                      "get": lambda s, k, d="": (str(length) if length is not None else d)})()

    def read(self, n=-1):
        return self.chunks.pop(0) if self.chunks else b""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Opener:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc, self.urls = resp, exc, []

    def open(self, req, timeout=None):
        self.urls.append(req.full_url)
        if self.exc:
            raise self.exc
        return self.resp


@pytest.mark.parametrize("url", ["http://pods.example.org/f.xml", "ftp://x.example.org/f", "file:///etc/passwd", "https://",
                                 "https://" + "user:pw@pods.example.org/f.xml", "https://pods.example.org:8443/f.xml",
                                 "https://pods.example.org/a b", "javascript:alert(1)", "", None, "https://x.example.org/" + "a" * 3000])
def test_non_https_and_odd_addresses_are_refused_before_any_request(url):
    op = Opener(Resp(b"x"))
    with pytest.raises(P.PodcastError) as e:
        P.fetch(url, op=op)
    assert e.value.kind == "url" and not op.urls


def test_a_huge_feed_is_refused_by_its_header_and_by_its_body():
    with pytest.raises(P.PodcastError) as e:
        P.fetch(FEED, op=Opener(Resp(b"x", length=P.MAX_FEED_BYTES + 1)))
    assert e.value.kind == "too_big"
    endless = Resp(chunks=[b"x" * 65536] * 200)
    with pytest.raises(P.PodcastError) as e:
        P.fetch(FEED, limit=100_000, op=Opener(endless))
    assert e.value.kind == "too_big" and len(endless.chunks) > 190  # stopped early, not read to the end


def test_a_slow_feed_runs_out_of_time():
    ticks = iter(range(0, 1000, 5))
    resp = Resp(chunks=[b"x" * 10] * 50)
    with pytest.raises(P.PodcastError) as e:
        P.fetch(FEED, op=Opener(resp), clock=lambda: next(ticks))
    assert e.value.kind == "fetch"


def test_a_network_failure_is_a_plain_fetch_error():
    with pytest.raises(P.PodcastError) as e:
        P.fetch(FEED, op=Opener(exc=OSError("boom")))
    assert e.value.kind == "fetch"


def test_a_pasted_audio_link_is_not_read():
    resp = Resp(chunks=[], ctype="audio/mpeg")
    assert P.fetch("https://cdn.example.org/a.mp3", op=Opener(resp), audio_stops=True) == ("audio/mpeg", b"")


@pytest.mark.parametrize("address", ["127.0.0.1", "10.1.2.3", "192.168.0.9", "172.16.5.5", "169.254.169.254", "::1",
                                     "fe80::1", "fc00::1", "::ffff:10.0.0.1", "0.0.0.0", "100.64.0.1", "224.0.0.1"])
def test_private_addresses_are_never_dialled(monkeypatch, address):
    monkeypatch.setattr(P, "resolve", lambda host, port: [(2, 1, 6, "", (address, port))])
    dialled = []
    monkeypatch.setattr(P.socket, "socket", lambda *a: dialled.append(a))
    with pytest.raises(P.PodcastError) as e:
        P._Connection("pods.example.org").connect()
    assert e.value.kind == "url" and not dialled


def test_a_name_with_one_private_answer_is_refused_even_beside_public_ones(monkeypatch):
    monkeypatch.setattr(P, "resolve", lambda host, port: [(2, 1, 6, "", ("93.184.216.34", port)), (2, 1, 6, "", ("10.0.0.5", port))])
    with pytest.raises(P.PodcastError):
        P._Connection("pods.example.org").connect()


def test_a_redirect_to_http_or_a_login_url_is_refused_hop_by_hop():
    r = P._Redirect()
    req = P.urllib.request.Request(FEED)
    for target in ("http://pods.example.org/x", "https://" + "u:p@pods.example.org/x", "https://pods.example.org:22/x",
                   "file:///etc/passwd"):
        with pytest.raises(P.PodcastError):
            r.redirect_request(req, None, 302, "Found", {}, target)
    assert r.max_redirections == P.MAX_REDIRECTS


def test_the_opener_has_no_proxy_handler():
    names = {h.__class__.__name__ for h in P.opener().handlers}
    assert "ProxyHandler" not in names and "_Https" in names and "_Redirect" in names


# --- downloading into the private cache ----------------------------------------------------------------------------

def test_a_download_lands_whole_or_not_at_all(tmp_path):
    dest = tmp_path / "0123456789abcdef.audio"
    assert P.download(AUDIO.format(n=1), dest, op=Opener(Resp(b"a" * 9000, ctype="audio/mpeg"))) == 9000
    assert dest.read_bytes() == b"a" * 9000 and not list(tmp_path.glob("*.part")) and (dest.stat().st_mode & 0o777) == 0o600
    big = tmp_path / "fedcba9876543210.audio"
    with pytest.raises(P.PodcastError) as e:
        P.download(AUDIO.format(n=1), big, limit=5000, op=Opener(Resp(b"a" * 9000, ctype="audio/mpeg")))
    assert e.value.kind == "too_big" and not big.exists() and not list(tmp_path.glob("fedcba*"))
    page = tmp_path / "1111111111111111.audio"
    with pytest.raises(P.PodcastError):
        P.download(AUDIO.format(n=1), page, op=Opener(Resp(b"<html>", ctype="text/html")))
    assert not page.exists()
    with pytest.raises(P.PodcastError):
        P.download(AUDIO.format(n=1), page, op=Opener(Resp(b"x", length=P.MAX_EPISODE_BYTES + 1, ctype="audio/mpeg")))


def test_the_cache_is_pruned_by_age_and_size_and_only_its_own_files(tmp_path):
    cache = tmp_path
    names = [f"{n:016x}.audio" for n in range(1, 5)]
    for i, name in enumerate(names):
        f = cache / name
        f.write_bytes(b"x" * 1000)
        old = time.time() - (5 * 86400 if i == 0 else 100 - i)
        import os
        os.utime(f, (old, old))
    (cache / "notes.txt").write_text("mine")
    (cache / "target").symlink_to(cache / "notes.txt")
    P.prune_cache(cache, max_bytes=2500, days=2)
    left = sorted(p.name for p in cache.iterdir())
    assert names[0] not in left and names[1] not in left  # too old, then the oldest over the cap
    assert names[2] in left and names[3] in left and "notes.txt" in left and "target" in left
    P.prune_cache(cache, max_bytes=2500, days=2, keep=frozenset({names[3]}), now=time.time() + 10 * 86400)
    assert names[3] in [p.name for p in cache.iterdir()] and names[2] not in [p.name for p in cache.iterdir()]


# --- the queue ---------------------------------------------------------------------------------------------------

def test_the_queue_keeps_order_and_refuses_duplicates_and_overflow():
    c = cfg()
    data = rss([(f"Episode {n}", "ok") for n in range(1, 4)])
    eps = P.parse_feed(data, FEED, "Science Hour")
    for e in reversed(eps):
        P.add_episode(c, e)
    assert [i["title"] for i in P.view(c)["queue"]] == ["Episode 1", "Episode 2", "Episode 3"]
    with pytest.raises(P.PodcastError) as e:
        P.add_episode(c, eps[0])
    assert e.value.kind == "dup"
    for n in range(P.MAX_QUEUE - 3):
        P.add_episode(c, dict(eps[0], id=f"{n:016x}", title=f"x{n}"))
    with pytest.raises(P.PodcastError) as e:
        P.add_episode(c, dict(eps[0], id="f" * 16))
    assert e.value.kind == "full" and len(P.view(c)["queue"]) == P.MAX_QUEUE


def test_remove_and_the_transport_controls_edit_the_queue_file():
    c = cfg()
    for e in P.parse_feed(rss([("A", "x"), ("B", "x")]), FEED):
        P.add_episode(c, e)
    first = P.view(c)["queue"][0]["id"]
    assert P.remove(first) and not P.remove(first) and len(P.view(c)["queue"]) == 1
    P.control("pause")
    assert P.view(c)["paused"] is True
    P.control("play")
    assert P.view(c)["paused"] is False
    P.control("stop")
    assert P.view(c)["queue"] == []
    with pytest.raises(P.PodcastError):
        P.control("rewind")


def test_the_state_file_is_private_and_a_damaged_one_reads_as_empty():
    c = cfg()
    P.add_episode(c, P.parse_feed(rss([("A", "x")]), FEED)[0])
    f = P.folder() / "queue.json"
    assert (f.stat().st_mode & 0o777) == 0o600 and (P.folder().stat().st_mode & 0o777) == 0o700
    f.write_text("{not json")
    assert P.view(c)["queue"] == []
    f.write_text(json.dumps({"items": [{"id": "../../etc/passwd", "url": "x"}, "junk", {"id": "a" * 16, "url": "u", "title": "t"}]}))
    assert [i["id"] for i in P.view(c)["queue"]] == ["a" * 16]


def test_a_pick_is_read_from_the_owners_feed_never_from_the_page():
    c = cfg()
    data = rss([("Only", "fine")])
    ep = P.parse_feed(data, FEED, "Science Hour")[0]
    item = P.add_picked(c, "science hour", ep["id"], feed_fetcher(data))
    assert item["url"] == ep["audio_url"] and item["feed"] == "Science Hour"
    for feed, eid in (("Nope", ep["id"]), ("Science Hour", "0" * 16), ("Science Hour", "../x")):
        with pytest.raises(P.PodcastError) as e:
            P.add_picked(c, feed, eid, feed_fetcher(data))
        assert e.value.kind == "unknown"


def test_a_pasted_link_is_a_feed_or_an_episode_page_and_never_plain_http():
    c = cfg()
    item = P.add_link(c, "https://other.example.org/feed", feed_fetcher(rss([("Fresh", "ok")])))
    assert item["title"] == "Fresh"
    page = (b'<html><head><title>Site</title><meta property="og:title" content="Talk &amp; Tea">'
            b'<meta property="og:description" content="A calm chat"><meta property="og:audio" content="/a/talk.mp3">'
            b"</head><body></body></html>")
    item = P.add_link(c, "https://other.example.org/ep/7", lambda url, *a, **k: ("text/html", page))
    assert item["title"] == "Talk & Tea" and item["url"] == "https://other.example.org/a/talk.mp3"
    audio = lambda url, *a, **k: ("audio/mpeg", b"")
    for link in ("http://cdn.example.org/a.mp3", "ftp://a.example.org/x"):
        with pytest.raises(P.PodcastError) as e:
            P.add_link(c, link, audio)
        assert e.value.kind == "url"
    with pytest.raises(P.PodcastError) as e:
        P.add_link(c, "https://other.example.org/page", feed_fetcher(b"<html>nothing</html>"))
    assert e.value.kind == "feed"
    insecure = b'<html><meta property="og:title" content="T"><meta property="og:audio" content="http://cdn.example.org/a.mp3"></html>'
    with pytest.raises(P.PodcastError):
        P.add_link(c, "https://other.example.org/p", lambda url, *a, **k: ("text/html", insecure))


def test_the_configured_feed_list_is_read_safely():
    c = cfg(radio_podcasts=[{"name": " Science  Hour ", "url": FEED}, {"name": "x", "url": "http://insecure.example.org/f"},
                            {"name": "", "url": FEED}, "junk", {"name": "No url"}])
    assert [f["name"] for f in P.feeds(c)] == ["Science Hour"]
    assert P.feeds(Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER)) == []  # default: empty


def test_the_config_validates_radio_podcasts():
    ok = from_dict({"radio_podcasts": [{"name": "A", "url": "https://a.example.org/f"}]})
    assert ok.radio_podcasts[0]["name"] == "A" and from_dict({}).radio_podcasts == []
    for bad in ("x", [{"name": "A", "url": "http://a.example.org/f"}], [{"name": "", "url": "https://a.example.org"}], [1], {"a": "b"}):
        with pytest.raises(Exception):
            from_dict({"radio_podcasts": bad})


# --- the suitability gate ------------------------------------------------------------------------------------------

def rules_file(tmp_path, pattern="badword|גרוע"):
    f = tmp_path / "rules.json"
    f.write_text(json.dumps({"pattern": pattern}), encoding="utf-8")
    return str(f)


def test_an_episode_is_checked_on_its_title_and_description_before_it_is_queued(tmp_path):
    c = cfg(radio={"room_suitability_file": rules_file(tmp_path, "forbidden")})
    bad_title = P.parse_feed(rss([("A badword here", "fine")]), FEED)[0]       # the shared checker
    bad_desc = P.parse_feed(rss([("Fine", "this is גרוע really")]), FEED)[0]    # the shared checker
    own_rule = P.parse_feed(rss([("Fine", "a forbidden thing")]), FEED)[0]       # the radio's own rule file
    good = P.parse_feed(rss([("Fine", "all good")]), FEED)[0]
    for ep in (bad_title, bad_desc, own_rule):
        with pytest.raises(P.PodcastError) as e:
            P.add_episode(c, ep)
        assert e.value.kind == "unsuitable"
    assert P.view(c)["queue"] == []
    P.add_episode(c, good)
    assert len(P.view(c)["queue"]) == 1


def test_with_no_shared_checker_nothing_is_queued(tmp_path, monkeypatch):
    from ctb import radio_suitability
    monkeypatch.setattr(radio_suitability, "SHARED_CHECKER", tmp_path / "missing.py")
    ep = P.parse_feed(rss([("Fine", "all good")]), FEED)[0]
    with pytest.raises(P.PodcastError) as e:
        P.add_episode(cfg(), ep)
    assert e.value.kind == "rules" and P.view(cfg())["queue"] == []


def test_the_checker_path_is_configurable_and_a_crashing_checker_refuses(tmp_path):
    ep = P.parse_feed(rss([("Fine", "all good")]), FEED)[0]
    ok = tmp_path / "ok.py"
    ok.write_text("import sys\nsys.exit(0)\n")
    P.add_episode(cfg(radio={"podcast_checker": str(ok)}), ep)
    crash = tmp_path / "crash.py"
    crash.write_text("raise RuntimeError('x')\n")
    with pytest.raises(P.PodcastError) as e:
        P.add_episode(cfg(radio={"podcast_checker": str(crash)}), dict(ep, id="b" * 16))
    assert e.value.kind == "unsuitable"


def test_an_episode_with_no_title_and_no_description_is_refused():
    with pytest.raises(P.PodcastError) as e:
        P.add_episode(cfg(), dict(P.parse_feed(rss([("x", "y")]), FEED)[0], title="", raw_title="", description=""))
    assert e.value.kind == "notext"


def test_an_explicit_episode_is_refused_even_with_a_checker():
    ep = P.parse_feed(rss([("A", "b")], extra="<itunes:explicit>yes</itunes:explicit>"), FEED)[0]
    with pytest.raises(P.PodcastError) as e:
        P.add_episode(cfg(), ep)
    assert e.value.kind == "explicit"


def test_a_rule_file_that_cannot_be_read_refuses_everything(tmp_path):
    (tmp_path / "broken.json").write_text("{nope")
    ep = P.parse_feed(rss([("Fine", "ok")]), FEED)[0]
    for path in (str(tmp_path / "broken.json"), str(tmp_path / "missing.json")):
        with pytest.raises(P.PodcastError) as e:
            P.add_episode(cfg(radio={"room_suitability_file": path}), ep)
        assert e.value.kind == "rules"


def test_a_bare_audio_link_has_no_text_to_judge_so_it_is_refused():
    c = cfg()
    for name in ("talk.mp3", "badword-talk.mp3"):
        with pytest.raises(P.PodcastError) as e:
            P.add_link(c, "https://cdn.example.org/" + name, lambda url, *a, **k: ("audio/mpeg", b""))
        assert e.value.kind == "notext"
    assert P.view(c)["queue"] == []


def test_a_page_or_feed_text_is_what_is_checked_for_a_link():
    c = cfg()
    page = (b'<html><meta property="og:title" content="Nice"><meta name="description" content="a badword inside">'
            b'<audio src="https://cdn.example.org/a.mp3"></audio></html>')
    with pytest.raises(P.PodcastError) as e:
        P.add_link(c, "https://other.example.org/ep", lambda url, *a, **k: ("text/html", page))
    assert e.value.kind == "unsuitable"


# --- the radio's talk pauses under an episode and resumes after it ---------------------------------------------------

class FakeProc:
    """Stands in for ffmpeg: a pipe holding `seconds` of audio."""

    def __init__(self, seconds):
        import io
        self.stdout = io.BytesIO(b"\x01\x00" * int(seconds * radio.RATE))
        self.killed = False

    def kill(self):
        self.killed = True


def make_player(tmp_path, seconds=0.2, blocked=lambda: False, fail=None):
    mixer = radio.Mixer(bytes(2 * radio.RATE))
    procs = []

    def make(path):
        assert Path(path).exists()
        p = FakeProc(seconds)
        procs.append(p)
        return P.Feed(path, spawn=lambda _p: p)

    def get(url, dest, cancel=None):
        if fail:
            raise fail
        dest.write_bytes(b"audio")
        return 5

    player = P.Player(mixer, mixer.lock, blocked=blocked, get=get, make=make, cfg=cfg())
    return player, mixer, procs


def drain(mixer, frames):
    return [mixer.next() for _ in range(frames)]


def wait_for(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def start(player, timeout=3.0):
    """Step until the head of the queue is on the mixer (its download runs on its own thread)."""
    end = time.time() + timeout
    while time.time() < end:
        player.step()
        if player.feed is not None or player.cur is None:
            return
        time.sleep(0.01)


def queue_one(c=None, n=1):
    c = c or cfg()
    ep = P.parse_feed(rss([("Episode", "ok")]), FEED, "Science Hour")[0]
    return P.add_episode(c, ep)


def test_the_episode_plays_instead_of_the_bed_while_the_radios_clips_wait(tmp_path):
    player, mixer, procs = make_player(tmp_path, seconds=0.2)
    item = queue_one()
    clip = b"\x07\x00" * (radio.FRAME * 3)
    mixer.add(clip, tag=1)
    start(player)
    assert mixer.show is not None and player.cur["id"] == item["id"]
    assert wait_for(lambda: mixer.show.size > 0)
    first = mixer.next()
    assert first[:2] == b"\x01\x00" and mixer.clips and mixer.voice is None  # the episode, the clip is held
    frames = 1
    while mixer.show is not None and frames < 200:
        out = mixer.next()
        if mixer.show is not None:
            assert out[:2] != b"\x07\x00"  # no clip leaked in during the show
        frames += 1
    assert mixer.show is None and mixer.gap > 0  # it ended
    spoken = []
    for _ in range(400):
        out = mixer.next()
        if out[:2] == b"\x07\x00":
            spoken.append(1)
            break
    assert spoken, "the held clip is said after the episode"
    assert not mixer.clips


def test_a_clip_already_on_the_air_is_never_cut_by_an_episode(tmp_path):
    player, mixer, procs = make_player(tmp_path, seconds=1.0)
    clip = b"\x07\x00" * (radio.FRAME * 6)
    mixer.add(clip, tag=1)
    for _ in range(200):  # until the clip is on the air
        if mixer.next()[:2] == b"\x07\x00":
            break
    assert mixer.voice is not None
    queue_one()
    start(player)
    assert wait_for(lambda: mixer.show and mixer.show.size > 0)
    heard = [mixer.next() for _ in range(5)]
    assert all(h[:2] == b"\x07\x00" for h in heard)  # still the whole clip, the episode waits for its end
    assert mixer.next()[:2] == b"\x01\x00"  # then the episode


def test_a_paused_episode_leaves_the_mix_so_the_talk_goes_on_and_play_resumes_it(tmp_path):
    player, mixer, _ = make_player(tmp_path, seconds=2.0)
    queue_one()
    start(player)
    assert wait_for(lambda: mixer.show and mixer.show.size > 0)
    assert mixer.next()[:2] == b"\x01\x00"
    mixer.add(b"\x07\x00" * radio.FRAME, tag=1)            # a session reply arrives while it plays: it waits
    P.control("pause")
    player.step()
    played = mixer.show.played
    heard = [mixer.next() for _ in range(5)]
    assert any(h[:2] == b"\x07\x00" for h in heard)           # the radio's talk is said, not held for ever
    assert mixer.show is not None and mixer.show.played == played and not mixer.clips   # nothing of the episode leaked
    assert P.view(cfg())["paused"] is True and not radio.private_audio_on()
    P.control("play")
    player.step()
    assert radio.private_audio_on()
    resumed = [mixer.next()[:2] for _ in range(60)]
    assert b"\x01\x00" in resumed and mixer.show.played > played


def test_the_marker_for_home_only_audio_follows_what_is_audible(tmp_path):
    player, mixer, _ = make_player(tmp_path, seconds=3.0)
    assert not radio.private_audio_on()
    queue_one()
    start(player)
    assert radio.private_audio_on()
    P.control("pause")
    player.step()
    assert not radio.private_audio_on()
    P.control("play")
    player.step()
    assert radio.private_audio_on()
    P.control("stop")
    player.step()
    assert not radio.private_audio_on() and mixer.show is None
    queue_one()
    start(player)
    player.stop_all()
    assert not radio.private_audio_on()


def test_a_stale_marker_no_longer_counts():
    import os
    path = radio.folder() / radio.PRIVATE_AUDIO
    path.write_text("")
    assert radio.private_audio_on()
    old = time.time() - radio.PRIVATE_AUDIO_FRESH - 5
    os.utime(path, (old, old))
    assert not radio.private_audio_on()


def test_a_slow_download_never_blocks_stop_skip_or_remove(tmp_path):
    release = threading.Event()
    mixer = radio.Mixer(bytes(2 * radio.RATE))
    cancelled = []

    def get(url, dest, cancel=None):
        release.wait(5)
        if cancel is not None and cancel.is_set():
            cancelled.append(1)
            raise P.PodcastError("fetch")
        dest.write_bytes(b"audio")

    player = P.Player(mixer, mixer.lock, get=get, make=lambda path: P.Feed(path, spawn=lambda _p: FakeProc(1.0)), cfg=cfg())
    c = cfg()
    for e in P.parse_feed(rss([("A", "x"), ("B", "x")]), FEED, "S"):
        P.add_episode(c, e)
    player.step()
    assert player.dl is not None and player.dl.done.is_set() is False
    began = time.time()
    P.control("skip")
    player.step()                                    # returns at once, with the download still running
    assert time.time() - began < 1.0 and player.cur is not None and player.cur["title"] == "A"
    P.control("stop")
    player.step()
    assert player.cur is None and player.dl is None and P.view(c)["queue"] == []
    release.set()
    assert wait_for(lambda: cancelled)
    assert not list(P.cache_dir().glob("*.audio"))


def test_the_decoder_is_started_with_a_local_file_audio_only_whitelist(monkeypatch):
    seen = {}
    monkeypatch.setattr(P.subprocess, "Popen", lambda argv, **kw: seen.setdefault("argv", argv))
    P._spawn("/tmp/x.audio")
    argv = seen["argv"]
    assert argv[argv.index("-protocol_whitelist") + 1] == "file"
    formats = argv[argv.index("-format_whitelist") + 1].split(",")
    assert "mp3" in formats and not {"hls", "concat", "m3u8", "ffconcat", "sdp", "rtsp", "http"} & set(formats)
    assert argv.index("-format_whitelist") < argv.index("-i")


def test_skip_moves_to_the_next_episode_and_stop_empties_everything(tmp_path):
    player, mixer, procs = make_player(tmp_path, seconds=5.0)
    c = cfg()
    for e in P.parse_feed(rss([("A", "x"), ("B", "x")]), FEED, "S"):
        P.add_episode(c, e)
    titles = lambda: [i["title"] for i in P.view(c)["queue"]]
    start(player)
    assert player.cur["title"] == "B" and titles() == ["B", "A"]
    first = mixer.show
    P.control("skip")
    player.step()
    assert first.stopped and procs[0].killed and titles() == ["A"]
    assert player.cur["title"] == "A" and mixer.show is not first
    P.control("stop")
    player.step()
    assert mixer.show is None and titles() == [] and player.cur is None
    assert not list(P.cache_dir().glob("*.audio"))  # the cache is cleaned


def test_removing_the_playing_episode_stops_it_and_a_bad_download_is_skipped(tmp_path):
    player, mixer, _ = make_player(tmp_path, seconds=5.0)
    item = queue_one()
    start(player)
    assert mixer.show is not None
    P.remove(item["id"])
    player.step()
    assert mixer.show is None
    bad, mixer2, _ = make_player(tmp_path, fail=P.PodcastError("too_big"))
    queue_one()
    start(bad)
    for _ in range(100):
        bad.step()
        if not P.view(cfg())["queue"]:
            break
        time.sleep(0.01)
    assert mixer2.show is None and P.view(cfg())["queue"] == []
    assert P.view(cfg(), now=time.time())["now"]["error"] == "too_big"


def test_a_finished_episode_leaves_the_queue_and_the_cache(tmp_path):
    player, mixer, _ = make_player(tmp_path, seconds=0.1)
    queue_one()
    start(player)
    assert wait_for(lambda: mixer.show and mixer.show.eof)
    for _ in range(100):
        mixer.next()
    assert mixer.show is None
    player.step()
    assert player.cur is None and P.view(cfg())["queue"] == [] and not list(P.cache_dir().glob("*.audio"))


def test_nothing_starts_while_paused_or_while_a_room_is_live(tmp_path):
    live = [True]
    player, mixer, _ = make_player(tmp_path, blocked=lambda: live[0])
    queue_one()
    start(player)
    assert mixer.show is None
    live[0] = False
    P.control("pause")
    player.step()
    assert mixer.show is None
    P.control("play")
    start(player)
    assert mixer.show is not None


def test_going_quiet_stops_the_episode_but_keeps_the_queue(tmp_path):
    player, mixer, procs = make_player(tmp_path, seconds=5.0)
    queue_one()
    start(player)
    player.stop_all()
    assert mixer.show is None and procs[0].killed and len(P.view(cfg())["queue"]) == 1


def test_an_undecodable_episode_ends_with_an_error_and_never_hangs_the_radio(tmp_path):
    player, mixer, _ = make_player(tmp_path, seconds=0.0)
    queue_one()
    start(player)
    assert wait_for(lambda: mixer.show is None or mixer.show.eof)
    mixer.next()
    player.step()
    assert mixer.show is None and P.view(cfg())["queue"] == []


def test_the_view_says_the_radio_is_not_running_when_the_status_is_stale(tmp_path):
    player, mixer, _ = make_player(tmp_path, seconds=5.0)
    queue_one()
    start(player)
    c = cfg()
    assert P.view(c)["running"] is True and P.view(c)["now"]["state"] == "playing"
    later = P.view(c, now=time.time() + 60)
    assert later["running"] is False and later["now"] is None


def test_routine_updates_do_not_pile_up_behind_an_episode_but_replies_still_queue():
    src = Path(radio.__file__).read_text(encoding="utf-8")
    body = src[src.index("    def content_loop"):src.index("    # the stream")]
    assert "self.mixer.show_playing()" in body and body.index("self.spoken_requests()") < body.index("self.mixer.show_playing()")


def test_mirroring_off_silences_the_episode_with_the_rest(monkeypatch):
    src = Path(radio.__file__).read_text(encoding="utf-8")
    assert "player.stop_all()" in src[src.index("    def silence"):src.index("    def give_up")]
    loop = src[src.index("    def podcast_loop"):src.index("    def board_wanted")]
    assert "policy.on()" in loop and loop.index("policy.on()") < loop.index("self.player.step()")


# --- privacy: the owner's private radio only -------------------------------------------------------------------------

def test_podcast_audio_never_reaches_the_youtube_staff_radio():
    for mod in (radio_youtube, radio_staff):
        text = Path(mod.__file__).read_text(encoding="utf-8")
        assert "radio_podcast" not in text and "podcast" not in text.lower(), mod.__name__
    assert not [s for s in radio_staff.SECTIONS if "podcast" in s or "audio" in s]


def test_only_the_telegram_station_owns_a_player():
    import inspect
    assert "Player(" in inspect.getsource(radio.Station.__init__)
    assert not hasattr(radio_youtube, "Player")
    src = Path(radio_youtube.__file__).read_text(encoding="utf-8")
    assert "mixer.show" not in src  # the staff mixer never gets a show


def test_nothing_about_an_episode_is_spoken_captioned_or_sent_to_the_alerts_channel():
    import ast
    tree = ast.parse(Path(P.__file__).read_text(encoding="utf-8"))
    code = ast.unparse(tree.body[1:])  # the module's words, without its docstring
    for name in ("post_channel", "captions", "headline", "speak(", "radio_post", "radio_script", "bulletin"):
        assert name not in code, name


def test_an_audio_address_must_be_https_when_queued():
    c = cfg()
    ep = P.parse_feed(rss([("A", "b")]), FEED)[0]
    with pytest.raises(P.PodcastError) as e:
        P.add_episode(c, dict(ep, audio_url="http://cdn.example.org/a.mp3"))
    assert e.value.kind == "url"


# --- the /podcast command ----------------------------------------------------------------------------------------------

def run_command(c, arg, fetcher=None):
    out, done = [], threading.Event()

    def reply(text):
        out.append(text)
        done.set()
    assert P.telegram_command(c, arg, reply, fetcher or feed_fetcher(rss([("Older", "ok"), ("Latest ep", "ok")]))) == "podcast"
    assert done.wait(5)
    return out[0]


def test_the_command_queues_the_latest_episode_of_a_named_feed():
    c = cfg()
    text = run_command(c, "science hour")
    assert "Latest ep" in text  # no dates in that feed: the first item is the latest
    assert [i["title"] for i in P.view(c)["queue"]] == ["Latest ep"]
    data = rss([("One", "x"), ("Two", "x")])
    ep_dates = P.parse_feed(data, FEED)
    assert ep_dates[0]["title"] == "Two"  # the newest by date is "latest"
    P.control("stop")
    assert "Two" in run_command(c, "Science Hour", feed_fetcher(data))


def test_the_command_takes_a_link_and_says_why_it_refuses(tmp_path):
    c = cfg(radio={"room_suitability_file": rules_file(tmp_path)})
    assert "Fresh" in run_command(c, "https://other.example.org/feed", feed_fetcher(rss([("Fresh", "ok")])))
    assert "suitability" in run_command(c, "https://other.example.org/feed2", feed_fetcher(rss([("badword", "ok")])))
    assert "not allowed" in run_command(c, "http://other.example.org/feed3")
    assert "not allowed" in run_command(c, "no such feed")  # neither a feed name nor an https link


def test_the_command_controls_and_reports_in_hebrew_too():
    c = cfg()
    c.ui_language = "he"
    assert "שום פודקאסט" in run_command(c, "")
    assert re.search(r"[א-ת]", run_command(c, "דלג"))
    assert re.search(r"[א-ת]", run_command(c, "https://x.example.org/f", feed_fetcher(b"junk")))
    c.ui_language = "en"
    assert "skipped" in run_command(c, "skip") and "stopped" in run_command(c, "stop")


def test_the_command_is_owner_only_in_the_home_group_like_radio():
    src = Path(radio.__file__).parent.joinpath("inbox.py").read_text(encoding="utf-8")
    block = src[src.index('== "/podcast"'):]
    block = block[:block.index("history_query")]
    assert 'chat.get("id") != home_chat(cfg)' in block and "radio_podcast.telegram_command" in block
    # the /radio command, one block above, sits after the owner check, in the same function
    assert src.index('== "/radio"') < src.index('== "/podcast"')


def test_the_i18n_keys_exist_in_both_languages():
    from ctb.i18n import EN, HE
    keys = {k for k in EN if k.startswith(("podcast_", "cmd_podcast"))}
    assert keys == {k for k in HE if k.startswith(("podcast_", "cmd_podcast"))} and len(keys) > 15
    for k in P.PodcastError.__doc__.split(":", 1)[1].replace("\"", "").split("|"):
        k = k.strip()
        if k in ("bad", "url", "fetch", "feed", "too_big", "unknown", "full", "dup", "unsuitable", "explicit", "rules", "decode"):
            assert f"podcast_err_{k}" in EN, k


def test_the_new_cockpit_has_the_podcast_tile_title_in_both_languages():
    from ctb import panel_page
    he, en = panel_page.V2_STRINGS["he"]["tools"], panel_page.V2_STRINGS["en"]["tools"]
    assert he["podcast"] and en["podcast"] and not re.search(r"[A-Za-z]", re.sub("⁨[^⁩]*⁩", "", he["podcast"]))
    assert "podcastCard(d.podcast,d.radio)" in (Path(panel_page.__file__).parent / "cockpit_v2_screens.js").read_text(encoding="utf-8")


# --- the time limit holds against a server that trickles ------------------------------------------------------------

@pytest.fixture
def trickle(monkeypatch, tmp_path):
    """A local TLS server that answers at once and then sends one byte a second for ever, and the module's network
    guard pointed at it (the only things replaced are the address check, the port rule and the trusted CA)."""
    import socket
    import ssl
    import subprocess
    key, crt = tmp_path / "k.pem", tmp_path / "c.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(crt),
                    "-days", "1", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost"],
                   check=True, capture_output=True)
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(str(crt), str(key))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    port = listener.getsockname()[1]
    stop = threading.Event()

    def serve():
        listener.settimeout(0.2)
        while not stop.is_set():
            try:
                raw, _ = listener.accept()
            except OSError:
                continue
            try:
                conn = server_ctx.wrap_socket(raw, server_side=True)
                conn.recv(4096)
                conn.sendall(b"HTTP/1.0 200 OK\r\nContent-Type: audio/mpeg\r\n\r\n")
                for _ in range(60):
                    if stop.is_set():
                        break
                    conn.sendall(b"x")
                    time.sleep(1)
                conn.close()
            except OSError:
                pass

    threading.Thread(target=serve, daemon=True).start()
    client_ctx = ssl.create_default_context(cafile=str(crt))
    monkeypatch.setattr(P.ssl, "create_default_context", lambda *a, **k: client_ctx)
    monkeypatch.setattr(P, "resolve", lambda host, port_: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))])
    monkeypatch.setattr(P, "public", lambda address: True)
    monkeypatch.setattr(P, "check_url", lambda url: None)
    yield "https://localhost:%d/slow.mp3" % port
    stop.set()
    listener.close()


@pytest.mark.skipif(not __import__("shutil").which("openssl"), reason="openssl is not installed")
def test_a_server_that_trickles_cannot_hold_a_download_past_its_limit(trickle, monkeypatch, tmp_path):
    monkeypatch.setattr(P, "DOWNLOAD_DEADLINE", 2.0)
    dest = tmp_path / "0123456789abcdef.audio"
    began = time.time()
    with pytest.raises(P.PodcastError) as e:
        P.download(trickle, dest)
    assert e.value.kind == "fetch" and time.time() - began < 8      # without the watchdog this waits for the whole 60 s
    assert not dest.exists() and not list(tmp_path.glob("*.part"))


@pytest.mark.skipif(not __import__("shutil").which("openssl"), reason="openssl is not installed")
def test_a_server_that_trickles_cannot_hold_a_feed_fetch_past_its_limit(trickle, monkeypatch):
    monkeypatch.setattr(P, "FETCH_DEADLINE", 2.0)
    began = time.time()
    with pytest.raises(P.PodcastError) as e:
        P.fetch(trickle)
    assert e.value.kind == "fetch" and time.time() - began < 8


# --- text, and the same rules again before the episode is heard ---------------------------------------------------------

def test_a_feed_item_with_no_title_and_no_description_is_refused_not_judged_on_its_file_name():
    data = (b'<rss><channel><item><enclosure url="https://cdn.example.org/clean-name.mp3" type="audio/mpeg"/></item>'
            b'<item><title></title><description>  </description><enclosure url="https://cdn.example.org/b.mp3" type="audio/mpeg"/></item>'
            b"</channel></rss>")
    eps = P.parse_feed(data, FEED)
    assert [e["raw_title"] for e in eps] == ["", ""] and eps[0]["title"] == "clean-name.mp3" and eps[1]["title"] == "b.mp3"
    for ep in eps:
        with pytest.raises(P.PodcastError) as e:
            P.add_episode(cfg(), ep)
        assert e.value.kind == "notext"
    assert P.view(cfg())["queue"] == []
    with pytest.raises(P.PodcastError) as e:
        P.add_episode(cfg(), dict(eps[0], title="?", raw_title="?"))
    assert e.value.kind == "notext"


def test_the_rules_are_applied_again_just_before_an_episode_is_played(tmp_path, monkeypatch):
    from ctb import radio_suitability
    ok = P.parse_feed(rss([("Quiet walk", "birds")]), FEED)[0]
    player, mixer, _ = make_player(tmp_path, seconds=2.0)
    P.add_episode(cfg(), ok)
    # the word list changes after it was queued
    stricter = tmp_path / "stricter.py"
    stricter.write_text("import sys\nsys.exit(1 if 'birds' in open(sys.argv[1]).read() else 0)\n")
    monkeypatch.setattr(radio_suitability, "SHARED_CHECKER", stricter)
    start(player)
    for _ in range(20):
        player.step()
    assert mixer.show is None and P.view(cfg())["queue"] == []
    assert P.view(cfg(), now=time.time())["now"]["error"] == "unsuitable"


def test_the_last_look_before_it_is_heard_also_holds(tmp_path, monkeypatch):
    from ctb import radio_suitability
    ok = P.parse_feed(rss([("Quiet walk", "birds")]), FEED)[0]
    release = threading.Event()
    mixer = radio.Mixer(bytes(2 * radio.RATE))

    def get(url, dest, cancel=None):
        release.wait(5)
        dest.write_bytes(b"audio")
    player = P.Player(mixer, mixer.lock, get=get, make=lambda path: P.Feed(path, spawn=lambda _p: FakeProc(2.0)), cfg=cfg())
    P.add_episode(cfg(), ok)
    player.step()                                                  # downloading
    stricter = tmp_path / "stricter.py"
    stricter.write_text("import sys\nsys.exit(1)\n")
    monkeypatch.setattr(radio_suitability, "SHARED_CHECKER", stricter)
    release.set()
    for _ in range(100):
        player.step()
        if player.cur is None:
            break
        time.sleep(0.01)
    assert mixer.show is None and not radio.private_audio_on() and P.view(cfg())["queue"] == []


def test_english_text_is_screened_by_the_word_list_because_the_shared_checker_reads_hebrew():
    for title in ("A murder mystery", "Naked truth", "all about sex"):
        ep = P.parse_feed(rss([(title, "fine")]), FEED)[0]
        with pytest.raises(P.PodcastError) as e:
            P.add_episode(cfg(), ep)
        assert e.value.kind == "unsuitable", title                   # the stand-in checker accepts all of these
    ep = P.parse_feed(rss([("Gardening for beginners", "soil and seeds")]), FEED)[0]
    assert P.add_episode(cfg(), ep)["title"] == "Gardening for beginners"


def test_text_in_a_script_no_rule_covers_is_refused():
    for title in ("Подкаст о погоде", "برنامج عن الطقس", "天気の話"):
        ep = P.parse_feed(rss([(title, "ok")]), FEED)[0]
        with pytest.raises(P.PodcastError) as e:
            P.add_episode(cfg(), ep)
        assert e.value.kind == "unsuitable", title
    mixed = P.parse_feed(rss([("שיחה על Python", "וגם café")]), FEED)[0]       # Hebrew, Latin and accents are covered
    assert P.add_episode(cfg(), mixed)["feed"] == ""


def test_the_docs_say_which_languages_are_checked():
    text = (Path(radio.__file__).parent.parent.parent / "docs" / "radio.md").read_text(encoding="utf-8")
    text = " ".join(text.split())
    assert "portable word list" in text and "any other script is refused" in text and "just before each episode is played" in text
