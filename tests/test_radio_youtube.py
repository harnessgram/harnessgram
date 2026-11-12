"""The staff edition of the radio (radio_staff) and its YouTube side (radio_youtube).

Privacy is the point: the staff script and card are built by an allowlist over the snapshot. The property tests
walk EVERY leaf of a rich sample snapshot, change it, and prove the staff output does not move unless the leaf is
one the allowlist names. All data is sample data; the YouTube API, the Keychain and launchd are fakes."""
import copy
import threading
import os
import json
import time
import urllib.parse

import pytest

from conftest import GOOD_TOKEN
from ctb import radio, radio_script, radio_staff, radio_youtube as Y
from ctb.config import Config

OWNER = 111
NOW = time.mktime((2026, 3, 4, 15, 30, 0, 0, 0, -1))  # a fixed local afternoon: "today" is the same day

# unique markers: one per private field, Hebrew (passes the language gate) and Latin
SECRET_WORDS = ["סודאחד", "סודשתיים", "סודשלוש", "סודארבע", "סודחמש", "סודשש", "סודשבע", "סודשמונה", "סודתשע",
                "סודעשר", "PRIVATE", "sk-ant-xxxxxxxxxxxxxxxxxxxxxxxxxxxx", "/Users/someone/secret", "https://x.example"]


def _state():
    return {
        "decisions": [{"id": "dec1", "title": "סודאחד אישור תשלום", "topic": "סודשתיים", "age": 60}],
        "reminders": [{"text": "סודשלוש תזכורת", "topic": "alpha", "age": 120}],
        "sessions": [{"id": "sess1234abcd", "name": "סודארבע", "state": "working"},
                     {"id": "sess5678efgh", "name": "סודחמש PRIVATE", "state": "idle"}],
        "quota": [{"label": "סודשש", "account": "owner@example.com", "weekly": 95}],
    }


def _ledger():
    waiting = [{"key": "alpha#7", "project": "alpha", "text": "סודשבע מחכה לבעלים", "updated": NOW - 30}]
    done = [{"key": "alpha#1", "project": "alpha", "text": "הוספנו מסך חדש", "updated": NOW - 600},
            {"key": "alpha#2", "project": "alpha", "text": "תיקון https://x.example/a", "updated": NOW - 700},
            {"key": "beta#3", "project": "beta", "text": "סודשמונה פרויקט אחר", "updated": NOW - 60}]
    return waiting, done, 5


def snap():
    return radio_script.snapshot(_state(), ["sess1234"], _ledger(),
                                 [{"label": "סודתשע", "pct": 99, "window": "week"}], NOW,
                                 agents_done=[{"key": "a:x", "session": "סודעשר", "at": NOW - 10}],
                                 team_by={"alpha": 3, "beta": 4})


def prof(projects=None, sections=None):
    raw = {"enabled": True, "projects": projects or {}, "say_titles": True}  # an old key: ignored
    if sections is not None:
        raw["sections"] = sections
    return radio_staff.profile(raw, "he")


def outputs(sn, p, last=None):
    view = radio_staff.staff_view(sn, p, last)
    title, sections = radio_staff.render_inputs(view)
    return radio_staff.build_script(view), json.dumps([title, sections], ensure_ascii=False)


def _leaves(value, path=()):
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _leaves(v, path + (k,))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _leaves(v, path + (i,))
    else:
        yield path


def _set(root, path, value):
    for p in path[:-1]:
        root = root[p]
    root[path[-1]] = value


def _allowed_leaf(sn, path):
    """The only leaves the staff edition may depend on, for the allowlist {"alpha"}: times, keys and counts.
    No text field of the snapshot is among them."""
    if path == ("at",):
        return True
    if path[:1] == ("done",) and len(path) == 3:
        return sn["done"][path[1]]["project"] == "alpha" and path[2] in ("key", "project", "updated")
    return path == ("team_open_by", "alpha")


# --- the allowlist: deny by default --------------------------------------------------------------------------

def test_empty_allowlist_says_only_the_station_id():
    script, card = outputs(snap(), prof())
    assert script.startswith("רדיו הצוות, השעה ")
    assert "אין עדכונים חדשים" in script and "בפרויקט" not in script
    for word in SECRET_WORDS + ["הוספנו", "אלפא", "alpha"]:
        assert word not in script and word not in card


def test_no_private_marker_reaches_script_or_card():
    script, card = outputs(snap(), prof({"alpha": "אלפא"}))
    assert "בפרויקט אלפא" in script
    for word in SECRET_WORDS + ["הוספנו", "תיקון"]:  # not even the listed project's own item titles
        assert word not in script and word not in card, word


def test_a_private_plain_sentence_in_an_item_never_goes_out():
    """Review of #230: a plain Hebrew sentence passes every language filter, so item text is never read at all."""
    sn = snap()
    sn["done"][0]["text"] = sn["done"][0]["say"] = "סוכם שכר של עשרים אלף שקלים"
    script, card = outputs(sn, prof({"alpha": "אלפא"}))
    assert "שכר" not in script and "שכר" not in card and "שקלים" not in script


def test_every_snapshot_leaf_outside_the_allowlist_is_ignored():
    """Property: change any single leaf the allowlist does not name (to a marker, a number, a link) and the staff
    script and card stay exactly the same. Covers every section of the snapshot, present and future fields, and
    every text field (item titles included): only times, keys and counts of listed projects can move it."""
    base = snap()
    p = prof({"alpha": "אלפא"})
    want = outputs(base, p)
    checked = 0
    for path in list(_leaves(base)):
        if _allowed_leaf(base, path):
            continue
        for value in ("סודחדש PRIVATE https://x.example/a", 987654, None, NOW - 5):
            changed = copy.deepcopy(base)
            _set(changed, path, value)
            assert outputs(changed, p) == want, path
        checked += 1
    assert checked > 30  # the sample really covers the snapshot's sections


def test_items_added_anywhere_else_are_ignored():
    """Whole new private items (an owner decision, an item waiting on him, another project's work) change nothing."""
    p = prof({"alpha": "אלפא"})
    want = outputs(snap(), p)
    sn = snap()
    sn["decisions"].append({"key": "d:z", "text": "סודחדש", "topic": "alpha", "at": NOW})
    sn["ledger"].append({"key": "l:alpha#9", "project": "alpha", "text": "סודחדש", "at": NOW})
    sn["done"].append({"key": "x:gamma#1", "project": "gamma", "text": "סודחדש", "updated": NOW})
    sn["team_open_by"]["gamma"] = 9
    sn["quota"].append({"label": "סודחדש", "window": "week", "pct": 100})
    sn["extra_future_field"] = [{"text": "סודחדש"}]
    assert outputs(sn, p) == want


def test_allowed_project_facts_do_move_the_output():
    """The counterpart: the allowlisted facts are really used (so the invariance test is not vacuous)."""
    p = prof({"alpha": "אלפא"})
    sn = snap()
    before = outputs(sn, p)
    sn["team_open_by"]["alpha"] = 7
    assert outputs(sn, p) != before and "שבע משימות" in outputs(sn, p)[0]


def test_old_or_told_items_are_not_spoken_again():
    p = prof({"alpha": "אלפא"})
    sn = snap()
    view = radio_staff.staff_view(sn, p)
    last = radio_staff.remember(view, NOW)
    again = radio_staff.staff_view(sn, p, last)
    assert not radio_staff.has_news(again) and "הושלמ" not in radio_staff.build_script(again)
    assert "אלפא: שתי משימות" in json.dumps(radio_staff.card_sections(again), ensure_ascii=False)  # still counted
    sn["done"][0]["updated"] = NOW - 7 * 3600  # beyond the fresh window: never read as news
    assert sum(d["fresh"] for p_ in radio_staff.staff_view(sn, p)["projects"] for d in p_["done"]) == 1


