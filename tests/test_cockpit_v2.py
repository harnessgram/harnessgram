"""Opt-in boundary, immutable public assets, truthful project status and packaging."""
import copy
import io
import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ctb import addons, cockpit_v2, panel, panel_page, panel_settings
from ctb.config import Config, state_dir
from conftest import GOOD_TOKEN
from test_panel import signed
from cockpit_v2_browser import sample_state


@pytest.mark.parametrize("mode,owner,want", [("off", True, False), ("preview", False, False),
    ("preview", True, True), ("on", True, True), ("on", False, True), ("unexpected", True, False)])
def test_preview_is_owner_only(mode, owner, want):
    assert cockpit_v2.enabled(mode, owner=owner) is want


def test_mode_is_persistent_closed_choice_and_defaults_off():
    assert cockpit_v2.mode() == panel_settings.read()["ui_v2"] == "off"
    for value in cockpit_v2.MODES:
        assert panel_settings.allowed("ui_v2", value)
        panel_settings.write("ui_v2", value)
        assert cockpit_v2.mode() == value
    for value in (True, False, None, "true", "owner", 1):
        assert not panel_settings.allowed("ui_v2", value)
        with pytest.raises(ValueError):
            panel_settings.write("ui_v2", value)
    (state_dir() / cockpit_v2.MODE_FILE).write_text("corrupt")
    assert cockpit_v2.mode() == "off"
    (state_dir() / cockpit_v2.MODE_FILE).write_bytes(b"\xff")
    assert cockpit_v2.mode() == "off"


def handler(path, method="GET", body=None, init=""):
    """Real request handling and HMAC auth, with an in-memory transport (no listener)."""
    h = object.__new__(panel.PanelHandler)
    payload = json.dumps(body or {}).encode()
    h.path, h.command = path, method
    h.headers = {"Content-Length": str(len(payload) if method == "POST" else 0), panel.INIT_HEADER: init}
    h.rfile = io.BytesIO(payload)
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=7)
    h.server = SimpleNamespace(cfg=cfg, bot_token=GOOD_TOKEN, owner_id=7, lang="he", translator=None, youtube_view=lambda: {})
    h._off = lambda: False
    replies = []
    h._send = lambda status, data, content_type, extra=None: replies.append((status, data, content_type, extra or {}))
    return h, replies


@pytest.mark.parametrize("value", cockpit_v2.MODES)
def test_preview_flag_is_emitted_only_after_owner_auth(monkeypatch, value):
    monkeypatch.setattr(panel, "build_state", lambda *a, **kw: {"ok": True, "settings": {"ui_v2": value}})
    h, replies = handler("/api/state", "POST", init=signed())
    h.do_POST()
    assert replies[0][0] == 200
    assert json.loads(replies[0][1])["ui_v2_enabled"] is (value != "off")
    h, replies = handler("/api/state", "POST", init=signed(user_id=8))
    h.do_POST()
    assert replies[0][0] == 403 and b"settings" not in replies[0][1]


@pytest.mark.parametrize("init", ["", "app1.sample.1.0.invalid", signed(user_id=8)],
                         ids=["missing", "invalid-app-token", "non-owner"])
def test_non_owner_cannot_turn_preview_on_or_mutate_requests(init):
    for path, body in [("/api/setting", {"name": "ui_v2", "value": "on"}),
                       ("/api/request", {"project": "alpha", "id": 1, "action": "done"}),
                       ("/api/decide", {"id": "1" * 16, "choice": "yes"})]:
        h, replies = handler(path, "POST", body, init)
        h.do_POST()
        assert replies[0][0] == 403
    assert cockpit_v2.mode() == "off"


def test_owner_can_change_preview_using_existing_setting_api():
    h, replies = handler("/api/setting", "POST", {"name": "ui_v2", "value": "preview"}, signed())
    h.do_POST()
    assert replies[0][0] == 200 and cockpit_v2.mode() == "preview"


