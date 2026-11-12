"""The redesigned control panel: Hebrew copies of agent text, the strings table, logical CSS only, the
default-model switch, and the signed login that every route still needs."""
import json
import os
import re
import threading
import time

import pytest

from conftest import GOOD_TOKEN
from ctb import accounts, decisions, models, panel, panel_page, panel_text, translate
from ctb.config import Config, state_dir, write_private
from test_panel import OWNER, call, signed


def gemini_cfg(**kw):
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he", translate=True,
                  engines=["gemini", "mlx"], **kw)


def cache(text: str, hebrew: str, cfg: Config) -> None:
    tcfg = panel_text.panel_config(cfg)
    write_private(translate._cache_path(translate.apply_glossary(text, "he"), tcfg),
                  json.dumps({"text": hebrew, "engine": "gemini"}, ensure_ascii=False))


@pytest.fixture
def hebrew_server(monkeypatch):
    calls = []

    def fake_translate(text, cfg, fast=False, keep_links=False):
        calls.append(text)
        return "תרגום: " + text

    monkeypatch.setattr(translate, "translate_text", fake_translate)  # never the network
    cfg = gemini_cfg()
    srv = panel.PanelServer(("127.0.0.1", 0), cfg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv, calls
    srv.shutdown()
    srv.server_close()


def wait_idle(tr, seconds=3.0):
    end = time.time() + seconds
    while tr.pending() and time.time() < end:
        time.sleep(0.02)
    assert tr.pending() == 0


# --- Hebrew beside the original ------------------------------------------------------------------

def test_the_api_returns_the_cached_hebrew_at_once(hebrew_server):
    srv, calls = hebrew_server
    write_private(state_dir() / "topics.json", json.dumps({"abcd1234-ffff": {"thread": 11, "name": "מסלול: שחרור"}}))
    title = "Release 5284cd49d to Oak Hill and Cedar Park, including the phone-app update?"
    cache(title, "לשחרר את הגרסה לאוק היל ולסידר פארק, כולל עדכון האפליקציה?", srv.cfg)
    decisions.create("abcd1234-ffff", OWNER, 11, title, yes="כן, לשחרר", no="לחכות")
    body = json.loads(call(srv, init=signed())[2])
    d = body["decisions"][0]
    assert d["title"] == title  # the original stays, for the "מקור" toggle
    assert d["title_he"].startswith("לשחרר את הגרסה")
    assert "yes_he" not in d and "no_he" not in d and "topic_he" not in d  # already Hebrew: nothing to add
    assert title not in calls  # served from the cache, no engine call


def test_an_uncached_text_is_pending_first_and_hebrew_on_the_next_fetch(hebrew_server):
    srv, calls = hebrew_server
    write_private(state_dir() / "topics.json", json.dumps({"abcd1234-ffff": {"thread": 11, "name": "Pantry deploy"}}))
    decisions.create("abcd1234-ffff", OWNER, 11, "Merge the banner fix now?", yes="כן", no="לא עכשיו")
    first = json.loads(call(srv, init=signed())[2])
    assert first["decisions"][0]["title_he"] is None and first["pending"] >= 1  # the page shows the original meanwhile
    wait_idle(srv.translator)
    second = json.loads(call(srv, init=signed())[2])
    assert second["decisions"][0]["title_he"] == "תרגום: Merge the banner fix now?"
    assert second["sessions"][0]["name_he"] == "תרגום: Pantry deploy"
    assert second["pending"] == 0
    assert calls.count("Merge the banner fix now?") == 1  # each text is translated once


def test_the_topic_board_gets_hebrew_too(hebrew_server):
    from ctb import board
    srv, _ = hebrew_server
    write_private(state_dir() / "topics.json", json.dumps({"abcd1234-ffff": {"thread": 11, "name": "Pantry"}}))
    write_private(state_dir() / board.STATE_FILE, json.dumps({f"{OWNER}:11": {"id": 0, "items": [
        {"k": "check", "i": 1, "s": "Ship the hero photo", "x": False, "at": 6}]}}))
    init = signed(extra={"start_param": panel.topic_param(OWNER, 11)})
    call(srv, init=init)
    wait_idle(srv.translator)
    body = json.loads(call(srv, init=init)[2])
    assert body["topic"]["board"]["check"][0]["text_he"] == "תרגום: Ship the hero photo"
    assert body["topic"]["name_he"] == "תרגום: Pantry"


def test_no_translator_means_no_hebrew_fields_and_nothing_pending():
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he")
    write_private(state_dir() / "topics.json", json.dumps({"abcd1234-ffff": {"thread": 11, "name": "Deploy"}}))
    decisions.create("abcd1234-ffff", OWNER, 11, "Merge now?")
    body = panel.build_state(cfg, tr=panel_text.Translator(cfg))
    assert "title_he" not in body["decisions"][0] and body["pending"] == 0


@pytest.mark.parametrize("kw", [
    {},  # offline engines only, and NLLB has no model folder
    {"engines": ["mlx"]},  # never loads the big local model inside the panel
    {"engines": ["gemini"], "translate": False},  # network engines only when the owner allowed them
    {"engines": ["gemini"], "translate": True, "ui_language": "en"},
])
def test_the_panel_translates_only_with_light_allowed_engines(kw):
    assert panel_text.panel_config(Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he", **kw)) is None


def test_the_panel_config_drops_mlx_and_targets_hebrew():
    tcfg = panel_text.panel_config(gemini_cfg())
    assert tcfg.engines == ["gemini"] and tcfg.target_language == "he"


def test_hebrew_and_code_only_text_is_never_queued():
    tr = panel_text.Translator(gemini_cfg(), translate_fn=lambda t: pytest.fail("no engine call expected"))
    assert tr.he("לשחרר את הגרסה?") == "" and tr.he("") == "" and tr.he(None) == "" and tr.he("5284cd49d") == ""
    assert tr.pending() == 0


def test_a_failed_translation_shows_the_original_and_is_not_retried_at_once():
    seen = []
    tr = panel_text.Translator(gemini_cfg(), translate_fn=lambda t: seen.append(t) or "")
    assert tr.he("Deploy the fix") is None
    wait_idle(tr)
    assert tr.he("Deploy the fix") == "" and seen == ["Deploy the fix"]


def test_cached_reads_the_disk_cache_without_any_engine(monkeypatch):
    cfg = panel_text.panel_config(gemini_cfg())
    monkeypatch.setattr(translate, "_engines", lambda *a, **k: pytest.fail("no engine call expected"))
    assert translate.cached("Hold the release", cfg) is None
    cache("Hold the release", "לעצור את השחרור", gemini_cfg())
    assert translate.cached("Hold the release", cfg) == "לעצור את השחרור"


# --- the signed login is still required everywhere --------------------------------------------------

def test_every_route_still_needs_the_signed_owner_login(hebrew_server):
    srv, _ = hebrew_server
    stale = signed(auth_date=int(time.time()) - panel.DECIDE_MAX_AGE - 5)
    for path, body in (("/api/state", b"{}"), ("/api/model", b'{"model": "opus"}'), ("/api/decide", b"{}"),
                       ("/api/radio", b'{"on": true}')):
        assert call(srv, path=path, init=None, body=body)[0] == 403
        assert call(srv, path=path, init=signed(tamper=True), body=body)[0] == 403
        assert call(srv, path=path, init=signed(user_id=OWNER + 1), body=body)[0] == 403
    assert call(srv, path="/api/model", init=stale, body=b'{"model": "opus"}')[0] == 403  # acting needs a fresh login
    assert not models.settings_file().exists()


# --- the default model for new sessions ----------------------------------------------------------------

def test_the_model_switch_changes_only_the_model_key(hebrew_server):
    srv, _ = hebrew_server
    path = accounts.claude_home() / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": {"Stop": [1]}, "theme": "dark", "model": "claude-opus-5-5"}))
    os.chmod(path, 0o640)
    state = json.loads(call(srv, init=signed())[2])["default_model"]
    assert state["id"] == "claude-opus-5-5" and state["label"] == "Opus 5.5"
    assert [c["key"] for c in state["choices"]] == ["fable", "opus", "sonnet", "haiku"]
    code, _, data = call(srv, path="/api/model", init=signed(), body=b'{"model": "sonnet"}')
    assert code == 200 and json.loads(data) == {"ok": True, "label": "Sonnet 5.5"}
    saved = json.loads(path.read_text())
    assert saved == {"hooks": {"Stop": [1]}, "theme": "dark", "model": "claude-sonnet-5-5"}
    assert path.stat().st_mode & 0o777 == 0o640
    assert not [p for p in path.parent.iterdir() if p.name.endswith(".tmp")]


@pytest.mark.parametrize("body", [b'{"model": "gpt"}', b'{"model": "opus", "x": 1}', b'{}', b'[]', b'nope'])
def test_the_model_switch_refuses_anything_else(hebrew_server, body):
    srv, _ = hebrew_server
    assert call(srv, path="/api/model", init=signed(), body=body)[0] == 400
    assert not models.settings_file().exists()


def test_a_damaged_settings_file_is_never_overwritten(hebrew_server):
    srv, _ = hebrew_server
    path = accounts.claude_home() / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json")
    assert call(srv, path="/api/model", init=signed(), body=b'{"model": "haiku"}')[0] == 409
    assert path.read_text() == "{ not json"


# --- the page -----------------------------------------------------------------------------------

def test_the_strings_table_has_every_key_the_page_uses():
    he, en = panel_page.STRINGS["he"], panel_page.STRINGS["en"]
    assert he.keys() == en.keys()
    used = set(re.findall(r"\bS\.([A-Za-z0-9_]+)", panel_page.PAGE))
    assert used and not used - he.keys(), used - he.keys()
    assert all(v.strip() for v in list(he.values()) + list(en.values()))


def test_hebrew_strings_never_mix_english_outside_an_isolate():
    for key, value in panel_page.STRINGS["he"].items():
        bare = re.sub("⁨[^⁩]*⁩", "", value)
        assert not re.search(r"[A-Za-z]", bare), key
        for name in re.findall("⁨([^⁩]*)⁩", value):
            assert len(name.split()) <= 3, key


def test_the_page_uses_logical_css_only():
    page = panel_page.PAGE
    css = page[page.index("<style"):page.index("</style>")]
    assert not re.search(r"\b(left|right)\b", css)
    assert not re.search(r"\.style\.(left|right|margin(Left|Right)|padding(Left|Right)|textAlign)\b", page)
    assert not re.search(r"\b(margin|padding|border)-(left|right)\b", page)


def test_the_page_is_rtl_themed_and_safe():
    page = panel.render_page("he", "N").decode()
    assert 'dir="rtl"' in page and 'lang="he"' in page
    assert "--tg-theme-bg-color" in page and "--tg-theme-button-color" in page and "prefers-color-scheme:dark" in page
    assert "HapticFeedback" in page and "Intl.RelativeTimeFormat" in page and "/api/model" in page
    assert 'class="sk' in page  # a skeleton until the first answer
    assert "innerHTML" not in page and "eval(" not in page and "document.write" not in page
    assert page.count('nonce="N"') == 2


def test_an_alias_in_the_model_setting_still_marks_its_choice(hebrew_server):
    srv, _ = hebrew_server  # review of #147: "opus" in settings left every choice unselected
    path = accounts.claude_home() / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"model": "opus"}))
    state = json.loads(call(srv, init=signed())[2])["default_model"]
    assert state["id"] == "claude-opus-5-5" and state["label"] == "Opus 5.5"


def test_the_page_keeps_link_taps_and_answered_cards_where_they_belong():
    from ctb import panel_page
    page = panel_page.PAGE
    # Tapping "original" (or the original text) inside a board link never opens the link.
    assert 'e.preventDefault();if(e.target.closest&&e.target.closest(".src-t,.orig"))return;' in page
    # An answered decision of another topic is never shown in this topic's view.
    assert "answeredIds(false).length" in page and "Object.keys(answered).forEach" not in page
