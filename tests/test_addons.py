"""What HarnessGram adds to your agents (addons.py): the catalogue, its gates, locks, the endpoint and the page.

Every test runs on the isolated config and state directories from conftest; the real bridge config is never read."""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import GOOD_TOKEN
from ctb import addons, app_request, hooks, knowledge, mac_tools, missed_replies, panel, panel_page, panel_settings
from ctb.config import Config, state_dir, write_private
from test_panel import OWNER, call, server, signed  # noqa: F401  (server is a fixture)
from test_panel_page import func

SRC = Path(addons.__file__).parent
DOC = Path(__file__).resolve().parent.parent / "docs" / "addons.md"
PENDING = {"recall_after_compact": "feat/recall-after-compact", "rule_change_notice": "feat/recall-after-compact",
           "listen_button": "feat/listen-button"}
LOCKED = {"approval_gates", "secret_redaction", "owner_only", "sensitive_wording", "reply_mirror"}


def _sources(exclude=("addons.py",)):
    return {p.name: p.read_text(encoding="utf-8") for p in SRC.glob("*.py") if p.name not in exclude}


# --- the catalogue -------------------------------------------------------------------------------------------------

def test_catalogue_shape_groups_and_locks():
    keys = [a.key for a in addons.CATALOGUE]
    assert len(keys) == len(set(keys)) and set(keys) == set(addons.BY_KEY)
    assert all(re.fullmatch(r"[a-z][a-z_]{2,30}", k) for k in keys)
    assert {a.group for a in addons.CATALOGUE} == set(addons.GROUPS)
    assert {a.key for a in addons.CATALOGUE if a.locked} == LOCKED
    assert all(a.group == "safety" or a.key == "reply_mirror" for a in addons.CATALOGUE if a.locked)
    assert all(a.locked for a in addons.CATALOGUE if a.group == "safety")
    assert {a.key: a.pending for a in addons.CATALOGUE if a.pending} == PENDING
    for a in addons.CATALOGUE:
        assert a.providers and set(a.providers) <= set(addons.HARNESSES), a.key
        assert a.source[0] in ("config", "setting", "marker", "store", "locked"), a.key


def test_the_brief_inventory_is_covered():
    for key in ("reply_mirror", "translation", "rules_reminder", "progress_cards", "progress_reminder", "missed_replies",
                "compact_helper", "auto_compact", "topic_titles", "morning_brief", "radio_replies", "voice_digest",
                "car_mode", "knowledge_overlay", "recall_after_compact", "rule_change_notice", "listen_button",
                "app_request", "mac_tools", "topic_cleanup", "schedule_card"):
        assert key in addons.BY_KEY, key


def test_user_text_is_provider_neutral_and_hebrew_only():
    for a in addons.CATALOGUE:
        for text in (a.title_he, a.title_en, a.text_he, a.text_en):
            assert text.strip()
            assert not re.search(r"claude|codex|gemini|קלוד|קודקס", text, re.I), (a.key, text)
        for text in (a.title_he, a.text_he):
            bare = re.sub("⁨[^⁩]*⁩", "", text)
            assert not re.search(r"[A-Za-z]", bare), (a.key, text)
            assert re.search(r"[֐-׿]", bare)


def test_every_harness_module_declares_its_add_ons():
    """A new harness module (harness_<name>.py) must be listed in addons.HARNESSES, and an add-on must support it."""
    found = {"claude"} | {p.stem[len("harness_"):] for p in SRC.glob("harness_*.py")}
    assert set(addons.HARNESSES) == found, "declare the new harness in addons.HARNESSES and in each add-on it supports"
    assert set(addons.HARNESS_NAMES) == found
    for name, module in addons.HARNESSES.items():
        assert (SRC / module).exists(), module
        assert any(name in a.providers for a in addons.CATALOGUE), name