def test_app_visual_switches_default_on_and_use_the_owner_setting_api():
    assert cockpit_v2.app_switches() == {"wideCockpit": True, "summaryRows": True}
    h, replies = handler("/api/setting", "POST", {"name": "app_wide_cockpit", "value": False}, signed())
    h.do_POST()
    assert replies[0][0] == 200
    assert cockpit_v2.app_switches() == {"wideCockpit": False, "summaryRows": True}

    h, replies = handler("/api/setting", "POST", {"name": "app_summary_rows", "value": False}, signed(user_id=8))
    h.do_POST()
    assert replies[0][0] == 403 and cockpit_v2.app_switches()["summaryRows"] is True


def test_state_contract_and_settings_page_carry_the_data_driven_app_switches():
    state = panel.build_state(Config(), light=True)
    assert state["appSwitches"] == {"wideCockpit": True, "summaryRows": True}
    page = panel_page.render_page("he", "nonce").decode()
    assert 'var APP_SWITCHES=[["app_wide_cockpit","wideCockpit","appWideCockpit"],["app_summary_rows","summaryRows","appSummaryRows"]];' in page
    assert 'data-v2-section","settings-app-appearance"' in page
    assert "מראה חדש באפליקציה" in page


def test_classic_settings_carry_the_localized_app_switches():
    code = panel_page.PAGE
    for setting in ("app_wide_cockpit", "app_summary_rows"):
        assert '"' + setting + '"' in code
        assert panel_page.STRINGS["he"]["set_" + setting]
        assert panel_page.STRINGS["en"]["hint_" + setting]
    screens = (Path(panel_page.__file__).parent / "cockpit_v2_screens.js").read_text()
    assert 'row.querySelector(".sw").setAttribute("aria-label",V2.settings[copy])' in screens


@pytest.mark.parametrize("name", sorted(cockpit_v2.ASSETS))
def test_only_public_brand_images_are_unauthenticated_and_cacheable(name):
    h, replies = handler(cockpit_v2.ASSET_PREFIX + name + ".webp")
    h._off = lambda: (_ for _ in ()).throw(AssertionError("public brand assets do not read private policy"))
    h.do_GET()
    status, data, content_type, headers = replies[0]
    assert status == 200 and content_type == "image/webp"
    assert data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    assert len(data) < 100000
    assert headers["Cache-Control"] == "public, max-age=31536000, immutable"


@pytest.mark.parametrize("name", ["../config.json", "%2e%2e/config.json", "header.png", "private.webp", "nested/header.webp", "artwork.webp/extra"])
def test_asset_route_refuses_every_non_catalogue_path(name):
    h, replies = handler(cockpit_v2.ASSET_PREFIX + name)
    h.do_GET()
    assert replies[0][0] == 404


def test_live_projects_have_real_progress_and_old_decisions_stay_waiting():
    state = sample_state()
    projects = state["cockpit_v2"]["projects"]
    assert [p["percent"] for p in projects] == [68, 82, 56, 74]
    assert [p["status"] for p in projects] == ["waiting", "waiting", "waiting", "blocked"]
    assert state["decisions"][2]["old"]
    assert [p["asset"] for p in projects] == ["plane", "book", "books", "visor"]


def test_ended_and_replaced_decisions_are_not_counted_as_waiting_but_old_ones_are():
    cfg = SimpleNamespace(projects=[], brief={})
    entry = SimpleNamespace(session="s0000000", chat=1, name="Plan: task")
    state = {"cards": [], "requests": [], "sessions": [], "brief": None,
             "decisions": [dict(id=str(n) * 16, topic="Plan: task", url="", stale=why) for n, why in
                           enumerate(("ended", "replaced", "old", ""), 1)]}
    plan = next(p for p in cockpit_v2.presentation(cfg, state, [entry], 1000)["projects"] if p["key"] == "plan")
    assert plan["waiting"] == 2 and plan["status"] == "waiting"


def project_for(cards=None, requests=None, brief=None):
    state = {"cards": cards or [], "requests": requests or [], "decisions": [], "sessions": [], "brief": brief}
    cfg = SimpleNamespace(projects=[], brief={})
    return cockpit_v2.presentation(cfg, state, [], 1000)["projects"]


