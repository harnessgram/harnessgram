"""Translation protocol regressions; all engines and state are fake."""
import json
import shutil
import subprocess
import threading

import pytest

from ctb import cockpit_v2, panel, panel_text
from ctb.config import Config
from test_panel_hebrew import gemini_cfg, wait_idle
from test_panel_page import func


@pytest.mark.parametrize("text", [
    "🔒 Approve the deployment after review?",
    "הסבר בעברית שמכיל הרבה יותר אותיות בעברית מאנגלית. " * 6 + "Hold the release.",
    "Подтвердить обновление приложения?",
    "承認する前に変更を確認してください",
    "ठीक है",
    "否",
    "A",
])
def test_prose_is_not_skipped_by_script_majority_or_emoji(text):
    calls = []
    tr = panel_text.Translator(gemini_cfg(), translate_fn=lambda s: calls.append(s) or "תרגום עברי")
    assert tr.he(text) is None
    wait_idle(tr)
    assert tr.he(text) == "תרגום עברי" and calls == [text]


@pytest.mark.parametrize("text", [
    ("A long approval condition. " * 180) + "\n\nFinal condition.",
    "x" * (panel_text.MAX_CHARS * 3 + 7),
])
def test_long_copy_is_chunked_without_dropping_any_source(text):
    calls = []
    pieces = list(panel_text._pieces(text))
    assert "".join(pieces) == text and all(0 < len(p) <= panel_text.MAX_CHARS for p in pieces)
    tr = panel_text.Translator(gemini_cfg(), translate_fn=lambda s: calls.append(s) or "תורגם")
    assert tr.he(text) is None
    wait_idle(tr)
    result = tr.he(text)
    assert isinstance(result, str) and result.count("תורגם") == len(pieces)
    assert calls == pieces
    assert tr.he(text) == result and calls == pieces  # completed fields are memoized


def test_a_failed_chunk_is_not_frozen_as_a_partial_translation(monkeypatch):
    text = "Approval condition. " * 180
    calls = []

    def engine(piece):
        calls.append(piece)
        return "תורגם" if len(calls) == 1 else ""

    tr = panel_text.Translator(gemini_cfg(), translate_fn=engine)
    assert tr.he(text) is None
    wait_idle(tr)
    assert tr.he(text) == "" and len(calls) == 2
    monkeypatch.setattr(panel_text, "RETRY_AFTER", 0)
    tr._fn = lambda _: "תורגם מחדש"
    assert tr.he(text) is None
    wait_idle(tr)
    assert "תורגם מחדש" in tr.he(text)


def test_an_unchanged_product_name_chunk_keeps_the_completed_translation():
    text = ("Review the release. " * 100)[:1599] + "\nCodex"
    calls = []

    def engine(piece):
        calls.append(piece)
        return piece if piece.strip() == "Codex" else "בדיקת הגרסה."

    tr = panel_text.Translator(gemini_cfg(), translate_fn=engine)
    assert tr.he(text) is None
    wait_idle(tr)
    assert tr.he(text) == "בדיקת הגרסה.\nCodex"
    assert calls == list(panel_text._pieces(text))
    assert tr.he(text) == "בדיקת הגרסה.\nCodex" and len(calls) == 2


@pytest.mark.parametrize("text,expected", [("Codex", "Codex"), ("Review the release.", "")])
def test_only_protected_names_can_succeed_without_any_translated_chunk(text, expected):
    calls = []
    tr = panel_text.Translator(gemini_cfg(), translate_fn=lambda s: calls.append(s) or s)
    assert tr.he(text) is None
    wait_idle(tr)
    assert tr.he(text) == expected and calls == [text]


def test_code_links_hashes_and_hebrew_do_not_use_an_engine():
    tr = panel_text.Translator(gemini_cfg(), translate_fn=lambda _: pytest.fail("engine call"))
    for text in ("אפשר לאשר?", "שָׁלוֹם", "123", "?!", "🔒🙂", "`Deploy the release`",
                 "https://example.com/approve", "5284cd49d", "/tmp/source.py"):
        assert tr.he(text) == ""
    assert tr.pending() == 0


def test_late_presentation_copy_is_translated_and_counted_without_changing_ids(monkeypatch):
    fake_secret = "sk-" + "proj-" + "Q" * 30
    seen = []
    # The normal state builder must run its late pass after presentation.
    monkeypatch.setattr(cockpit_v2, "presentation", lambda *a: {
        "projects": [{"key": "source-key", "name": "Project name " + fake_secret, "status": "working",
                      "work_map": [{"what": "Review approval", "impact": "Unblocks delivery"}]}]})

    class Pending:
        def he(self, text):
            seen.append(text)
            return None if text else ""

        def he_item(self, *args):
            return self.he(args[-1])

    # Set the actual switch in the sandbox; no real configuration is read.
    panel.panel_settings.write("ui_v2", "on")
    state = panel.build_state(Config(), tr=Pending(), light=True)
    row = state["cockpit_v2"]["projects"][0]
    assert row["name_he"] is None and row["work_map"][0]["what_he"] is None
    assert row["work_map"][0]["impact_he"] is None and state["pending"] >= 3
    assert row["key"] == "source-key" and row["status"] == "working"
    assert "key_he" not in row and "status_he" not in row
    assert fake_secret not in json.dumps(seen) and fake_secret not in json.dumps(state)