def test_every_key_is_read_by_a_real_gate():
    """A switch that nothing reads would lie to the owner: each key must reach the code it switches."""
    src = _sources()
    joined = "\n".join(src.values())
    fields = set(Config.__dataclass_fields__)
    for a in addons.CATALOGUE:
        kind = a.source[0]
        if a.pending:
            continue  # its gate lands with its own branch; the key already answers here
        if kind == "store":
            pattern = r"(?:addons\.enabled|_addon_on)\(\s*[\w.]+,\s*\"%s\"" % a.key
            assert re.search(pattern, joined), "%s: no addons.enabled(..., %r) anywhere in src" % (a.key, a.key)
        elif kind in ("config", "locked") and a.source[1]:
            attr = a.source[1]
            assert attr in fields, a.key
            readers = [n for n, text in src.items() if n not in ("config.py", "setup.py", "setup_page.py", "panel_settings.py")
                       and re.search(r"\.%s\b|getattr\([^)]*\"%s\"" % (attr, attr), text)]
            assert readers, "%s: nothing reads config %s" % (a.key, attr)
        elif kind == "setting":
            assert a.source[1] in panel_settings.SETTINGS, a.key
        elif kind == "marker":
            name = a.source[1]
            assert sum(f'"{name}"' in text for n, text in src.items()) >= 1, a.key


@pytest.mark.new_install
def test_defaults_match_the_settings_they_mirror():
    cfg = Config()
    for a in addons.CATALOGUE:
        if a.source[0] == "config":
            assert a.default == bool(getattr(cfg, a.source[1])), a.key
        if a.source[0] in ("setting", "marker", "store"):
            assert addons.enabled(cfg, a.key, a.providers[0]) == a.default, a.key
        if a.locked:
            assert addons.enabled(cfg, a.key, a.providers[0]) is True


def test_docs_table_is_generated_from_the_catalogue():
    assert DOC.read_text(encoding="utf-8") == addons.markdown(), \
        "regenerate: PYTHONPATH=src python -m ctb addons docs > docs/addons.md"
    for a in addons.CATALOGUE:
        assert "`%s`" % a.key in DOC.read_text(encoding="utf-8")


# --- reading and switching -------------------------------------------------------------------------------------

def test_store_switch_round_trip_is_private():
    cfg = Config()
    assert addons.enabled(cfg, "missed_replies") is True
    out = addons.set_on(cfg, "missed_replies", False)
    assert out == {"key": "missed_replies", "on": False, "previous": True}
    assert addons.enabled(cfg, "missed_replies") is False
    path = state_dir() / addons.STORE
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    addons.set_on(cfg, "missed_replies", True)
    assert addons.enabled(cfg, "missed_replies") is True


@pytest.mark.parametrize("key", sorted(LOCKED))
def test_locked_items_refuse_to_switch(key):
    with pytest.raises(addons.Locked):
        addons.set_on(Config(), key, False)
    assert addons.enabled(Config(), key, addons.BY_KEY[key].providers[0]) is True


def test_unknown_and_bad_values_are_refused():
    with pytest.raises(addons.Unknown):
        addons.set_on(Config(), "nope", False)
    with pytest.raises(addons.AddonError):
        addons.set_on(Config(), "missed_replies", "no")
    with pytest.raises(KeyError):
        addons.enabled(Config(), "nope")


def test_config_add_ons_write_the_config_key_and_the_live_copy(monkeypatch):
    written = []
    monkeypatch.setattr(panel_settings, "_config_set", lambda name, value: written.append((name, value)))
    cfg = Config(translate=True)
    out = addons.set_on(cfg, "translation", False)
    assert written == [("translate", False)] and cfg.translate is False and out["previous"] is True
    assert addons.enabled(cfg, "translation") is False


def test_a_setting_add_on_is_the_same_switch_and_returns_to_its_old_value():
    cfg = Config()
    panel_settings.write("auto_compact", 250)
    assert addons.enabled(cfg, "auto_compact") is True
    addons.set_on(cfg, "auto_compact", False)
    assert panel_settings.read()["auto_compact"] == 0
    addons.set_on(cfg, "auto_compact", True)
    assert panel_settings.read()["auto_compact"] == 250
    addons.set_on(cfg, "voice_digest", False)
    assert panel_settings.read()["voice_digest"] == "off"
    addons.set_on(cfg, "voice_digest", True)
    assert panel_settings.read()["voice_digest"] == "important"
    addons.set_on(cfg, "morning_brief", True)
    assert panel_settings.read()["brief_home"] is True


def test_car_mode_is_the_bots_own_marker():
    from ctb import inbox
    addons.set_on(Config(), "car_mode", True)
    assert inbox.car_mode_active() is True
    inbox.set_car_mode(False)
    assert addons.enabled(Config(), "car_mode") is False