@pytest.mark.parametrize("percent", [None, True, float("nan"), float("inf"), "68"])
def test_unknown_or_invalid_progress_is_never_invented(percent):
    p = project_for(cards=[dict(kind="progress", percent=percent, topic="alpha: task", age=5)])[0]
    assert p["percent"] is None and p["status"] == "on_track"


def test_completed_request_is_not_proof_that_project_shipped():
    p = project_for(requests=[dict(project="alpha", status="done", wait="team")])[0]
    assert p["percent"] is None and p["status"] != "shipped"


def test_explicit_completed_progress_is_shipped_but_auto_closed_is_not():
    card = dict(kind="progress", percent=100, topic="alpha: task", age=5, finished=True)
    assert project_for(cards=[card])[0]["status"] == "shipped"
    assert project_for(cards=[dict(card, auto_closed=True)])[0]["status"] == "on_track"
    assert project_for(cards=[dict(card, stale=True)])[0]["status"] == "blocked"


def test_latest_progress_wins_and_steps_supply_exact_progress():
    cards = [dict(kind="progress", percent=100, topic="alpha: first", age=200, finished=True),
             dict(kind="progress", steps=["a", "b", "c", "d"], at=2, topic="alpha: next", age=10)]
    p = project_for(cards=cards)[0]
    assert p["percent"] == 50 and p["status"] == "on_track"


def test_checklist_progress_and_stale_source_are_explicit():
    p = project_for(brief={"lines": [{"key": "alpha", "name": "Alpha", "age": 100000,
        "checklist": {"items": [{"done": True}, {"done": False}]}}]})[0]
    assert p["percent"] == 50 and p["stale"] is True
    assert p["age"] == 100000


def test_configured_art_wins_and_an_invalid_entry_falls_back_to_the_automatic_pick():
    cfg = SimpleNamespace(brief={"project_art": {"alpha": "plane", "beta": "../../private", "studio": "book"}})
    assert cockpit_v2.asset("alpha", cfg) == "plane"
    assert cockpit_v2.asset("studio", cfg) == "book"  # config beats the word "studio"
    assert cockpit_v2.asset("beta", cfg) == cockpit_v2.auto_asset("beta") in cockpit_v2.SPREAD
    assert cockpit_v2.asset("unknown", cfg) == cockpit_v2.auto_asset("unknown") in cockpit_v2.SPREAD
    assert cockpit_v2.asset("", cfg) == "generic"


@pytest.mark.parametrize("name,scene", [
    ("flight-school", "plane"), ("טיסה לילית", "plane"), ("Video studio", "clapper"), ("סטודיו", "clapper"),
    ("app-store-review-watch", "catalog"), ("קטלוג מוצרים", "catalog"), ("quest_lessons", "visor"), ("משקפי מציאות", "visor"),
    ("harnessgram-ops", "visor"), ("גשר", "visor"), ("weekly-lesson-plan", "book"), ("לימוד יומי", "book"),
    ("library", "books"), ("ספרים", "books"),
    # Words inside other words do not count: a ticket is not a flight, a number is not a book.
    ("כרטיס", None), ("מספר", None), ("overview", None)])
def test_project_words_pick_their_scene(name, scene):
    assert cockpit_v2.word_asset(name) == scene
    if scene:
        assert cockpit_v2.auto_asset(name) == scene


def test_unmatched_names_get_a_stable_spread_and_generic_only_without_a_name():
    names = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta"]
    first = [cockpit_v2.auto_asset(n) for n in names]
    assert first == [cockpit_v2.auto_asset(n) for n in names]  # deterministic, no per-process hash
    assert set(first) <= set(cockpit_v2.SPREAD) and len(set(first)) > 1
    assert cockpit_v2.auto_asset("Alpha") == cockpit_v2.auto_asset("alpha")
    assert cockpit_v2.auto_asset("") == cockpit_v2.spread_asset("") == "generic"
    # The value is fixed across releases for the same key (sha256, not hash()).
    import hashlib
    assert cockpit_v2.spread_asset("alpha") == cockpit_v2.SPREAD[int.from_bytes(hashlib.sha256(b"alpha").digest()[:4], "big") % 6]