def test_yesterday_is_not_today():
    sn = snap()
    for d in sn["done"]:
        d["updated"] = NOW - 16 * 3600  # 23:30 the day before
    view = radio_staff.staff_view(sn, prof({"alpha": "אלפא"}))
    assert view["projects"][0]["done"] == []


def test_sections_are_a_fixed_allowlist():
    p = radio_staff.profile({"projects": {"alpha": "אלפא"}, "sections": ["decisions", "quota", "in_work"]}, "he")
    assert p["sections"] == ["station_id", "in_work"]
    script, _ = outputs(snap(), p)
    assert "הושלמ" not in script and "שלוש משימות בעבודה" in script


def test_a_project_name_with_latin_or_digits_is_left_out():
    for name in ("Team app", "אלפא 2", "אלפא/בטא", "אלפא@", "🚀 אלפא"):
        script, card = outputs(snap(), prof({"alpha": name}))
        assert "בפרויקט" not in script, name


def test_hebrew_output_never_mixes_directions():
    script, card = outputs(snap(), prof({"alpha": "אלפא", "beta": "בטא"}))
    assert not radio_script.LATIN.search(script) and not any(c.isdigit() for c in script)
    for section in json.loads(card)[1]:
        for line in [section["head"]] + section["lines"]:
            assert not radio_script.LATIN.search(line) and not any(c.isdigit() for c in line), line


@pytest.mark.parametrize("text,ok", [
    ("משימה חדשה", True), ("שלום, צוות!", True), ("fix bug", False), ("גרסה 2", False),
    ("ראו https://x.example", False), ("קובץ /tmp/a", False), ("@someone", False), ("#12", False),
    ("שלום 🙂", False), ("(סוגריים)", False), ("", False), ("א" * 200, False), (None, False)])
def test_gate(text, ok):
    assert bool(radio_staff.gate(text, "he")) is ok


def test_card_check_rejects_a_bad_line():
    assert radio_staff.card_ok([{"head": "✅ הושלם היום", "lines": ["אלפא: משימה אחת"]}], "he")
    assert not radio_staff.card_ok([{"head": "✅ הושלם היום", "lines": ["owner@example.com"]}], "he")


# --- the YouTube side ---------------------------------------------------------------------------------------

REFRESH_TOKEN = "1//sample-refresh-token-value"
STREAM_KEY = "abcd-efgh-ijkl-mnop-qrst"


class Store:
    def __init__(self, **items):
        self.items = dict(items)

    def get(self, name):
        return self.items.get(name)

    def has(self, name):
        return name in self.items

    def set(self, name, value):
        self.items[name] = value

    def delete(self, name):
        self.items.pop(name, None)


class FakeGoogle:
    """Token endpoint + the Live API calls, recorded. `enabled=False`: the channel cannot stream yet."""

    def __init__(self, enabled=True, phase="ready", fail=(), fail_ids=(), channel="UCsample"):
        self.calls, self.enabled, self.phase, self.fail, self.fail_ids = [], enabled, phase, set(fail), set(fail_ids)
        self.channel = channel  # the channel the credential really belongs to (what Google answers)

    def __call__(self, method, url, body=None, form=None, token="", timeout=20.0):
        parts = urllib.parse.urlparse(url)
        path = parts.path.replace("/youtube/v3", "")
        q = dict(urllib.parse.parse_qsl(parts.query))
        self.calls.append((method, path, q, body))
        if url == Y.TOKEN_URL:
            if form.get("grant_type") == "authorization_code":
                return 200, {"access_token": "at-1", "refresh_token": REFRESH_TOKEN, "scope": Y.SCOPE}
            return 200, {"access_token": "at-2"}
        if url == Y.REVOKE_URL:
            return 200, {}
        if path in self.fail or q.get("id") in self.fail_ids:
            return 500, {"error": {"errors": [{"reason": "backendError"}]}}
        if (method, path) == ("GET", "/liveBroadcasts") and "mine" in q:
            if not self.enabled:
                return 403, {"error": {"errors": [{"reason": "liveStreamingNotEnabled"}]}}
            return 200, {"items": []}
        if (method, path) == ("GET", "/liveBroadcasts"):
            return 200, {"items": [{"status": {"lifeCycleStatus": self.phase}}]}
        if (method, path) == ("GET", "/channels"):
            return 200, {"items": [{"id": self.channel, "snippet": {"title": "Sample channel"}}]}
        if (method, path) == ("GET", "/liveStreams"):
            return 200, {"items": []}
        if (method, path) == ("POST", "/liveStreams"):
            return 200, {"id": "stream-1", "cdn": {"ingestionInfo": {
                "rtmpsIngestionAddress": "rtmps://ingest.example/live2", "streamName": STREAM_KEY}}}
        if (method, path) == ("POST", "/liveBroadcasts"):
            return 200, {"id": "bc-1"}
        if method == "POST" and path in ("/liveBroadcasts/bind", "/liveBroadcasts/transition"):
            return 200, {"id": "bc-1"}
        if (method, path) == ("DELETE", "/liveBroadcasts"):
            return 204, {}
        return 404, {}

    def did(self, method, path, bid=None):
        return [c for c in self.calls if c[0] == method and c[1] == path and (bid is None or c[2].get("id") == bid)]


def cfg(enabled=True, **extra):
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he",
                  radio={"youtube": dict({"enabled": enabled, "projects": {"alpha": "אלפא"}}, **extra)})


SAMPLE_CLIENT = {"client_id": "cid.sample", "client_secret": "sample-client-secret"}


def service(c, google, store=None):
    """A service whose stored token was issued to SAMPLE_CLIENT (the token and its record, as a sign-in leaves)."""
    if store is not None and Y.REFRESH in store.items and Y.REFRESH_CLIENT not in store.items:
        store.items[Y.REFRESH_CLIENT] = SAMPLE_CLIENT["client_id"]
    return Y.Service(Y.settings(c), store=store if store is not None else
                     Store(**{Y.REFRESH: REFRESH_TOKEN, Y.REFRESH_CLIENT: SAMPLE_CLIENT["client_id"]}),
                     request=google, client=SAMPLE_CLIENT)


@pytest.fixture(autouse=True)
def connected_sample_channel():
    """The stored connection most tests start from: the sample channel the fake Google reports."""
    with Y.connection_lock():
        Y._set_connection(connected=True, channel_id="UCsample", channel_title="Sample channel", live_enabled=True)


@pytest.fixture(autouse=True)
def no_launchd(monkeypatch):
    monkeypatch.setattr(Y, "_job_on", lambda: False)
    monkeypatch.setattr(Y, "_launchctl", lambda *a: pytest.fail("launchctl must not run in tests"))


def _files_text():
    return "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in Y.folder().rglob("*") if p.is_file())