def test_providers_decide_where_an_add_on_applies():
    cfg = Config()
    assert addons.enabled(cfg, "reply_mirror", "codex") is True
    assert addons.enabled(cfg, "progress_reminder", "codex") is False  # not a Codex add-on
    addons.set_on(cfg, "knowledge_overlay", False)
    assert addons.enabled(cfg, "knowledge_overlay", "codex") is False  # one global switch for every harness
    # The file format has no per-harness override: a stray one is ignored, the owner's switch still decides.
    write_private(state_dir() / addons.STORE, json.dumps({"on": {"knowledge_overlay": False},
                                                          "overrides": {"codex": {"knowledge_overlay": True}}}))
    assert addons.enabled(cfg, "knowledge_overlay", "codex") is False and addons.flags(cfg)["knowledge_overlay"] is False


# --- the Cockpit can never switch itself off ------------------------------------------------------------------

def test_no_switchable_add_on_maps_onto_a_master_switch():
    for a in addons.CATALOGUE:
        if not a.locked:
            assert tuple(a.source[:2]) not in addons.MASTER_SWITCHES, a.key
    assert ("config", "mirror") in addons.MASTER_SWITCHES and ("setting", "ui_v2") in addons.MASTER_SWITCHES


def test_set_on_refuses_a_master_switch_even_if_the_catalogue_were_wrong(monkeypatch):
    bad = addons.Addon("bad", "conversation", "א", "a", "א", "a", ("config", "mirror"))
    monkeypatch.setitem(addons.BY_KEY, "bad", bad)
    with pytest.raises(addons.Locked):
        addons.set_on(Config(), "bad", False)


def test_every_switch_leaves_the_bridge_and_the_cockpit_running(server):
    """Flip every switchable add-on away from its default through the endpoint: the bridge stays on and the add-ons
    route keeps answering, so each switch can be undone from the phone."""
    from ctb import policy
    from ctb.config import config_path
    write_private(config_path(), json.dumps({"bot_token": GOOD_TOKEN, "owner_chat_id": OWNER, "mirror": True}))
    for a in addons.CATALOGUE:
        if a.locked:
            continue
        status, out = _post(server, {"action": "set", "key": a.key, "on": not a.default})
        assert status == 200, a.key
        assert policy.on(), a.key
        assert _post(server, {"action": "view"})[0] == 200, a.key
        assert _post(server, {"action": "set", "key": a.key, "on": a.default})[0] == 200, a.key


# --- fail-safe ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("damage", ["{not json", "[]", json.dumps({"on": {"missed_replies": "no"}}), json.dumps({"on": 5})])
def test_a_damaged_store_answers_the_defaults(damage):
    write_private(state_dir() / addons.STORE, damage)
    assert all(addons.enabled(Config(), a.key, a.providers[0]) == (True if a.locked else a.default)
               for a in addons.CATALOGUE if a.source[0] == "store")


def test_a_failing_reader_answers_the_default(monkeypatch):
    monkeypatch.setattr(addons, "read_map", lambda path: (_ for _ in ()).throw(OSError("disk")))
    assert addons.enabled(Config(), "missed_replies") is True
    assert addons.enabled(Config(), "translation") is False
    monkeypatch.setattr(addons, "_raw", lambda *a: 1 / 0)
    assert addons.flags(Config())["missed_replies"] is True


def test_hooks_fall_back_to_defaults_when_the_registry_cannot_load(monkeypatch):
    monkeypatch.setitem(sys.modules, "ctb.addons", None)  # `from . import addons` raises ImportError
    import ctb
    monkeypatch.delattr(ctb, "addons", raising=False)
    assert hooks._addon_on(Config(), "rules_reminder") is True
    assert hooks._addon_on(Config(), "x", default=False) is False
    out = hooks.prompt_output({"prompt": "hi"}, Config(extra_reminder="Keep it short."))
    assert "Keep it short." in out["hookSpecificOutput"]["additionalContext"]
    assert missed_replies._on(Config()) is True and app_request._on(Config()) is True and mac_tools._on(Config()) is True
    assert knowledge._overlay_on(Config()) is True