def test_neighbouring_unplaced_projects_get_different_scenes_in_any_order():
    keys = ["alpha", "beta", "gamma", "delta", "epsilon", "studio"]
    cfg = SimpleNamespace(projects=[], brief={"project_art": {"alpha": "plane"}})
    for order in (keys, keys[::-1]):
        state = {"sessions": [dict(id="s%d" % i, name=k + ": task", age=60) for i, k in enumerate(order)],
                 "requests": [], "decisions": [], "cards": [], "reminders": []}
        got = {p["key"]: p["asset"] for p in cockpit_v2.presentation(cfg, state, [], 0)["projects"]}
        assert got["alpha"] == "plane" and got["studio"] == "clapper"
        assert len(set(got.values())) == len(got)  # six projects, six scenes
        assert "generic" not in got.values()
        if order is keys:
            first = got
        else:
            assert got == first


def test_strings_match_by_screen_and_styles_are_logical():
    strings = panel_page.V2_STRINGS
    assert strings["he"].keys() == strings["en"].keys() == {"shell", "requests", "now", "settings", "projects", "tools", "knowledge", "cleanup", "schedule", "addons", "family", "workmap", "voiceCopilot", "packs", "art", "native"}
    for screen in strings["he"]:
        assert strings["he"][screen].keys() == strings["en"][screen].keys()
        assert all(strings[lang][screen][k].strip() for lang in strings for k in strings[lang][screen])
    for name in ("cockpit_v2.css", "cockpit_v2_knowledge.css", "cockpit_v2_schedule.css", "cockpit_v2_addons.css", "cockpit_v2_family.css", "cockpit_v2_workmap.css"):
        css = (Path(panel_page.__file__).parent / name).read_text()
        assert not re.search(r"(?:^|[;{\s])(?:left|right|top|bottom|margin-left|margin-right|padding-left|padding-right)\s*:", css)
        assert ":root.cockpit-v2[data-theme=\"dark\"]" in css