def test_settings_default_off_and_empty():
    s = Y.settings(Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he"))
    assert s["enabled"] is False and s["projects"] == {} and s["keychain_service"] == Y.SERVICE
    assert s["max_hours"] == Y.MAX_HOURS
    assert Y.settings(cfg(max_hours=50))["max_hours"] == Y.MAX_HOURS


def test_start_refuses_when_disabled_or_not_connected():
    g = FakeGoogle()
    with pytest.raises(Y.YouTubeError) as e:
        Y.start(cfg(enabled=False), service(cfg(enabled=False), g), load_job=lambda s: None)
    assert e.value.key == "youtube_disabled"
    with pytest.raises(Y.YouTubeError) as e:
        Y.start(cfg(), service(cfg(), g, store=Store()), load_job=lambda s: None)
    assert e.value.key == "youtube_not_connected" and g.calls == []


def test_start_stops_when_live_is_not_enabled_and_creates_nothing():
    g = FakeGoogle(enabled=False)
    with pytest.raises(Y.YouTubeError) as e:
        Y.start(cfg(), service(cfg(), g), load_job=lambda s: pytest.fail("no job"))
    assert e.value.key == "youtube_not_enabled"
    assert not [c for c in g.calls if c[0] in ("POST", "DELETE") and c[1] != "/token"]


def test_start_creates_an_unlisted_broadcast_and_keeps_the_key_in_the_store():
    g, store, loaded = FakeGoogle(), Store(**{Y.REFRESH: REFRESH_TOKEN}), []
    assert Y.start(cfg(), service(cfg(), g, store), load_job=loaded.append) == "youtube_started"
    body = g.did("POST", "/liveBroadcasts")[0][3]
    assert body["status"] == {"privacyStatus": "unlisted", "selfDeclaredMadeForKids": False}
    assert body["contentDetails"]["enableAutoStart"] is True and body["contentDetails"]["enableAutoStop"] is True
    assert body["contentDetails"]["monitorStream"] == {"enableMonitorStream": False}
    assert "latencyPreference" not in body["contentDetails"]  # not settable on insert
    assert g.did("POST", "/liveStreams")[0][3]["contentDetails"] == {"isReusable": True}
    assert g.did("POST", "/liveBroadcasts/bind")[0][2] == {"id": "bc-1", "streamId": "stream-1", "part": "id,status"}
    assert store.items[Y.STREAM_KEY] == STREAM_KEY and store.items[Y.INGEST_URL].startswith("rtmps://")
    assert len(loaded) == 1
    st = Y._state()
    assert st["broadcast_id"] == "bc-1" and st["owner_start"]
    text = _files_text()
    assert STREAM_KEY not in text and REFRESH_TOKEN not in text and "ingest.example" not in text


def test_a_failed_bind_leaves_nothing_behind():
    g = FakeGoogle(fail={"/liveBroadcasts/bind"})
    with pytest.raises(Y.YouTubeError):
        Y.start(cfg(), service(cfg(), g), load_job=lambda s: pytest.fail("no job"))
    assert g.did("DELETE", "/liveBroadcasts") and not Y._state().get("broadcast_id")
    assert not Y._state().get("owner_start")


@pytest.mark.parametrize("phase,call", [("live", ("POST", "/liveBroadcasts/transition")),
                                        ("ready", ("DELETE", "/liveBroadcasts"))])
def test_stop_ends_only_its_own_broadcast(monkeypatch, phase, call):
    monkeypatch.setattr(Y, "_unload_job", lambda: None)
    g = FakeGoogle(phase=phase)
    Y._set_state(on=True, broadcast_id="bc-1", owner_start="n")
    assert Y.stop(cfg(), service(cfg(), g)) == "youtube_stopped"
    assert g.did(*call) and g.did(*call)[0][2]["id"] == "bc-1"
    if phase == "live":
        assert g.did(*call)[0][2]["broadcastStatus"] == "complete"
    assert Y._state()["broadcast_id"] == "" and not Y._state()["owner_start"]


def test_stop_keeps_the_broadcast_to_retry_when_youtube_fails(monkeypatch):
    monkeypatch.setattr(Y, "_unload_job", lambda: None)
    Y._set_state(on=True, broadcast_id="bc-1", owner_start="n")
    assert Y.stop(cfg(), service(cfg(), FakeGoogle(fail={"/liveBroadcasts"}))) == "youtube_stopped_left"
    assert Y._state()["cleanup"] == ["bc-1"] and Y.status(cfg(), service=service(cfg(), None))["cleanup_pending"] == 1


def test_status_works_offline():
    def no_network(*a, **k):
        raise AssertionError("status must not call the network")
    out = Y.status(cfg(), service=service(cfg(), no_network))
    assert out["enabled"] is True and out["projects"] == 1 and out["on"] is False
    assert REFRESH_TOKEN not in json.dumps(out)


def test_connect_stores_only_the_refresh_token(monkeypatch):
    g, store, shown = FakeGoogle(), Store(), []

    def read_line():
        while not shown:
            time.sleep(0.01)
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(shown[0]).query))
        assert q["scope"] == Y.SCOPE and q["code_challenge_method"] == "S256" and q["access_type"] == "offline"
        return q["redirect_uri"] + "?" + urllib.parse.urlencode({"state": q["state"], "code": "sample-code"})
    assert Y.connect(cfg(), service(cfg(), g, store), show=shown.append, read_line=read_line,
                     timeout=5) == "youtube_connected"
    assert store.items == {Y.REFRESH: REFRESH_TOKEN, Y.REFRESH_CLIENT: "cid.sample"}  # the token and its client
    st = Y.connection()
    assert st["connected"] and st["channel_title"] == "Sample channel" and st["live_enabled"] is True
    assert not [c for c in g.calls if c[0] in ("POST", "DELETE") and "youtube" not in c[1] and c[1] != "/token"]
    assert not [c for c in g.calls if c[0] in ("POST", "DELETE") and c[1].startswith("/live")]  # read-only
    assert REFRESH_TOKEN not in _files_text() and "sample-code" not in _files_text()


def test_connect_ignores_a_pasted_address_with_the_wrong_state():
    shown = []

    def read_line():
        while not shown:
            time.sleep(0.01)
        if not getattr(read_line, "sent", False):
            read_line.sent = True
            return "http://127.0.0.1:1/?state=forged&code=evil"
        return ""
    with pytest.raises(Y.YouTubeError) as e:
        Y.connect(cfg(), service(cfg(), FakeGoogle(), Store()), show=shown.append, read_line=read_line, timeout=0.5)
    assert e.value.key == "youtube_connect_timeout"


def test_disconnect_revokes_and_forgets():
    g, store = FakeGoogle(), Store(**{Y.REFRESH: REFRESH_TOKEN, Y.STREAM_KEY: STREAM_KEY, Y.INGEST_URL: "rtmps://x"})
    with Y.connection_lock():
        Y._set_connection(connected=True, channel_title="Sample channel")
    assert Y.disconnect(cfg(), service(cfg(), g, store)) == "youtube_disconnected"
    assert store.items == {} and [c for c in g.calls if c[0] == "POST"] and not Y.connection()["connected"]


def test_keychain_secret_goes_on_stdin_never_argv():
    seen = []

    def runner(argv, **kw):
        seen.append((argv, kw.get("input")))
        return type("R", (), {"returncode": 0, "stdout": ""})()
    Y.KeychainStore("sample.service", runner).set(Y.STREAM_KEY, STREAM_KEY)
    assert all(STREAM_KEY not in " ".join(argv) for argv, _ in seen)
    assert STREAM_KEY in seen[0][1] and seen[0][0] == ["security", "-i"]
    with pytest.raises(Y.YouTubeError):
        Y.KeychainStore("sample.service", runner).set(Y.STREAM_KEY, 'bad" -w "x')


@pytest.mark.new_install
def test_the_job_never_streams_without_an_owner_start(monkeypatch):
    station = Y.StaffStation(cfg(), service(cfg(), FakeGoogle(), Store(**{Y.STREAM_KEY: STREAM_KEY,
                                                                           Y.INGEST_URL: "rtmps://x"})))
    gave = []
    monkeypatch.setattr(station, "give_up", lambda end=True, why="failed": gave.append(end) or 0)
    monkeypatch.setattr(Y.radio, "Pipeline", lambda *a, **k: pytest.fail("no pipeline without an owner start"))
    assert station._run() == 0 and gave == [False]