def test_a_hook_call_reads_the_switch_file_once(monkeypatch, tmp_path):
    reads = []
    real = addons.read_map
    monkeypatch.setattr(addons, "read_map", lambda path: reads.append(path) or real(path))
    monkeypatch.setattr(hooks, "home_chat", lambda c: -1001234567890)
    cfg = Config(chat_id=-1001234567890, extra_reminder="Keep it short.", periodic_reminder="Save.")
    monkeypatch.setattr(hooks, "load_config", lambda: cfg)
    t = tmp_path / "t.jsonl"
    t.write_text("x")
    hooks.hook_prompt(json.dumps({"session_id": "s-once", "prompt": "hello", "cwd": str(tmp_path),
                                  "transcript_path": str(t)}))
    assert len([p for p in reads if p.name == addons.STORE]) == 1
    reads.clear()
    hooks.hook_post(json.dumps({"session_id": "s-once", "tool_name": "Bash", "transcript_path": str(t)}))
    assert len([p for p in reads if p.name == addons.STORE]) <= 1


# --- the gates -----------------------------------------------------------------------------------------------------

def _off(key):
    addons.set_on(Config(), key, False)


def test_rules_reminder_gates_the_standing_and_periodic_reminders(tmp_path):
    cfg = Config(extra_reminder="Keep it short.", periodic_reminder="Save a lesson.", periodic_reminder_min_kb=0)
    t = tmp_path / "t.jsonl"
    t.write_text("x")
    _off("rules_reminder")
    assert hooks.prompt_output({"prompt": "hi"}, cfg) is None
    assert hooks.periodic_reminder("s1", str(t), cfg) == ""


def test_progress_reminder_and_title_sync_gates(monkeypatch):
    monkeypatch.setattr(hooks, "_progress_nudge", lambda session, now=None: "make a card")
    assert hooks.progress_nudge("s1") == "make a card"
    _off("progress_reminder")
    assert hooks.progress_nudge("s1") == ""


def test_board_hint_parts_follow_their_switches(monkeypatch):
    cfg = Config(chat_id=-1001234567890)
    monkeypatch.setattr(hooks, "home_chat", lambda c: -1001234567890)
    assert hooks.board_hint(1, cfg) == hooks.BOARD_HINT
    _off("compact_helper")
    assert hooks.board_hint(1, cfg) == hooks.TOOLS_HINT
    _off("cockpit_hint")
    assert hooks.board_hint(1, cfg) is None


def test_missed_replies_app_request_and_mac_tools_gates():
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, app_request_command=["/bin/echo"],
                 mac_tools={"dry_run": ["/bin/echo"], "maintenance": ["/bin/echo"]})
    assert app_request.configured(cfg) is True and mac_tools.configured(cfg) is True
    for key in ("missed_replies", "app_request", "mac_tools"):
        _off(key)
    assert missed_replies.sweep(cfg, force=True, spawn_fn=lambda *a: pytest.fail("swept")) == 0
    assert app_request.configured(cfg) is False and mac_tools.configured(cfg) is False


def test_the_knowledge_overlay_pauses_to_an_empty_list():
    assert "Nothing is switched off." in knowledge.overlay_text({"items": []}, paused=True)
    _off("knowledge_overlay")
    assert knowledge._overlay_on(Config()) is False


def test_compact_helper_gate_in_the_cli(capsys):
    from ctb import cli
    assert cli._compact_helper_on() is True
    _off("compact_helper")
    assert cli._compact_helper_on() is False


# --- the endpoint ----------------------------------------------------------------------------------------------

def _post(srv, body, init=None):
    status, _, data = call(srv, path="/api/addons", init=signed() if init is None else init,
                           body=json.dumps(body).encode())
    return status, json.loads(data or b"{}")


def test_endpoint_needs_the_fresh_owner_login(server):
    assert call(server, path="/api/addons", body=b'{"action":"view"}')[0] == 403
    assert _post(server, {"action": "view"}, init=signed(user_id=OWNER + 1))[0] == 403
    assert _post(server, {"action": "view"}, init=signed(tamper=True))[0] == 403
    stale = signed(auth_date=int(time.time()) - panel.DECIDE_MAX_AGE - 60)
    assert _post(server, {"action": "set", "key": "missed_replies", "on": False}, init=stale)[0] == 403
    assert addons.enabled(server.cfg, "missed_replies") is True