def test_widget_projection_preserves_parallel_list_positions(monkeypatch):
    monkeypatch.setattr(panel, "radio_player_on", lambda cfg: False)
    monkeypatch.setattr("ctb.waiting.waiting_count", lambda cfg: 1)
    state = {"pending": 2, "decisions": [{"id": "a" * 16, "title": "Approve?", "title_he": None}],
             "cards": [{"id": "b" * 16, "kind": "progress", "steps": ["Check", "בדיקה"],
                        "steps_he": [None, None]}]}
    out = panel.widget_view(Config(), state)
    assert "pending" not in out  # the iOS widget parser has no such field yet (build 24)
    assert out["decisions"][0]["title_he"] is None
    assert out["cards"][0]["steps_he"] == [None, None]


def test_state_refresh_picks_up_completed_hebrew_without_a_socket(monkeypatch):
    ready = threading.Event()
    original = {"id": "c" * 16, "title": "🔒 Approve the release?",
                "detail": "Release condition. " * 180, "yes": "Approve", "no": "Decline",
                "topic": "Release team", "url": "", "stale": "", "age": 0}
    monkeypatch.setattr(panel, "waiting_for_owner", lambda *a, **k: ([dict(original)], []))
    monkeypatch.setattr(panel, "collect", lambda *a, **k: [])

    def engine(text):
        assert ready.wait(3)
        return "נוסח עברי"

    tr = panel_text.Translator(gemini_cfg(), translate_fn=engine)
    try:
        first = panel.build_state(Config(), tr=tr, light=True)
        assert first["decisions"][0]["title_he"] is None and first["pending"] >= 5
    finally:
        ready.set()
    wait_idle(tr)
    second = panel.build_state(Config(), tr=tr, light=True)
    decision = second["decisions"][0]
    assert second["pending"] == 0 and decision["title_he"] == "נוסח עברי"
    assert decision["detail_he"].count("נוסח עברי") > 1
    for field, value in original.items():
        assert decision[field] == value  # translation must never replace the source or action identity


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
@pytest.mark.parametrize("pending,hidden,expected", [(1, False, 10000), (1, True, None), (0, False, None)])
def test_phone_keeps_refreshing_pending_translations_after_ten_attempts(pending, hidden, expected):
    script = r'''
let wa={initData:'fake'},loading=false,reloadPending=false,targetPending=false,targetClosed=false,
 priorWaiting=[],target='',view='me',followTimer=null,followups=10,last=null,ASKS={},S={};
let document={hidden:HIDDEN,getElementById:()=>({className:''})},window={};window.parent=window;
let timers=[];
function setTimeout(fn,delay){timers.push(delay);return 1;} function clearTimeout(){}
function status(){} function makeDigest(){} function render(d){last=d;} function radioPlayerRefresh(){}
function v2Enabled(){return true;}
function renderFailed(e){throw e;}
function fetch(){return Promise.resolve({ok:true,json:()=>Promise.resolve({pending:PENDING})});}
'''.replace("HIDDEN", json.dumps(hidden)).replace("PENDING", str(pending))
    script += func("load") + '\nload();setImmediate(()=>console.log(JSON.stringify({timers,followups})));'
    result = subprocess.run([shutil.which("node"), "-"], input=script, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    got = json.loads(result.stdout)
    assert got["timers"] == ([] if expected is None else [expected])
    if not pending:
        assert got["followups"] == 0


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_original_disclosure_includes_labels_and_nested_card_copy_in_one_tap():
    from test_panel_cards_by_session import DOM

    script = DOM.replace("STRINGS", json.dumps({"source": "מקור", "hideSource": "הסתר"})).replace("SESSIONS", "[]").replace("WA", "null")
    script += '\nvar srcOpen={};function bindBackLayer(k,n,on,apply){return apply;}\n'
    script += func("el") + func("source")
    script += r'''
let card={title:'Title',title_he:'כותרת',yes:'Approve',yes_he:'אישור',
 steps:['First step','בדיקה'],steps_he:['שלב ראשון',null],
 figures:[{label:'Open issues',label_he:'בעיות פתוחות'}]},host=new Element('div');
source('card',card,host);
let original=host.children[1],before=original.hidden;
host.children[0].listeners.click();
console.log(JSON.stringify({before,after:original.hidden,text:original.textContent}));
'''
    result = subprocess.run([shutil.which("node"), "-"], input=script, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    got = json.loads(result.stdout)
    assert got["before"] and not got["after"]
    assert all(s in got["text"] for s in ("Title", "Approve", "First step", "בדיקה", "Open issues"))


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_work_map_uses_completed_hebrew_and_keeps_original_one_tap_away():
    from test_cockpit_v2_workmap_node import card

    got = card([{"what": "Review release", "what_he": "בדיקת הגרסה", "status": "working",
                 "impact": "Unblocks delivery", "impact_he": "מאפשר מסירה"}],
               "var li=c.querySelector('.wm-line'),o=li.querySelector('.orig');var before=o.hidden;"
               "li.querySelector('.src-t').click();emit({before,after:o.hidden,original:o.textContent,"
               "what:li.querySelector('.wm-what').textContent,impact:li.querySelector('.wm-impact').textContent});")
    assert got["before"] and not got["after"]
    assert got["what"] == "בדיקת הגרסה" and "מאפשר מסירה" in got["impact"]
    assert "Review release" in got["original"] and "Unblocks delivery" in got["original"]