def test_rendered_script_parses_in_node_and_packaged_files_are_declared(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    page = panel_page.render_page("he", "test").decode()
    script = page.split('<script nonce="test">', 1)[1].split('</script>', 1)[0]
    p = tmp_path / "page.js"
    p.write_text(script)
    checked = subprocess.run([node, "--check", str(p)], capture_output=True, text=True, timeout=15)
    assert checked.returncode == 0, checked.stderr
    config = (Path(panel.__file__).parents[2] / "pyproject.toml").read_text()
    for pattern in ("cockpit_v2.css", "cockpit_v2.js", "cockpit_v2_screens.js", "cockpit_v2_knowledge.js",
                    "cockpit_v2_knowledge.css", "cockpit_v2_addons.js", "cockpit_v2_addons.css", "cockpit_v2_schedule.js", "cockpit_v2_schedule.css", "cockpit_v2_workmap.js", "cockpit_v2_workmap.css", "cockpit_v2_family.js", "cockpit_v2_family.css", "cockpit_v2_art.js", "cockpit_v2_art.css", "cockpit_v2_strings.json",
                    "assets/cockpit/*.webp"):
        assert pattern in config


def test_state_has_no_v2_project_payload_when_switch_is_off(monkeypatch):
    monkeypatch.setattr(panel, "collect", lambda *a, **kw: [])
    assert "cockpit_v2" not in panel.build_state(Config(bot_token=GOOD_TOKEN, owner_chat_id=7), light=True)
    cockpit_v2.set_mode("preview")
    state = panel.build_state(Config(bot_token=GOOD_TOKEN, owner_chat_id=7), light=True)
    assert state["cockpit_v2"] == {"projects": [], "as_of": state["now"], "since": None}


@pytest.mark.parametrize("mode", ["off", "preview", "on"])
def test_v2_state_includes_every_reminder_the_shared_count_counts(monkeypatch, mode):
    from ctb import board, decisions

    rows = [(7, 1, dict(i=i, s=f"Reminder {i}", at=900, d="")) for i in range(37)]
    monkeypatch.setattr(panel, "collect", lambda *a, **kw: [])
    monkeypatch.setattr(decisions, "open_decisions", lambda: [])
    monkeypatch.setattr(board, "open_reminders", lambda: rows)
    cockpit_v2.set_mode(mode)
    state = panel.build_state(Config(bot_token=GOOD_TOKEN, owner_chat_id=7), now=1000, light=True)
    assert state["waiting_count"] == 37
    assert len(state["reminders"]) == (panel.MAX_REMINDERS if mode == "off" else state["waiting_count"])


@pytest.mark.parametrize("mode", ["preview", "on"])
@pytest.mark.parametrize("brief_on", [False, True])
def test_v2_uses_only_classic_state_and_respects_brief_switch(monkeypatch, mode, brief_on):
    fixture = sample_state()
    for decision in fixture["decisions"]:
        for key in ("project_key", "project_name", "project_asset"):
            decision.pop(key, None)
    monkeypatch.setattr(panel, "collect", lambda *a, **kw: [])
    monkeypatch.setattr(panel.session_usage, "snapshot", lambda *a: {"sessions": {}, "totals": None})
    monkeypatch.setattr(panel, "waiting_for_owner", lambda *a, **kw: (
        copy.deepcopy(fixture["decisions"]), copy.deepcopy(fixture["reminders"])))
    monkeypatch.setattr(panel.owner_requests, "read", lambda *a, **kw: copy.deepcopy(fixture["requests"]))
    monkeypatch.setattr(panel.brief, "on", lambda: brief_on)
    # A brief-only work line must never leak through a disabled brief switch.
    build = Mock(return_value={"lines": [{"key": "brief-only", "name": "Brief only", "text": "Extra work status",
        "age": 20, "checklist": {"items": [{"done": True}]}}], "waiting": [], "noticed": [], "since": None})
    monkeypatch.setattr(panel.brief, "build", build)
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=7)
    classic = panel.build_state(cfg, now=1000, light=True)
    build.reset_mock()
    cockpit_v2.set_mode(mode)
    state = panel.build_state(cfg, now=1000, light=True)
    assert build.call_count == int(brief_on)
    expected = cockpit_v2.presentation(cfg, copy.deepcopy(classic), [], 1000)
    assert state.pop("cockpit_v2") == expected
    assert ("brief" in state) is brief_on
    # Aside from presentation annotations and the switch, classic data is unchanged.
    for decision in state["decisions"]:
        for key in ("project_key", "project_name", "project_asset"):
            decision.pop(key, None)
    state["settings"]["ui_v2"] = "off"
    state.pop("topic_cleanup")  # the old-topics card data is v2-only (topic_cleanup.py)
    assert state.pop("addons") == addons.flags(cfg)  # the add-on switches the v2 page reads (addons.py)
    assert state == classic
    assert any(p["key"] == "brief-only" for p in expected["projects"]) is brief_on


def test_derived_identity_fields_pass_the_response_secret_filter(monkeypatch):
    secret = "gh" + "p_" + "R" * 36
    monkeypatch.setattr(panel, "collect", lambda *a, **kw: [])
    def derived(cfg, state, entries, now):
        state["decisions"].append({"project_name": secret})
        state["cards"].append({"project_key": secret})
        return {"projects": [{"name": secret}]}
    monkeypatch.setattr(cockpit_v2, "presentation", derived)
    cockpit_v2.set_mode("preview")
    state = panel.build_state(Config(bot_token=GOOD_TOKEN, owner_chat_id=7), light=True)
    assert secret not in json.dumps(state)
    assert "R" * 36 not in json.dumps(state)