def test_endpoint_view_set_and_errors(server):
    status, view = _post(server, {"action": "view"})
    assert status == 200 and [g["key"] for g in view["groups"]] == list(addons.GROUPS)
    rows = {i["key"]: i for g in view["groups"] for i in g["items"]}
    assert rows["owner_only"]["locked"] and rows["reply_mirror"]["providers"] == ["Claude", "Codex"]
    status, out = _post(server, {"action": "set", "key": "missed_replies", "on": False})
    assert status == 200 and out["previous"] is True and out["on"] is False and out["item"]["on"] is False
    assert addons.enabled(server.cfg, "missed_replies") is False
    assert _post(server, {"action": "set", "key": "secret_redaction", "on": False}) == (409, {"error": "locked"})
    assert _post(server, {"action": "set", "key": "reply_mirror", "on": False}) == (409, {"error": "locked"})
    status, again = _post(server, {"action": "view"})
    assert {i["key"]: i["on"] for g in again["groups"] for i in g["items"]} == addons.flags(server.cfg)
    assert _post(server, {"action": "set", "key": "nope", "on": False}) == (404, {"error": "unknown"})
    assert _post(server, {"action": "set", "key": "missed_replies", "on": "no"})[0] == 400
    assert _post(server, {"action": "set", "key": "missed_replies"})[0] == 400
    assert _post(server, {"action": "drop"})[0] == 400


def test_switched_off_routes_answer_not_found(server):
    _off("schedule_card")
    _off("topic_cleanup")
    assert call(server, path="/api/schedule-view", init=signed(), body=b"{}")[0] == 404
    assert call(server, path="/api/schedule", init=signed(), body=b'{"action":"remove","id":"x"}')[0] == 404
    assert call(server, path="/api/topic-cleanup", init=signed(), body=b'{"action":"keep","ids":["abcd1234"]}')[0] == 404


# --- the page (node, the page's real functions) ----------------------------------------------------------------

NODE = shutil.which("node")
HERE = Path(__file__).parent
node = pytest.mark.skipif(not NODE, reason="node is not installed")