def test_the_owner_radio_never_reaches_youtube():
    """The Telegram radio's start path does not know the YouTube module at all: going live there is only the
    owner's explicit `radio youtube start`."""
    import ast
    tree = ast.parse(open(radio.__file__, encoding="utf-8").read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names |= {node.module or ""} | {a.name for a in node.names}
        elif isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    assert not [n for n in names if "youtube" in n.lower()]


def test_staff_station_draws_only_the_staff_card(monkeypatch):
    _live_state()
    station = Y.StaffStation(cfg(), service(cfg(), FakeGoogle()), reload=lambda: Y.settings(cfg()))
    monkeypatch.setattr(station, "gather", lambda now: snap())
    drawn, said = [], []
    monkeypatch.setattr(Y.radio_card, "render", lambda folder, out, title, when, sections, foot, rtl: drawn.append(
        (title, sections, foot)) or True)
    monkeypatch.setattr(station, "speak", lambda text: said.append(text) or True)
    station.update()
    assert drawn and drawn[0][2] == "" and said
    blob = json.dumps([drawn, said], ensure_ascii=False)
    for word in SECRET_WORDS:
        assert word not in blob


def test_english_station_keeps_the_same_rules():
    p = radio_staff.profile({"projects": {"alpha": "Alpha"}, "say_titles": True}, "en")
    sn = snap()
    sn["done"][0]["text"] = "Added a new screen"
    script = radio_staff.build_script(radio_staff.staff_view(sn, p))
    assert script.startswith("Team radio. Hello") and "In Alpha, two tasks finished." in script
    assert "screen" not in script
    assert not any(c.isdigit() for c in script) and "PRIVATE" not in script



# --- review of #230: cleanup ids, live settings, the card on air, the end time, the key ----------------------

def test_a_new_start_never_loses_a_broadcast_awaiting_cleanup():
    Y._set_state(broadcast_id="bc-old", on=False)
    g = FakeGoogle(fail_ids={"bc-old"})
    assert Y.start(cfg(), service(cfg(), g), load_job=lambda s: None) == "youtube_started"
    st = Y._state()
    assert st["broadcast_id"] == "bc-1" and st["cleanup"] == ["bc-old"]
    g2 = FakeGoogle(phase="live")  # YouTube answers again: the old one is finished first on the next stop
    Y._set_state(on=False)
    assert Y.stop(cfg(), service(cfg(), g2)) == "youtube_stopped"
    assert g2.did("POST", "/liveBroadcasts/transition", "bc-old") and g2.did("POST", "/liveBroadcasts/transition", "bc-1")
    assert Y._state()["cleanup"] == [] and Y._state()["broadcast_id"] == ""


def test_start_finishes_pending_broadcasts_first():
    Y._set_state(cleanup=["bc-old"])
    g = FakeGoogle(phase="ready")
    Y.start(cfg(), service(cfg(), g), load_job=lambda s: None)
    old = [i for i, c in enumerate(g.calls) if c[2].get("id") == "bc-old" and c[0] == "DELETE"]
    new = [i for i, c in enumerate(g.calls) if c[:2] == ("POST", "/liveBroadcasts")]
    assert old and new and old[0] < new[0] and Y._state()["cleanup"] == []


def _live_state(**kw):
    Y._set_state(**dict(dict(on=True, broadcast_id="bc-1", owner_start="n", since=int(time.time()),
                             start_generation=Y.generation()), **kw))


def _station(monkeypatch, settings_box, **kw):
    station = Y.StaffStation(cfg(), service(cfg(), FakeGoogle(), Store(**{Y.STREAM_KEY: STREAM_KEY,
                                                                           Y.INGEST_URL: "rtmps://ingest.example/x"})),
                             reload=lambda: settings_box[0])
    drawn, said, gave = [], [], []
    monkeypatch.setattr(Y.radio_card, "render", lambda folder, out, title, when, sections, foot, rtl: (
        drawn.append(json.dumps([title, sections], ensure_ascii=False)), out.write_bytes(b"NEW"))[-1] and True)
    monkeypatch.setattr(Y.radio_card, "plain", lambda out: out.write_bytes(b"PLAIN"))
    monkeypatch.setattr(station, "speak", lambda text: said.append(text) or True)
    monkeypatch.setattr(station, "give_up", lambda end=True, why="failed": gave.append((end, why)) or 0)
    monkeypatch.setattr(station, "gather", kw.get("gather") or (lambda now: snap()))
    return station, drawn, said, gave


def test_turning_it_off_in_the_settings_ends_the_broadcast(monkeypatch):
    box = [Y.settings(cfg())]
    _live_state()
    station, drawn, said, gave = _station(monkeypatch, box)
    station.update()
    assert said and "אלפא" in drawn[-1]
    box[0] = Y.settings(cfg(enabled=False))
    n = len(drawn)
    station.update()
    assert len(drawn) == n and station.quit.is_set() and station.ending == "off"
    assert station._run() == 0 and gave == [(True, "ended")]


def test_a_project_taken_off_the_allowlist_is_gone_from_the_next_update(monkeypatch):
    box = [Y.settings(cfg())]
    _live_state()
    station, drawn, said, gave = _station(monkeypatch, box)
    station.update()
    assert "אלפא" in drawn[-1]
    box[0] = Y.settings(cfg(projects={}))
    station.update()
    assert "אלפא" not in drawn[-1]


def test_unreadable_settings_leave_only_the_station_id(monkeypatch):
    _live_state()
    station, drawn, said, gave = _station(monkeypatch, [None])
    station.reload = lambda: (_ for _ in ()).throw(ValueError("broken config"))
    station.update()
    assert "אלפא" not in drawn[-1] and not station.quit.is_set()


def test_an_old_card_is_replaced_before_any_connection(monkeypatch):
    _live_state()
    station, drawn, said, gave = _station(monkeypatch, [Y.settings(cfg())])
    station.card.write_bytes(b"OLD CARD WITH A REMOVED PROJECT")
    seen = []

    class Pipe:
        def __init__(self, url, key, card, *a, **k):
            seen.append(card.read_bytes())
            station.quit.set()

        def run(self):
            return ""
    monkeypatch.setattr(Y.radio, "Pipeline", Pipe)
    monkeypatch.setattr(Y.radio, "pick_encoder", lambda wanted: "libx264")
    monkeypatch.setattr(station, "content_loop", lambda: None)
    station._run()
    assert seen == [b"NEW"] and "אלפא" not in drawn[0] and "משימ" not in drawn[0]


def test_a_failed_gather_puts_the_id_card_on_air(monkeypatch):
    _live_state()

    def broken(now):
        raise OSError("disk")
    station, drawn, said, gave = _station(monkeypatch, [Y.settings(cfg())], gather=broken)
    station.card.write_bytes(b"OLD")
    station.update()
    assert station.card.read_bytes() == b"NEW" and "אלפא" not in drawn[-1] and not said


def test_a_failed_draw_never_leaves_the_old_card(monkeypatch):
    _live_state()
    station, drawn, said, gave = _station(monkeypatch, [Y.settings(cfg())])
    monkeypatch.setattr(Y.radio_card, "render", lambda *a, **k: False)
    station.card.write_bytes(b"OLD")
    station.update()
    assert station.card.read_bytes() == b"PLAIN"


def test_a_restart_never_extends_the_broadcast(monkeypatch):
    _live_state(since=int(time.time()) - 3 * 3600)
    station, drawn, said, gave = _station(monkeypatch, [Y.settings(cfg(max_hours=1))])
    monkeypatch.setattr(Y.radio, "Pipeline", lambda *a, **k: pytest.fail("past max_hours: no connection"))
    assert station._run() == 0 and station.ending == "ended" and gave == [(True, "ended")]


def test_the_stream_key_is_never_on_the_encoder_command_line(monkeypatch):
    """The key and the ingest URL stay in this process: ffmpeg only encodes to a pipe and radio_rtmp publishes.
    Neither reaches ffmpeg's argv (what `ps` shows) nor its environment."""
    seen = {}

    class Pub:
        def __init__(self, url, key, timeout=10):
            seen["pub"] = (url, key)

        def connect(self):
            pass

        def close(self):
            pass

    def popen(args, **kw):
        seen["argv"], seen["env"] = list(args), kw.get("env")
        raise OSError("stop here")
    monkeypatch.setattr("ctb.radio_rtmp.Publisher", Pub)
    monkeypatch.setattr(radio.subprocess, "Popen", popen)
    pipe = radio.Pipeline("rtmps://ingest.example/live2", STREAM_KEY, Y.folder() / "card.png", "libx264",
                          radio.Mixer(b"\0\0" * 960), lambda pid: None, err_path=Y.folder() / "ffmpeg.err",
                          marker=Y.MARKER)
    pipe.run()
    argv = " ".join(seen["argv"])
    assert seen["pub"] == ("rtmps://ingest.example/live2", STREAM_KEY)
    assert STREAM_KEY not in argv and "ingest.example" not in argv and "rtmp" not in argv
    assert seen["env"] is None or STREAM_KEY not in json.dumps(seen["env"])
    assert Y.MARKER in argv and STREAM_KEY not in Y.plist_bytes().decode()


def _card_writer(monkeypatch):
    """render() writes the card's text into the file, so a test can read what is on air."""
    def render(folder, out, title, when, sections, foot, rtl):
        out.write_text(json.dumps([title, sections], ensure_ascii=False), encoding="utf-8")
        return True
    monkeypatch.setattr(Y.radio_card, "render", render)


def test_a_reconnect_after_a_project_is_removed_shows_no_project(monkeypatch):
    """Review 230b: allowlisted project on the card, disconnect, project removed, reconnect: the first frames of
    the new connection carry no project name and no count."""
    box = [Y.settings(cfg())]
    _live_state()
    station, drawn, said, gave = _station(monkeypatch, box)
    _card_writer(monkeypatch)
    monkeypatch.setattr(station, "content_loop", lambda: None)
    monkeypatch.setattr(Y.radio, "pick_encoder", lambda wanted: "libx264")
    on_air = []

    class Pipe:
        runs = 0

        def __init__(self, url, key, card, *a, **k):
            on_air.append(card.read_text(encoding="utf-8"))  # what the connection starts with

        def run(self):
            Pipe.runs += 1
            if Pipe.runs == 1:
                station.update()  # the allowlisted project is on the card
                assert "אלפא" in station.card.read_text(encoding="utf-8")
                box[0] = Y.settings(cfg(projects={}))  # the owner removes it ...
                return "connection lost"  # ... while the connection drops
            station.quit.set()
            return ""
    monkeypatch.setattr(Y.radio, "Pipeline", Pipe)
    monkeypatch.setattr(station.quit, "wait", lambda t=None: station.quit.is_set())  # no real backoff sleep
    station._run()
    assert len(on_air) == 2
    for card in on_air:
        assert "אלפא" not in card and "משימ" not in card


def test_an_update_begun_before_a_reconnect_never_draws(monkeypatch):
    """An update that read the settings before a connection was opened is stale: its card is not drawn."""
    _live_state()
    station, drawn, said, gave = _station(monkeypatch, [Y.settings(cfg())])
    _card_writer(monkeypatch)

    def gather(now):
        with station.draw_lock:  # what connect() does meanwhile
            station.generation += 1
        return snap()
    monkeypatch.setattr(station, "gather", gather)
    station.card.write_text("ID", encoding="utf-8")
    station.update()
    assert station.card.read_text(encoding="utf-8") == "ID" and not said


def test_only_connect_opens_a_connection():
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(Y))
    callers = set()
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "Pipeline":
                    callers.add(fn.name)  # a Pipeline is built (= a connection opened) only here
    assert callers == {"connect"}