def test_projects_reference_their_sessions_topics_and_reminders_without_copying():
    from cockpit_v2_browser import rich_state
    state = rich_state("en")
    projects = {p["key"]: p for p in state["cockpit_v2"]["projects"]}
    first = projects[state["decisions"][0]["project_key"]]
    assert {"sample00", "sample20"} <= set(first["sessions"])
    assert [t["key"] for t in state["topic_cards"]] == first["topics"]
    assert first["reminders"] == [r["ref"] for r in state["reminders"]]
    # References only: classic objects are never copied into the presentation.
    assert all(isinstance(k, list) for k in first["topics"])
    assert all(isinstance(s, str) for p in projects.values() for s in p["sessions"])


def test_topics_and_reminders_never_create_a_project():
    state = {"sessions": [], "requests": [], "decisions": [], "cards": [], "reminders": [
        {"topic": "Ghost: x", "ref": "1:1"}], "topic_cards": [{"project": "Ghost", "key": ["Ghost", "item", 1]}]}
    assert cockpit_v2.presentation(SimpleNamespace(projects=[], brief={}), state, [], 0)["projects"] == []


def test_settings_scenes_are_decoration_never_project_art():
    cfg = SimpleNamespace(projects=[], brief={"project_art": {"alpha": "bell", "beta": "header"}})
    assert cockpit_v2.asset("alpha", cfg) in cockpit_v2.SPREAD and cockpit_v2.asset("beta", cfg) in cockpit_v2.SPREAD
    for name in ("bell", "gears", "server", "mic", "laptop"):
        assert name in cockpit_v2.ASSETS and (cockpit_v2.ASSET_ROOT / (name + ".webp")).is_file()


def test_every_classic_setting_has_a_v2_group():
    """A classic switch must never vanish from v2 Settings; unknown ones fall back to Automatic work."""
    from test_panel_page import js
    code = js()
    switches = re.findall(r'"([a-z_]+)"', re.search(r"var SWITCHES=\[([^\]]*)\]", code).group(1))
    screens = (Path(panel_page.__file__).parent / "cockpit_v2_screens.js").read_text()
    grouped = set(re.findall(r'"([a-z_]+)"', screens[screens.index("var V2_GROUPS="):screens.index("// --- shared parts")]))
    app_switches = {setting for setting, _json_key, _copy in cockpit_v2.APP_SWITCHES}
    assert set(switches) | {"activity", "voice_digest", "auto_compact", "cold_compact"} <= grouped | app_switches
    assert 'if(name==="auto")Object.keys(rows).forEach' in screens


def test_same_named_topics_in_two_project_groups_never_cross_projects():
    """A decision belongs to the topic's place (group chat + thread), never to a name another project shares."""
    from ctb import panel_view
    cfg = SimpleNamespace(projects=[SimpleNamespace(name="Alpha", chat_id=-1001111111111), SimpleNamespace(name="Beta", chat_id=-1002222222222)], brief={})
    entries = [SimpleNamespace(session="aaaaaaaa1", chat=-1001111111111, thread=5, name="Weekly: plan"),
               SimpleNamespace(session="bbbbbbbb1", chat=-1002222222222, thread=7, name="Weekly: plan")]
    decision = lambda i, url: dict(id=str(i) * 16, topic="Weekly: plan", title="Approve?", detail="", yes="Yes", no="No",
                                   age=1, stale="", old=False, url=url)
    state = {"sessions": [], "requests": [], "cards": [], "decisions": [
        decision(1, panel_view.topic_url(-1001111111111, 5)), decision(2, panel_view.topic_url(-1002222222222, 7)), decision(3, "")],
        "reminders": [{"topic": "Weekly: plan", "url": "", "ref": "x:1"},
                      {"topic": "Weekly: plan", "url": panel_view.topic_url(-1002222222222, 7), "ref": "x:2"}]}
    for reverse in (False, True):  # entry order must not decide either
        got = cockpit_v2.presentation(cfg, copy.deepcopy(state), entries[::-1] if reverse else entries, 0)
        projects = {p["key"]: p for p in got["projects"]}
        assert {k: p["waiting"] for k, p in projects.items()} == {"alpha": 1, "beta": 1}
        assert projects["beta"]["reminders"] == ["x:2"] and projects["alpha"]["reminders"] == []
    mapped = copy.deepcopy(state)
    cockpit_v2.presentation(cfg, mapped, entries, 0)
    assert [d["project_key"] for d in mapped["decisions"]] == ["alpha", "beta", ""]
    # The ambiguous one stays on Now with its label, but on no project page.
    assert mapped["decisions"][2]["project_name"] == "Weekly" and mapped["decisions"][2]["project_asset"] == "generic"


