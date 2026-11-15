"""Card visibility: every card is private, team or public; outward paths take only public ones.

The outward paths are the radio's YouTube staff station (radio_board.card_projects -> StaffStation.staff_cards ->
radio_staff renderers), the YouTube mirror's board (radio_board.gather(public_only=True)) and the home room's
spoken digest while a mirror may re-broadcast the home audio. A card that is private (the default), team, or has a
missing or unknown visibility never reaches any of them. All data is sample data."""
import json
import time
from types import SimpleNamespace

import pytest

from conftest import GOOD_TOKEN
from ctb import cards, cli, radio, radio_board as B, radio_staff as S, radio_youtube as Y
from ctb.config import Config, ProjectConfig, state_dir
from test_radio_staff_progress import blob, lists, snap  # noqa: F401  (fixture and helpers)

NOW = time.mktime((2026, 3, 4, 15, 30, 0, 0, 0, -1))
GROUP, HOME = -1001, -1009
PRIVATE_WORDS = ["משכנתהפרטית", "PrivateDoctorVisit", "TeamOnlyMilestone", "HandEditedSecret", "NoFieldSecret"]


def publish(title, visibility=None, session="s-private", chat=GROUP, steps=None, **kw):
    return cards.publish(session, chat, 7, "progress", title, steps=steps or ["א", "ב", "ג"], at=1, now=NOW,
                         visibility=visibility, **kw)


# --- the field itself -------------------------------------------------------------------------------------------

def test_a_new_card_is_private_unless_it_says_otherwise():
    assert publish("one")["visibility"] == "private"
    assert publish("two", "team", session="s2")["visibility"] == "team"
    assert publish("three", "public", session="s3")["visibility"] == "public"
    stored = json.loads((state_dir() / cards.STATE_FILE).read_text())
    assert sorted(r["visibility"] for r in stored.values()) == ["private", "public", "team"]


@pytest.mark.parametrize("bad", ["PUBLIC", "public ", "PUBLIC!", "everyone", "", " ", 1, True, ["public"]])
def test_an_unknown_visibility_is_refused_and_nothing_is_written(bad):
    with pytest.raises(cards.CardError):
        publish("x", bad)
    assert cards.open_cards() == []


def test_an_update_keeps_the_visibility_unless_it_restates_it():
    rec = publish("one", "public")
    again = cards.publish("s-private", GROUP, 7, "progress", "", percent=50, ident=rec["id"], now=NOW + 1)
    assert again["visibility"] == "public"
    back = cards.publish("s-private", GROUP, 7, "progress", "", percent=60, ident=rec["id"], now=NOW + 2,
                         visibility="private")
    assert back["visibility"] == "private"


@pytest.mark.parametrize("value", [None, "", "Public", "public ", "world", 1, True, {"v": "public"}])
def test_a_hand_edited_missing_or_unknown_value_reads_private(value):
    rec = publish("one", "public")
    path = state_dir() / cards.STATE_FILE
    data = json.loads(path.read_text())
    if value is None:
        data[rec["id"]].pop("visibility")
    else:
        data[rec["id"]]["visibility"] = value
    path.write_text(json.dumps(data))
    (loaded,) = cards.open_cards()
    assert loaded["visibility"] == "private" and not cards.is_public(loaded)


@pytest.mark.parametrize("rec", [None, {}, {"visibility": "team"}, {"visibility": "private"}, {"visibility": "Public"},
                                 {"visibility": True}, "public", ["public"]])
def test_is_public_is_exact(rec):
    assert cards.is_public(rec) is False
    assert cards.visibility_of(rec) in ("private", "team")


def test_the_automatic_card_is_private():
    rec = cards.sync_auto("s-auto", GROUP, 7, "Topic", [{"content": "x", "status": "in_progress"}], now=NOW)
    assert rec is not None and not cards.is_public(rec)
    assert all(not cards.is_public(r) for r in cards.open_cards())


def test_the_cli_takes_only_the_three_values():
    p = cli._parser()
    assert p.parse_args(["card", "progress", "t", "--percent", "5"]).visibility is None
    assert p.parse_args(["card", "numbers", "t", "--figure", "a=1", "--visibility", "public"]).visibility == "public"
    assert p.parse_args(["card", "ask", "t", "--visibility", "team"]).visibility == "team"
    with pytest.raises(SystemExit):
        p.parse_args(["card", "progress", "t", "--percent", "5", "--visibility", "everyone"])


# --- the leak audit: outward paths ------------------------------------------------------------------------------

def _staff_cfg(lists):  # noqa: F811
    c = Config(bot_token=GOOD_TOKEN, owner_chat_id=1, target_language="he", chat_id=HOME,
               radio={"youtube": dict({"enabled": True, "projects": {"alpha": "אלפא"}}, **lists)})
    c.projects = [ProjectConfig(name="alpha", chat_id=GROUP)]
    return c