# --- the bridge-off switch (policy.on, PR #227): read again each time, from the config file ---------------------

def _bridge(on: bool):
    from ctb import config as C
    C.config_path().parent.mkdir(parents=True, exist_ok=True)
    C.config_path().write_text(json.dumps({"bot_token": GOOD_TOKEN, "owner_chat_id": OWNER, "mirror": on}))


def test_off_before_start_creates_nothing(real_policy):
    from types import SimpleNamespace
    from ctb import cli
    _bridge(False)
    g = FakeGoogle()
    with pytest.raises(Y.YouTubeError) as e:
        Y.start(cfg(), service(cfg(), g), load_job=lambda s: pytest.fail("no job while off"))
    assert e.value.key == "mirror_off" and g.calls == [] and not Y._state().get("broadcast_id")
    assert cli._off(SimpleNamespace(cmd="radio", radio_cmd="youtube", youtube_cmd="start")) == 1
    for action in ("status", "stop", "disconnect"):
        assert cli._off(SimpleNamespace(cmd="radio", radio_cmd="youtube", youtube_cmd=action)) is None


def test_no_new_sign_in_while_explicitly_off(real_policy):
    _bridge(False)
    g = FakeGoogle()
    with pytest.raises(Y.YouTubeError) as e:
        Y.connect(cfg(), service(cfg(), g, Store()), show=lambda url: pytest.fail("no consent link while off"),
                  read_line=lambda: "", timeout=0.1)
    assert e.value.key == "mirror_off" and g.calls == []


def test_the_cli_prints_the_shared_off_notice(real_policy, monkeypatch, capsys):
    from ctb import policy
    _bridge(False)
    monkeypatch.setattr(Y, "start", lambda c, **k: (_ for _ in ()).throw(Y.YouTubeError("mirror_off")))
    assert Y.cli(cfg(), "start") == 1 and policy.OFF_NOTICE in capsys.readouterr().err


def test_off_mid_stream_stops_and_ends_the_broadcast(real_policy, monkeypatch):
    """Switched off while live: the pipeline stops within its one-second check, the broadcast is ended through
    the cleanup path, the job unloads itself; nothing is left streaming."""
    _bridge(True)
    _live_state()
    g = FakeGoogle(phase="live")
    station = Y.StaffStation(cfg(), service(cfg(), g, Store(**{Y.REFRESH: REFRESH_TOKEN, Y.STREAM_KEY: STREAM_KEY,
                                                               Y.INGEST_URL: "rtmps://ingest.example/x"})),
                             reload=lambda: Y.settings(cfg()))
    _card_writer(monkeypatch)
    monkeypatch.setattr(station, "content_loop", lambda: None)
    monkeypatch.setattr(Y.radio, "pick_encoder", lambda wanted: "libx264")
    unloaded, pipes = [], []
    monkeypatch.setattr(Y.subprocess, "Popen", lambda argv, **k: unloaded.append(argv[:2]))

    class Pipe:
        def __init__(self, *a, **k):
            pipes.append(1)

        def run(self):
            _bridge(False)  # the owner turns the bridge off while the stream runs
            return Y.radio.MIRROR_OFF  # what radio.Pipeline returns within a second of the switch
    monkeypatch.setattr(Y.radio, "Pipeline", Pipe)
    assert station._run() == 0
    assert pipes == [1] and station.ending == "bridge_off"
    assert g.did("POST", "/liveBroadcasts/transition", "bc-1")  # ended at YouTube
    assert unloaded == [["launchctl", "bootout"]]
    st = Y._state()
    assert not st["broadcast_id"] and not st["owner_start"] and st["cleanup"] == []


def test_a_round_while_off_ends_the_job_without_speaking(real_policy, monkeypatch):
    _bridge(True)
    _live_state()
    station, drawn, said, gave = _station(monkeypatch, [Y.settings(cfg())])
    _bridge(False)
    station.update()
    assert station.quit.is_set() and station.ending == "bridge_off" and not said and not drawn


def test_on_again_does_not_resume_a_stream_the_owner_did_not_start(real_policy, monkeypatch):
    _bridge(True)
    Y._set_state(on=False, broadcast_id="", owner_start="")  # what the off path above leaves behind
    station, drawn, said, gave = _station(monkeypatch, [Y.settings(cfg())])
    monkeypatch.setattr(Y.radio, "Pipeline", lambda *a, **k: pytest.fail("no stream without a new owner start"))
    assert station._run() == 0 and station.ending == "owner"


def test_speech_made_while_off_is_dropped(real_policy, monkeypatch):
    _bridge(False)
    station = Y.StaffStation(cfg(), service(cfg(), FakeGoogle()), reload=lambda: Y.settings(cfg()))
    monkeypatch.setattr(Y.radio_voice, "system_voice", lambda text, voice: b"\0\0" * 4800)
    assert station.speak("שלום") is False and station.mixer.pending() == 0