def test_an_item_whose_place_left_the_list_is_not_reclaimed_by_name():
    """A decision with a place that matches no live topic stays off every project page, even when
    exactly one other project still has a topic of the same name."""
    from ctb import panel_view
    cfg = SimpleNamespace(projects=[SimpleNamespace(name="Alpha", chat_id=-1001111111111), SimpleNamespace(name="Beta", chat_id=-1002222222222)], brief={})
    entries = [SimpleNamespace(session="bbbbbbbb1", chat=-1002222222222, thread=7, name="Weekly: plan")]
    gone = panel_view.topic_url(-1001111111111, 5)  # Alpha's topic, no longer in the list
    state = {"sessions": [], "requests": [], "cards": [], "decisions": [
        dict(id="1" * 16, topic="Weekly: plan", title="Approve?", detail="", yes="Yes", no="No", age=1, stale="", old=False, url=gone)],
        "reminders": [{"topic": "Weekly: plan", "url": gone, "ref": "x:1"}]}
    got = cockpit_v2.presentation(cfg, state, entries, 0)
    assert state["decisions"][0]["project_key"] == ""
    assert all(p["waiting"] == 0 and p["reminders"] == [] for p in got["projects"])


def test_brief_line_owner_waits_count_once_beside_requests_and_decisions():
    line = {"key": "alpha", "name": "Alpha", "age": 300000, "waiting": 2}
    # The work lines alone: the project waits on the owner.
    p = project_for(brief={"lines": [line]})[0]
    assert p["waiting"] == 2 and p["status"] == "waiting"
    # The same ledger items also listed as requests: counted once, not twice.
    reqs = [dict(project="Alpha", id=i, status="open", wait="owner", updated=0) for i in (1, 2)]
    assert project_for(requests=reqs, brief={"lines": [line]})[0]["waiting"] == 2
    assert project_for(requests=reqs[:1], brief={"lines": [line]})[0]["waiting"] == 2
    assert project_for(requests=reqs + [dict(reqs[0], id=3)], brief={"lines": [line]})[0]["waiting"] == 3
    for bad in (True, "2", -1, None):
        assert project_for(brief={"lines": [dict(line, waiting=bad)]})[0]["waiting"] == 0


@pytest.mark.parametrize("path,body", [
    ("/api/request-detail", {"project": "demo", "id": 1}),
    ("/api/topic-detail", {"key": ["demo", "item", 1]}),
])
def test_detail_reads_use_owner_read_freshness_without_clearing_state(tmp_path, path, body):
    import time
    from test_owner_requests import item, put

    put(tmp_path, "demo", [item()])
    # A login older than an action's 15-minute limit remains valid for a read.
    h, replies = handler(path, "POST", body, signed(auth_date=int(time.time()) - 1800))
    h.server.cfg.brief = {"ledger_dir": str(tmp_path)}
    h.server.state_cache = SimpleNamespace(clear=Mock())
    h.do_POST()
    assert replies[0][0] == 200
    assert json.loads(replies[0][1])["ok"] is True
    h.server.state_cache.clear.assert_not_called()

    h, replies = handler(path, "POST", body, signed(user_id=8))
    h.server.cfg.brief = {"ledger_dir": str(tmp_path)}
    h.do_POST()
    assert replies[0][0] == 403
    assert json.loads(replies[0][1]) == {"error": "forbidden"}