def _every_kind_of_private_card():
    """Private (default and explicit), team, and two cards whose visibility was edited by hand: all in alpha's own
    group chat, the one place a card can become a project's at all."""
    def steps(word):
        return [word[:10] + "1", word[:10] + "2", word[:10] + "3"]
    publish(PRIVATE_WORDS[0], session="s-a", steps=steps(PRIVATE_WORDS[0]))
    publish(PRIVATE_WORDS[1], "private", session="s-b", steps=steps(PRIVATE_WORDS[1]))
    publish(PRIVATE_WORDS[2], "team", session="s-c", steps=steps(PRIVATE_WORDS[2]))
    odd = publish(PRIVATE_WORDS[3], "public", session="s-d", steps=steps(PRIVATE_WORDS[3]))
    gone = publish(PRIVATE_WORDS[4], "public", session="s-e", steps=steps(PRIVATE_WORDS[4]))
    path = state_dir() / cards.STATE_FILE
    data = json.loads(path.read_text())
    data[odd["id"]]["visibility"] = "PUBLIC"
    data[gone["id"]].pop("visibility")
    path.write_text(json.dumps(data))


def test_private_cards_never_reach_the_staff_station_or_its_renderers(monkeypatch, lists):  # noqa: F811
    c = _staff_cfg(lists)
    monkeypatch.setattr(Y, "cockpit_prefs", lambda: {})
    st = Y.StaffStation(c, service=None, reload=lambda: Y.settings(c))
    _every_kind_of_private_card()
    assert len(cards.open_cards()) == 5
    assert B.card_projects(c) == []
    assert st.staff_cards() == []
    out = blob(S.staff_view(snap(*st.staff_cards()), st.s))
    for word in PRIVATE_WORDS:
        assert word not in out, word
    # control: one public card in the same chat does reach it, as numbers only
    publish("PublicLaunch", "public", session="s-pub")
    kept = st.staff_cards()
    assert [(k["project"], k["total"], k["at"]) for k in kept] == [("alpha", 3, 1)]
    out = blob(S.staff_view(snap(*kept), st.s))
    for word in PRIVATE_WORDS + ["PublicLaunch"]:
        assert word not in out, word


def test_private_cards_never_reach_the_mirror_board(monkeypatch, lists):  # noqa: F811
    c = _staff_cfg(lists)
    _every_kind_of_private_card()
    publish("PublicLaunch", "public", session="s-pub")
    base = radio.settings(c)
    public = B.gather(c, base, NOW, public_only=True)
    assert [card["title"] for _, card in public["cards"]] == ["PublicLaunch"]
    every = B.gather(c, base, NOW)  # the owner's own home radio still sees every card
    assert len(every["cards"]) == 6
    seen = []
    st = SimpleNamespace(cfg=c, show=SimpleNamespace(update=lambda projects: seen.append(projects)))
    Y.MirrorStation.feed_board(st)
    assert len(seen) == 1
    for projects in seen:
        text = json.dumps(projects, ensure_ascii=False)
        for word in PRIVATE_WORDS:
            assert word not in text, word


@pytest.mark.parametrize("mirror", [True, False, PermissionError, RuntimeError])
def test_the_room_digest_reads_public_cards_only_whatever_the_mirror_state(monkeypatch, mirror):
    """Findings 4 and 5: the room's digest never depends on reading the mirror state (a read failure cannot make it
    private), and a mirror armed mid-room finds no private card text already cached in the conductor."""
    def started():
        if isinstance(mirror, type):
            raise mirror("unreadable")
        return mirror
    monkeypatch.setattr(Y, "mirror_started", started)
    asked = []
    monkeypatch.setattr(B, "gather", lambda *a, **k: asked.append(k) or {})
    monkeypatch.setattr(B, "collect", lambda *a, **k: [])
    station = SimpleNamespace(cfg=None, s={"project_names": {}, "lang": "he"}, asking=())
    assert radio.Station.room_digest(station, "alpha") == ""
    assert asked == [{"public_only": True}]


def test_a_digest_cached_before_a_mirror_starts_holds_no_private_card(monkeypatch, lists):  # noqa: F811
    """The real gather and collect: private, team and hand-edited cards never enter the room's digest, so the
    conductor's cache (radio_room.Conductor.digests) holds nothing private when a mirror is armed later."""
    c = _staff_cfg(lists)
    _every_kind_of_private_card()
    publish("PublicLaunch", "public", session="s-pub", steps=["PubStepA", "PubStepB", "PubStepC"])
    monkeypatch.setattr(Y, "mirror_started", lambda: False)  # the mirror is off while the digest is made
    station = SimpleNamespace(cfg=c, s=radio.settings(c), asking=())
    conductor = SimpleNamespace(digests={}, digest=lambda key: radio.Station.room_digest(station, key),
                                rules=None)
    conductor.digests["alpha"] = conductor.digest("alpha")
    monkeypatch.setattr(Y, "mirror_started", lambda: True)   # now armed: the cached text is what would be said
    for word in PRIVATE_WORDS:
        assert word[:10] not in conductor.digests["alpha"], word
    assert "PubStepB" in conductor.digests["alpha"]  # the digest does read cards: the public one is there


def test_the_owners_own_home_board_still_reads_every_card(monkeypatch):
    asked = []
    monkeypatch.setattr(B, "gather", lambda *a, **k: asked.append(k) or {})
    monkeypatch.setattr(B, "collect", lambda *a, **k: [])
    station = SimpleNamespace(cfg=None, s={"project_names": {}, "lang": "he"}, asking=(), translations={},
                              show=SimpleNamespace(update=lambda projects: None))
    station.__dict__["show"] = station.show
    radio.Station.feed_board(station, NOW)
    assert asked == [{}]