def test_stop_and_disconnect_still_work_while_off(real_policy, monkeypatch):
    monkeypatch.setattr(Y, "_unload_job", lambda: None)
    _bridge(False)
    _live_state()
    g = FakeGoogle(phase="live")
    store = Store(**{Y.REFRESH: REFRESH_TOKEN, Y.STREAM_KEY: STREAM_KEY, Y.INGEST_URL: "rtmps://x"})
    assert Y.stop(cfg(), service(cfg(), g, store)) == "youtube_stopped"
    assert g.did("POST", "/liveBroadcasts/transition", "bc-1")
    assert Y.status(cfg(), service=service(cfg(), None, store))["on"] is False
    assert Y.disconnect(cfg(), service(cfg(), g, store)) == "youtube_disconnected" and store.items == {}


# --- review of #231: Start checks the confirmed channel again, under the connection lock -----------------------

def _connection(gen_bumps=0, channel_id="UCsample"):
    with Y.connection_lock():
        Y._set_connection(connected=True, channel_id=channel_id, channel_title="Sample channel", live_enabled=True)
    for _ in range(gen_bumps):
        Y.bump_generation()
    return Y.generation()


def test_start_refuses_when_the_channel_changed_after_the_confirmation():
    confirmed = {"generation": _connection(), "channel_id": "UCsample"}
    Y.bump_generation()  # a sign-in or a disconnect after the second tap
    g = FakeGoogle()
    with pytest.raises(Y.YouTubeError) as e:
        Y.start(cfg(), service(cfg(), g), load_job=lambda s: pytest.fail("no job"), expect=confirmed)
    assert e.value.key == "youtube_channel_changed" and not g.did("POST", "/liveBroadcasts")
    with pytest.raises(Y.YouTubeError):
        Y.start(cfg(), service(cfg(), g), load_job=lambda s: None,
                expect={"generation": Y.generation(), "channel_id": "UCother"})
    assert not g.did("POST", "/liveBroadcasts")


def test_start_holds_the_lock_until_the_broadcast_is_bound():
    """No sign-in can finish (and switch the channel) between the last check and the broadcast's creation."""
    confirmed = {"generation": _connection(), "channel_id": "UCsample"}
    seen = []

    class G(FakeGoogle):
        def __call__(self, method, url, **kw):
            if method == "POST" and url.endswith("/liveBroadcasts?part=snippet%2Cstatus%2CcontentDetails"):
                try:
                    with Y.connection_lock(wait=0.1):
                        seen.append("free")
                except Y.YouTubeError as e:
                    seen.append(e.key)
            return super().__call__(method, url, **kw)
    assert Y.start(cfg(), service(cfg(), G()), load_job=lambda s: None, expect=confirmed) == "youtube_started"
    assert seen == ["youtube_busy"]


def test_sign_in_and_disconnect_each_start_a_new_generation():
    g, store = FakeGoogle(), Store()
    before = Y.generation()
    gen = Y.store_credentials(service(cfg(), g, store), {"access_token": "a", "refresh_token": REFRESH_TOKEN},
                              Y.generation(), SAMPLE_CLIENT)
    assert gen == before + 1 and store.items[Y.REFRESH] == REFRESH_TOKEN
    Y.disconnect(cfg(), service(cfg(), g, store))
    assert Y.generation() == gen + 1 and Y.REFRESH not in store.items
    with pytest.raises(Y.YouTubeError) as e:  # a sign-in begun under the old generation
        Y.store_credentials(service(cfg(), g, store), {"access_token": "a", "refresh_token": REFRESH_TOKEN}, gen, SAMPLE_CLIENT)
    assert e.value.key == "youtube_connect_stale" and Y.REFRESH not in store.items



# --- review of #231, round 2: the account that publishes is checked at the last moment -----------------------

TOKENS = {"access_token": "a", "refresh_token": REFRESH_TOKEN}


def _revoked(g):
    return [c for c in g.calls if c[1] == "/revoke" or c[1].endswith("/revoke")]


def test_a_cli_sign_in_begun_before_a_disconnect_is_dropped():
    """Reviewer's repro 2: connect starts, a disconnect lands (another process), the old sign-in completes."""
    g, store, shown = FakeGoogle(), Store(), []

    def read_line():
        while not shown:
            time.sleep(0.01)
        Y.disconnect(cfg(), service(cfg(), FakeGoogle(), Store()))  # the disconnect, while the browser is open
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(shown[0]).query))
        return q["redirect_uri"] + "?" + urllib.parse.urlencode({"state": q["state"], "code": "sample-code"})
    with pytest.raises(Y.YouTubeError) as e:
        Y.connect(cfg(), service(cfg(), g, store), show=shown.append, read_line=read_line, timeout=5)
    assert e.value.key == "youtube_connect_stale" and Y.REFRESH not in store.items
    assert not Y.connection()["connected"]


def test_store_credentials_always_needs_the_expected_generation():
    import inspect
    sig = inspect.signature(Y.store_credentials)
    assert sig.parameters["expect"].default is inspect.Parameter.empty
    assert sig.parameters["client"].default is inspect.Parameter.empty
    with pytest.raises(Y.YouTubeError):
        Y.store_credentials(service(cfg(), FakeGoogle(), Store()), TOKENS, None, SAMPLE_CLIENT)


def test_publishing_checks_the_real_credential_with_google():
    """Reviewer's repro 3: the record and a confirmation still say channel A while the Keychain credential is B's.
    Google is asked with the credential that would publish: nothing is created."""
    g = FakeGoogle(channel="UCchannelB")
    confirmed = {"generation": Y.generation(), "channel_id": "UCsample"}
    for expect in (confirmed, None):  # a Cockpit start and the owner's CLI start
        with pytest.raises(Y.YouTubeError) as e:
            Y.start(cfg(), service(cfg(), g), load_job=lambda s: pytest.fail("no job"), expect=expect)
        assert e.value.key == "youtube_channel_changed"
    assert not g.did("POST", "/liveBroadcasts") and not g.did("POST", "/liveStreams")


class CrashingStore(Store):
    def set(self, name, value):
        raise OSError("crash while writing the Keychain")


@pytest.mark.parametrize("crash", ["after-step-1", "after-step-2", "after-step-3"])
def test_every_crash_point_of_a_sign_in_leaves_no_usable_confirmation(monkeypatch, crash):
    """A confirmation issued for channel A, then a sign-in to channel B crashes part-way: at every point Start
    refuses (the generation moved, the record says not connected, or Google names another channel)."""
    old = Y.generation()
    confirmed = {"generation": old, "channel_id": "UCsample"}
    store = Store(**{Y.REFRESH: "1//channel-a-token"})
    if crash == "after-step-1":
        with pytest.raises(OSError):
            Y.store_credentials(service(cfg(), FakeGoogle(), CrashingStore()), TOKENS, old, SAMPLE_CLIENT)
    elif crash == "after-step-2":
        real, calls = Y._set_connection, []

        def second_write_crashes(**kw):
            calls.append(kw)
            if len(calls) == 2:
                raise OSError("crash before the connected record")
            return real(**kw)
        monkeypatch.setattr(Y, "_set_connection", second_write_crashes)
        with pytest.raises(OSError):
            Y.store_credentials(service(cfg(), FakeGoogle(), store), TOKENS, old, SAMPLE_CLIENT)
        monkeypatch.setattr(Y, "_set_connection", real)
        assert store.items[Y.REFRESH] == REFRESH_TOKEN  # the Keychain already holds B's credential
    else:
        Y.store_credentials(service(cfg(), FakeGoogle(), store), TOKENS, old, SAMPLE_CLIENT)  # done; describe never ran
    assert Y.generation() == old + 1
    g = FakeGoogle(channel="UCchannelB")
    for expect in (confirmed, None):
        with pytest.raises(Y.YouTubeError) as e:
            Y.start(cfg(), service(cfg(), g, store), load_job=lambda s: pytest.fail("no job"), expect=expect)
        assert e.value.key in ("youtube_channel_changed", "youtube_not_connected")
    assert not g.did("POST", "/liveBroadcasts")