def run_page(body, lang="he", reply=None, status=200):
    from cockpit_v2_browser import sample_state
    view = addons.view(Config())
    script = (SRC / "cockpit_v2.js").read_text().replace("__V2_STRINGS__", json.dumps(panel_page.V2_STRINGS, ensure_ascii=False))
    script = 'var requestProject="",requestOwnerOnly=false;\n' + "\n".join(func(n) for n in ("el", "icon", "svgAdd", "he", "shown", "compactStorageKey", "compactBlock",
                                         "requestAction", "requestRow", "requestRank", "requestDetail", "requestList", "requestGroups", "requestText", "requestBoard", "v2ModeRow", "settingsBox")) + script
    for name in ("cockpit_v2_screens.js", "cockpit_v2_knowledge.js", "cockpit_v2_addons.js"):
        script += "\n" + (SRC / name).read_text()
    script += "\nvar view='settings';function render(){};vaData=%s;" % json.dumps(view, ensure_ascii=False)
    prelude = (HERE / "studio_fake_dom.js").read_text() + (HERE / "cockpit_v2_fake_dom.js").read_text()
    post = ("var VIEW=%s,REPLY=%s,STATUS=%d,toasts=[];sandbox.toast=(t,bad)=>toasts.push([t,!!bad]);sandbox.setTimeout=()=>0;"
            "sandbox.post=(path,body)=>{posted.push([path,body]);var rep=body.action==='view'?VIEW:Object.assign({},REPLY,{key:body.key,on:body.on});"
            "return Promise.resolve({ok:STATUS===200,status:STATUS,json:()=>Promise.resolve(JSON.parse(JSON.stringify(rep)))});};"
            % (json.dumps(view, ensure_ascii=False), json.dumps(reply or {"ok": True, "previous": True}), status))
    src = prelude + "\n" + post + "\nbootV2(%s,%s,%s,%s);\n(async()=>{root.textContent='';%s})().catch(e=>{console.error(e);process.exit(1);});" % (
        json.dumps(script), json.dumps(sample_state(lang)), json.dumps(panel_page.STRINGS[lang]), json.dumps(lang), body)
    result = subprocess.run([NODE, "-"], input=src, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@node
@pytest.mark.parametrize("lang", ["he", "en"])
def test_page_groups_rows_switches_and_locks(lang):
    got = run_page("sandbox.vaOpen=true;sandbox.vaPage(sandbox.last);"
                   "emit({groups:allEls().filter(e=>e.attrs['data-va-group']).map(e=>e.attrs['data-va-group']),"
                   "switches:allEls().filter(e=>e.attrs['data-va-toggle']).map(e=>e.attrs['data-va-toggle']),"
                   "locked:allEls().filter(e=>e.attrs['data-va-locked']!==undefined).map(e=>e.parentNode.attrs['data-va-key']),"
                   "always:allEls().filter(e=>e.attrs['data-va-locked']!==undefined).map(e=>e.textContent),"
                   "who:allEls().filter(e=>e.attrs['data-va-providers']!==undefined).map(e=>e.textContent)});", lang)
    s = panel_page.V2_STRINGS[lang]["addons"]
    assert got["groups"] == list(addons.GROUPS)
    assert got["switches"] == [a.key for a in addons.CATALOGUE if not a.locked]
    assert set(got["locked"]) == LOCKED and set(got["always"]) == {s["alwaysOn"]}
    mirror = got["who"][[a.key for a in addons.CATALOGUE].index("reply_mirror")]
    assert mirror == (s["appliesTo"] + ": ⁨Claude⁩, ⁨Codex⁩" if lang == "he" else s["appliesTo"] + ": Claude, Codex")


@node
def test_a_switch_posts_at_once_and_offers_a_one_tap_undo():
    got = run_page("sandbox.vaOpen=true;sandbox.vaPage(sandbox.last);"
                   "allEls().filter(e=>e.attrs['data-va-toggle']==='missed_replies')[0].click();await flush();"
                   "root.textContent='';sandbox.vaPage(sandbox.last);var snack=byClass('va-snack')[0];var text=snack.textContent;"
                   "allEls().filter(e=>e.attrs['data-va-undo']==='missed_replies')[0].click();await flush();emit({posted,text,toasts});")
    title = addons.BY_KEY["missed_replies"].title_he
    assert got["posted"][0] == ["/api/addons", {"action": "set", "key": "missed_replies", "on": False}]
    assert got["posted"][1] == ["/api/addons", {"action": "set", "key": "missed_replies", "on": True}]
    assert title in got["text"]
    assert [panel_page.V2_STRINGS["he"]["addons"]["undone"], False] in got["toasts"]


@node
def test_a_refused_switch_goes_back_and_says_why():
    got = run_page("sandbox.vaOpen=true;sandbox.vaPage(sandbox.last);"
                   "allEls().filter(e=>e.attrs['data-va-toggle']==='missed_replies')[0].click();await flush();"
                   "root.textContent='';sandbox.vaPage(sandbox.last);"
                   "emit({checked:allEls().filter(e=>e.attrs['data-va-toggle']==='missed_replies')[0].getAttribute('aria-checked'),toasts,"
                   "snack:byClass('va-snack').length});", status=409)
    assert got == {"checked": "true", "toasts": [[panel_page.V2_STRINGS["he"]["addons"]["lockedErr"], True]], "snack": 0}


@node
def test_settings_lists_the_add_ons_entry_after_the_knowledge_entry():
    got = run_page("var d=sandbox.last;d.settings=d.settings||{};sandbox.SWITCHES=[];sandbox.S.set_x='x';"
                   "sandbox.v2Capture=f=>[];sandbox.hookHealthCard=()=>{};"
                   "try{sandbox.v2Settings(d);}catch(e){}emit(root.children.slice(0,3).map(e=>e.attrs['data-v2-section']||e.className));")
    assert got[1:] == ["knowledge-entry", "addons-entry"]


def test_strings_are_hebrew_only_outside_isolates_and_match():
    he, en = panel_page.V2_STRINGS["he"]["addons"], panel_page.V2_STRINGS["en"]["addons"]
    assert he.keys() == en.keys()
    used = set(re.findall(r'vaT\("([A-Za-z_]+)"\)', (SRC / "cockpit_v2_addons.js").read_text()))
    used |= {"group_" + g for g in addons.GROUPS}
    assert used <= set(he), used - set(he)
    for key, value in he.items():
        assert not re.search(r"[A-Za-z]", re.sub("⁨[^⁩]*⁩", "", value)), key