def test_an_old_and_a_new_record_that_disagree_mean_not_connected():
    """Reviewer's downgrade/upgrade case: state.json (older version) and connection.json disagree."""
    Y._set_state(connected=True, channel_id="UCchannelB", channel_title="Other")
    conn = Y.connection()
    assert conn["connected"] is False and conn["conflict"] is True
    with pytest.raises(Y.YouTubeError) as e:
        Y.start(cfg(), service(cfg(), FakeGoogle()), load_job=lambda s: None)
    assert e.value.key == "youtube_not_connected"
    gen = Y.store_credentials(service(cfg(), FakeGoogle(), Store()), TOKENS, Y.generation(), SAMPLE_CLIENT)  # a new sign-in
    assert "channel_id" not in Y._state() and Y.connection()["generation"] == gen and Y.connection()["connected"]


def test_no_sign_in_is_stored_while_live():
    """Reviewer's repro 4: a sign-in finishing while a broadcast runs is refused (stop first) and revoked."""
    _live_state()
    g, store = FakeGoogle(), Store(**{Y.REFRESH: "1//channel-a-token"})
    with pytest.raises(Y.YouTubeError) as e:
        Y.store_credentials(service(cfg(), g, store), TOKENS, Y.generation(), SAMPLE_CLIENT)
    assert e.value.key == "youtube_stop_first" and store.items[Y.REFRESH] == "1//channel-a-token"
    assert _revoked(g)


def test_the_job_ends_its_broadcast_when_the_connection_changes(monkeypatch):
    box = [Y.settings(cfg())]
    _live_state()
    station, drawn, said, gave = _station(monkeypatch, box)
    Y.bump_generation()
    station.update()
    assert station.quit.is_set() and station.ending == "connection_changed" and not said


def test_disconnect_while_live_ends_the_broadcast_first(monkeypatch):
    monkeypatch.setattr(Y, "_unload_job", lambda: None)
    _live_state()
    g = FakeGoogle(phase="live")
    store = Store(**{Y.REFRESH: REFRESH_TOKEN, Y.STREAM_KEY: STREAM_KEY, Y.INGEST_URL: "rtmps://x"})
    assert Y.disconnect(cfg(), service(cfg(), g, store)) == "youtube_disconnected"
    transition = g.did("POST", "/liveBroadcasts/transition", "bc-1")
    revoke = [i for i, c in enumerate(g.calls) if c[1].endswith("/revoke")]
    assert transition and revoke and g.calls.index(transition[0]) < revoke[0]  # ended with the original credential
    assert store.items == {} and not Y._state().get("owner_start")


def test_a_start_request_is_consumed_once_even_by_concurrent_starts():
    """Reviewer's repro 5: two starts share one request: exactly one gets it, the other is refused."""
    rid = Y.put_start_request(Y.generation(), "UCsample")
    assert oct((Y.folder() / Y.START_REQUESTS / f"{rid}.json").stat().st_mode & 0o777) == "0o600"
    got, refused = [], []

    def take():
        try:
            got.append(Y.take_start_request(rid))
        except Y.YouTubeError as e:
            refused.append(e.key)
    threads = [threading.Thread(target=take) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert len(got) == 1 and refused == ["youtube_confirm_missing"] * 3


def test_a_cockpit_start_without_its_request_creates_nothing():
    g = FakeGoogle()
    for rid in ("0" * 31 + "1", "not-an-id"):
        with pytest.raises(Y.YouTubeError) as e:
            Y.switch(cfg(), "start", rid)
        assert e.value.key == "youtube_confirm_missing"
    assert Y.operation()["result"] == "youtube_confirm_missing" and g.calls == []


def test_an_expired_request_is_refused_and_cleaned_up():
    rid = Y.put_start_request(Y.generation(), "UCsample")
    path = Y.folder() / Y.START_REQUESTS / f"{rid}.json"
    data = json.loads(path.read_text())
    data["at"] -= Y.START_REQUEST_TTL + 5
    path.write_text(json.dumps(data))
    with pytest.raises(Y.YouTubeError):
        Y.take_start_request(rid)
    old = Y.put_start_request(Y.generation(), "UCsample")
    stale = Y.folder() / Y.START_REQUESTS / f"{old}.json"
    os.utime(stale, (time.time() - Y.START_REQUEST_TTL - 5,) * 2)
    Y.put_start_request(Y.generation(), "UCsample")
    assert not stale.exists()


def test_the_cockpit_start_carries_its_request_id(monkeypatch):
    seen = []
    monkeypatch.setattr(Y.subprocess, "Popen", lambda argv, **k: seen.append(argv))
    rid = Y.put_start_request(Y.generation(), "UCsample")
    Y.spawn("start", rid)
    Y.spawn("start", "bogus")  # never launched with a malformed id
    assert len(seen) == 1 and seen[0][-3:] == ["start", "--request", rid]


def test_a_channel_connected_in_the_cockpit_uses_the_cockpit_sign_in_client():
    """Regression: after "Connect with YouTube" in the Cockpit, Start failed with youtube_no_client because the
    service only looked at radio.youtube.client_command and never at the Web client in the Keychain."""
    c = cfg()
    assert not Y.settings(c)["client_command"]
    store = Store(**{Y.WEB_CLIENT_ID: "sample-web-id", Y.WEB_CLIENT_SECRET: "sample-web-value"})
    svc = Y.Service(Y.settings(c), store=store, request=lambda *a, **k: (200, {}))
    assert svc.client() == {"client_id": "sample-web-id", "client_secret": "sample-web-value"}


def test_without_any_sign_in_client_start_still_says_so():
    svc = Y.Service(Y.settings(cfg()), store=Store(), request=lambda *a, **k: (200, {}))
    with pytest.raises(Y.YouTubeError) as e:
        svc.client()
    assert str(e.value) == "youtube_no_client"


# --- which client a refresh token belongs to (the issuing client is recorded beside the token) -----------------

DESKTOP = {"client_id": "desktop-sample-id", "client_secret": "desktop-sample-value"}
WEB = {"client_id": "web-sample-id", "client_secret": "web-sample-value"}


def _cmd_cfg(client=DESKTOP):
    """A config whose client_command prints a sample Desktop client file."""
    return cfg(client_command=["/bin/echo", json.dumps({"installed": client})])


def _web_store(**items):
    return Store(**dict({Y.WEB_CLIENT_ID: WEB["client_id"], Y.WEB_CLIENT_SECRET: WEB["client_secret"]}, **items))


class FormGoogle(FakeGoogle):
    """Also keeps every form sent to the token and revoke endpoints."""

    def __init__(self, revoke_status=200, **kw):
        super().__init__(**kw)
        self.forms, self.revoke_status = [], revoke_status

    def __call__(self, method, url, body=None, form=None, token="", timeout=20.0):
        if url in (Y.TOKEN_URL, Y.REVOKE_URL):
            self.forms.append((url, dict(form or {})))
            if url == Y.REVOKE_URL:
                return self.revoke_status, {}
        return super().__call__(method, url, body=body, form=form, token=token, timeout=timeout)

    def refresh_clients(self):
        return [f.get("client_id") for u, f in self.forms if u == Y.TOKEN_URL and f.get("grant_type") == "refresh_token"]


def test_a_cockpit_sign_in_refreshes_with_the_web_client_even_when_a_command_is_set():
    """The mixed case: client_command is configured, but the channel was connected in the Cockpit (Web client).
    A later Start / status / stop builds a fresh service: the refresh must use the client that issued the token."""
    c, g, store = _cmd_cfg(), FormGoogle(), _web_store()
    signin = Y.Service(Y.settings(c), store=store, request=g, client=WEB)
    Y.store_credentials(signin, {"access_token": "a", "refresh_token": REFRESH_TOKEN}, Y.generation(), WEB)
    later = Y.Service(Y.settings(c), store=store, request=g)
    later.access_token()
    assert g.refresh_clients() == [WEB["client_id"]]


def test_a_cli_sign_in_refreshes_with_the_command_client_even_when_a_web_client_exists():
    c, g, store = _cmd_cfg(), FormGoogle(), _web_store()
    signin = Y.Service(Y.settings(c), store=store, request=g)
    Y.store_credentials(signin, {"access_token": "a", "refresh_token": REFRESH_TOKEN}, Y.generation(), DESKTOP)
    Y.Service(Y.settings(c), store=store, request=g).access_token()
    assert g.refresh_clients() == [DESKTOP["client_id"]]


def test_a_recorded_client_that_is_gone_asks_to_reconnect_and_never_guesses():
    g = FormGoogle()
    store = _web_store(**{Y.REFRESH: REFRESH_TOKEN, Y.REFRESH_CLIENT: "removed-client-id"})
    with pytest.raises(Y.YouTubeError) as e:
        Y.Service(Y.settings(_cmd_cfg()), store=store, request=g).access_token()
    assert e.value.key == "youtube_reconnect_client" and g.forms == []


def test_the_owners_cockpit_token_without_a_record_keeps_refreshing_with_the_web_client():
    """The owner's install today: connected in the Cockpit before records existed, no client_command, on air.
    It keeps refreshing with the Web client, read-only: no record is written (only a real sign-in writes one)."""
    g, store = FormGoogle(), _web_store(**{Y.REFRESH: REFRESH_TOKEN})
    assert not Y.settings(cfg())["client_command"]
    Y.Service(Y.settings(cfg()), store=store, request=g).access_token()
    assert g.refresh_clients() == [WEB["client_id"]] and Y.REFRESH_CLIENT not in store.items


@pytest.mark.parametrize("command", [None, "works", "fails"])
def test_every_other_token_without_a_record_asks_to_reconnect(command):
    """No record and a client_command configured (working or failing), or no client at all: reconnect. A failing
    source never turns the remaining one into "the only client"."""
    c = {None: cfg(), "works": _cmd_cfg(), "fails": cfg(client_command=["/usr/bin/false"])}[command]
    store = (Store if command is None else _web_store)(**{Y.REFRESH: REFRESH_TOKEN})
    g = FormGoogle()
    with pytest.raises(Y.YouTubeError) as e:
        Y.Service(Y.settings(c), store=store, request=g).access_token()
    assert e.value.key == "youtube_reconnect_client" and g.forms == [] and Y.REFRESH_CLIENT not in store.items


def test_a_record_only_the_failing_command_could_provide_asks_to_reconnect():
    c = cfg(client_command=["/usr/bin/false"])
    g, store = FormGoogle(), _web_store(**{Y.REFRESH: REFRESH_TOKEN, Y.REFRESH_CLIENT: DESKTOP["client_id"]})
    with pytest.raises(Y.YouTubeError) as e:
        Y.Service(Y.settings(c), store=store, request=g).access_token()
    assert e.value.key == "youtube_reconnect_client" and g.forms == []


def test_store_credentials_runs_no_command_and_records_the_exchanging_client(monkeypatch):
    """The caller hands over the client that did the code exchange; nothing is loaded or run under the lock."""
    ran = []
    monkeypatch.setattr(Y, "load_client", lambda command: ran.append(command) or DESKTOP)
    c, store = _cmd_cfg(), Store()
    svc = Y.Service(Y.settings(c), store=store, request=FormGoogle())
    Y.store_credentials(svc, {"access_token": "a", "refresh_token": REFRESH_TOKEN}, Y.generation(), WEB)
    assert ran == [] and store.items[Y.REFRESH_CLIENT] == WEB["client_id"] and svc.client() == WEB


def test_a_job_on_with_an_empty_state_file_counts_as_live(monkeypatch):
    """launchd says the job runs although the state file is empty: still live, so no sign-in is stored."""
    monkeypatch.setattr(Y, "_job_on", lambda: True)
    (Y.folder() / "state.json").unlink(missing_ok=True)
    assert Y._live()
    store, g = Store(), FormGoogle()
    with pytest.raises(Y.YouTubeError) as e:
        Y.store_credentials(Y.Service(Y.settings(cfg()), store=store, request=g),
                            {"access_token": "a", "refresh_token": REFRESH_TOKEN}, Y.generation(), WEB)
    assert e.value.key == "youtube_stop_first" and store.items == {}


def test_a_broken_client_command_does_not_block_the_recorded_web_client():
    c = cfg(client_command=["/usr/bin/false"])
    g, store = FormGoogle(), _web_store(**{Y.REFRESH: REFRESH_TOKEN, Y.REFRESH_CLIENT: WEB["client_id"]})
    Y.Service(Y.settings(c), store=store, request=g).access_token()
    assert g.refresh_clients() == [WEB["client_id"]]


def test_the_record_is_written_with_the_token_in_the_crash_safe_order(monkeypatch):
    """Stored after the generation is marked not connected and before it is marked connected: a crash between
    the token and its record leaves the connection not connected (Start refuses)."""
    store, order = Store(), []
    real_set = store.set

    def recording_set(name, value):
        order.append(name)
        if name == Y.REFRESH_CLIENT:
            raise OSError("crash before the record")
        real_set(name, value)
    monkeypatch.setattr(store, "set", recording_set)
    with pytest.raises(OSError):
        Y.store_credentials(Y.Service(Y.settings(cfg()), store=store, request=FormGoogle(), client=WEB),
                            {"access_token": "a", "refresh_token": REFRESH_TOKEN}, Y.generation(), WEB)
    assert order == [Y.REFRESH, Y.REFRESH_CLIENT] and Y.connection()["connected"] is False


def test_disconnect_revokes_without_loading_any_client():
    """Google's revoke takes only the token: a broken or missing client never skips it."""
    c = cfg(client_command=["/usr/bin/false"])
    g = FormGoogle()
    store = Store(**{Y.REFRESH: REFRESH_TOKEN, Y.REFRESH_CLIENT: "desktop-sample-id"})
    assert Y.disconnect(c, Y.Service(Y.settings(c), store=store, request=g)) == "youtube_disconnected"
    revokes = [f for u, f in g.forms if u == Y.REVOKE_URL]
    assert revokes == [{"token": REFRESH_TOKEN}] and store.items == {}


def test_a_failed_revoke_is_reported_and_the_local_disconnect_still_happens():
    g = FormGoogle(revoke_status=400)
    store = _web_store(**{Y.REFRESH: REFRESH_TOKEN, Y.REFRESH_CLIENT: WEB["client_id"]})
    assert Y.disconnect(cfg(), Y.Service(Y.settings(cfg()), store=store, request=g)) == "youtube_disconnected_revoke_failed"
    assert Y.REFRESH not in store.items and Y.REFRESH_CLIENT not in store.items and not Y.connection()["connected"]


def test_a_refused_sign_in_revokes_without_a_client():
    _live_state()
    g = FormGoogle()
    svc = Y.Service(Y.settings(cfg(client_command=["/usr/bin/false"])), store=Store(), request=g)
    with pytest.raises(Y.YouTubeError):
        Y.store_credentials(svc, {"access_token": "a", "refresh_token": REFRESH_TOKEN}, Y.generation(), WEB)
    assert [f for u, f in g.forms if u == Y.REVOKE_URL] == [{"token": REFRESH_TOKEN}]


def test_start_never_runs_the_client_command_while_holding_the_lock(monkeypatch):
    """A Start whose token was issued to the command's client resolves it before taking the connection lock."""
    under_lock = []

    def load(command):
        try:
            with Y.connection_lock(wait=0.05):
                under_lock.append(False)
        except Y.YouTubeError:
            under_lock.append(True)
        return DESKTOP
    monkeypatch.setattr(Y, "load_client", load)
    c = _cmd_cfg()
    store = Store(**{Y.REFRESH: REFRESH_TOKEN, Y.REFRESH_CLIENT: DESKTOP["client_id"]})
    svc = Y.Service(Y.settings(c), store=store, request=FakeGoogle())
    assert Y.start(c, svc, load_job=lambda s: None) == "youtube_started"
    assert under_lock == [False]
