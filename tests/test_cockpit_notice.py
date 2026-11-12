"""Question notices use only sample IDs and a fake Telegram bot."""
from types import SimpleNamespace
from urllib.parse import urlsplit, parse_qs
import multiprocessing
import subprocess

from ctb import alerts, cockpit_notice, decisions, cards, panel, panel_page, policy, sweeper

ID = "a1b2c3d4e5f6a7b8"
REC = {"id": ID, "session": "sample", "chat": -100123, "thread": 12}


def setup(monkeypatch, tmp_path, bot):
    cfg = SimpleNamespace(ui_language="en", bot_token="fake", owner_chat_id=1)
    monkeypatch.setattr(decisions, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cards, "state_dir", lambda: tmp_path)
    for ident in (ID, "fedcba9876543210"):
        rec = dict(REC, id=ident, title="Sample", detail="", yes="Yes", no="No", created=1, state="open")
        with decisions._locked() as path:
            data = decisions._load(path)
            data[ident] = decisions._record(ident, rec)
            decisions._save(path, data)
    monkeypatch.setattr(cockpit_notice.decisions, "owner_chats", lambda cfg: {-100123})
    monkeypatch.setattr(cockpit_notice.panel_pin, "known_name", lambda cfg: "SampleBot")
    return cfg


def test_notice_is_edited_and_link_has_only_question_id(monkeypatch, tmp_path, bot):
    cfg = setup(monkeypatch, tmp_path, bot)
    monkeypatch.setattr(alerts, "alert", lambda *a, **k: True)
    cockpit_notice.update(cfg, REC, True, bot)
    cockpit_notice.update(cfg, REC, True, bot)
    decisions.cancel("sample", ID)
    cockpit_notice.update(cfg, REC, False, bot)
    assert len(bot.methods("sendMessage")) == 1
    edits = bot.methods("editMessageText")
    assert len(edits) == 1
    assert edits[-1]["reply_markup"] == {"inline_keyboard": []}
    assert edits[-1]["text"] == bot.methods("sendMessage")[0]["text"]
    assert edits[-1]["message_id"] == bot.methods("sendMessage")[0]["message_id"]
    assert edits[-1]["text"].startswith("🗳")
    button = bot.methods("sendMessage")[0]["reply_markup"]["inline_keyboard"][0][0]
    assert parse_qs(urlsplit(button["url"]).query) == {"startapp": ["q" + ID]}
    assert panel.start_param_of("start_param=q" + ID) == "q" + ID


def test_alert_is_once_even_if_other_question_intervenes(monkeypatch, tmp_path, bot):
    cfg = setup(monkeypatch, tmp_path, bot)
    calls = []
    monkeypatch.setattr(alerts, "alert", lambda *a, **k: calls.append(k))
    other = dict(REC, id="fedcba9876543210")
    for rec in (REC, REC, other, REC):
        cockpit_notice.update(cfg, rec, True, bot)
    assert len(calls) == 2


def test_setting_off_suppresses_alert(monkeypatch, tmp_path, bot):
    cfg = setup(monkeypatch, tmp_path, bot)
    monkeypatch.setattr(alerts, "state", lambda cfg, bot: ({"id": -10}, {**alerts.DEFAULTS, "cockpit": "off"}))
    cockpit_notice.update(cfg, REC, True, bot)
    assert len(bot.methods("sendMessage")) == 1


def test_each_question_keeps_its_own_message(monkeypatch, tmp_path, bot):
    cfg = setup(monkeypatch, tmp_path, bot)
    monkeypatch.setattr(alerts, "alert", lambda *a, **k: True)
    other = dict(REC, id="fedcba9876543210")
    cockpit_notice.update(cfg, REC, True, bot)
    cockpit_notice.update(cfg, other, True, bot)
    decisions.cancel("sample", ID)
    cockpit_notice.update(cfg, REC, False, bot)
    decisions.cancel("sample", other["id"])
    cockpit_notice.update(cfg, other, False, bot)
    assert [p["message_id"] for p in bot.methods("editMessageText")] == [p["message_id"] for p in bot.methods("sendMessage")]
    assert all(p["reply_markup"] == {"inline_keyboard": []} for p in bot.methods("editMessageText"))


def test_non_owner_and_unsafe_urls_are_refused(monkeypatch, tmp_path, bot):
    cfg = setup(monkeypatch, tmp_path, bot)
    calls = []
    monkeypatch.setattr(alerts, "alert", lambda *a, **k: calls.append(k))
    cockpit_notice.update(cfg, dict(REC, chat=-100999), True, bot)
    assert not bot.calls and not calls
    for ident in (None, 42, ID + "?x=1", ID.upper(), "q" + ID):
        assert cockpit_notice.url(cfg, bot, ident) == ""
    monkeypatch.setattr(cockpit_notice.panel_pin, "known_name", lambda cfg: "SampleBot?startapp=bad")
    assert cockpit_notice.url(cfg, bot, ID) == ""


def test_close_before_open_does_not_restore_button(monkeypatch, tmp_path, bot):
    cfg = setup(monkeypatch, tmp_path, bot)
    decisions.cancel("sample", ID)
    cockpit_notice.update(cfg, REC, False, bot)
    cockpit_notice.update(cfg, REC, True, bot)
    assert not bot.methods("sendMessage")


def test_concurrent_processes_send_one_alert(monkeypatch, tmp_path, bot):
    cfg = setup(monkeypatch, tmp_path, bot)
    log = tmp_path / "alerts"
    def alert(*args, **kwargs):
        with log.open("a") as f:
            f.write("alert\n")
    monkeypatch.setattr(alerts, "alert", alert)
    ctx = multiprocessing.get_context("fork")
    workers = [ctx.Process(target=cockpit_notice.update, args=(cfg, REC, True, bot)) for _ in range(4)]
    for p in workers:
        p.start()
    for p in workers:
        p.join(10)
        assert p.exitcode == 0
    assert log.read_text().splitlines() == ["alert"]


def test_page_has_question_targets_and_highlight():
    page = panel_page.render_page("en", "test").decode()
    assert 'c.id="question-"+d.id' in page
    assert 'box.id="question-"+c.id' in page
    assert 'getElementById("question-"+id)' in page
    assert 'question-highlight' in page


def test_question_page_logic_executes_for_open_unknown_and_closed_ids():
    page = panel_page.render_page("en", "test").decode()
    start = page.index("function questionTarget(")
    end = page.index("var target=questionTarget(", start)
    js = page[start:end] + r'''
const assert = require("assert");
const id = "a1b2c3d4e5f6a7b8", other = "fedcba9876543210";
assert.equal(questionTarget("q" + id), id);
for (const value of ["q" + id + "&x=1", "q" + id.toUpperCase(), "bad", null]) assert.equal(questionTarget(value), "");
const d = {decisions:[{id:other}, {id:id}], cards:[]};
assert.equal(questionAvailable(d,id), true);
assert.equal(questionIndex(d.decisions,id,0), 1);
assert.equal(questionAvailable(d,"0000000000000000"), false);
assert.equal(questionAvailable({decisions:[], cards:[]},id), false);
assert.equal(questionAvailable({decisions:[{id:id,stale:"ended"}]},id), false);
let scrolled = "";
global.document = {getElementById: key => key === "question-" + id ? {scrollIntoView: opt => { scrolled = key + opt.block; }} : null};
assert.equal(scrollQuestion(id), true);
assert.equal(scrolled, "question-" + id + "center");
assert.equal(scrollQuestion(other), false);
'''
    subprocess.run(["node", "-e", js], check=True, capture_output=True, text=True)


def test_closed_during_link_lookup_sends_nothing(monkeypatch, tmp_path, bot):
    cfg = setup(monkeypatch, tmp_path, bot)
    def link(*args):
        decisions.cancel("sample", ID)
        return "https://t.me/SampleBot?startapp=q" + ID
    monkeypatch.setattr(cockpit_notice, "url", link)
    monkeypatch.setattr(alerts, "alert", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    cockpit_notice.update(cfg, REC, True, bot)
    assert not bot.methods("sendMessage")


def test_failed_edit_is_retried_by_sweep(monkeypatch, tmp_path, bot):
    cfg = setup(monkeypatch, tmp_path, bot)
    monkeypatch.setattr(alerts, "alert", lambda *a, **k: True)
    cockpit_notice.update(cfg, REC, True, bot)
    decisions.cancel("sample", ID)
    original = bot.call
    def fail(method, *args, **kwargs):
        if method == "editMessageText":
            raise OSError("temporary")
        return original(method, *args, **kwargs)
    monkeypatch.setattr(bot, "call", fail)
    cockpit_notice.update(cfg, REC, False, bot)
    assert decisions._load(tmp_path / decisions.STATE_FILE)[ID]["notice_pending"]
    monkeypatch.setattr(bot, "call", original)
    cockpit_notice.sweep(cfg, bot)
    assert len(bot.methods("editMessageText")) == 1
    assert not decisions._load(tmp_path / decisions.STATE_FILE)[ID].get("notice_pending")


def test_queue_scan_precedes_at_most_three_failed_notice_edits(monkeypatch, tmp_path, bot):
    cfg = setup(monkeypatch, tmp_path, bot)
    with decisions._locked() as path:
        data = {}
        for n in range(10):
            ident = f"{n:016x}"
            data[ident] = decisions._record(ident, dict(REC, id=ident, title="Sample", detail="", yes="Yes", no="No",
                                                         created=1, state="cancelled", notice_pending=True, notice_message=n + 1))
        decisions._save(path, data)
    events = []
    def fail(method, params, timeout):
        events.append((method, timeout))
        raise OSError("temporary")
    monkeypatch.setattr(bot, "call", fail)
    monkeypatch.setattr(policy, "on", lambda: True)
    inbox = SimpleNamespace(sessions=lambda: events.append(("scan", None)) or [])
    sw = sweeper.Sweeper(cfg, bot, inbox, workers=0)
    sw.sweep()
    assert events == [("scan", None)] + [("editMessageText", 5)] * 3


def test_notice_sweep_exception_does_not_escape(monkeypatch, tmp_path, bot):
    cfg = setup(monkeypatch, tmp_path, bot)
    monkeypatch.setattr(policy, "on", lambda: True)
    monkeypatch.setattr(cockpit_notice, "sweep", lambda *a: (_ for _ in ()).throw(OSError("temporary")))
    sw = sweeper.Sweeper(cfg, bot, SimpleNamespace(sessions=lambda: []), workers=0)
    sw.sweep()


def test_closed_question_card_blocks_other_answers_until_tapped():
    page = panel_page.render_page("en", "test").decode()
    start = page.index("function render(d,force){")
    end = page.index("function fail(", start)
    js = r'''
const assert = require("assert");
function node() { return {children:[], appendChild(x){this.children.push(x);}, addEventListener(e,fn){this.tap=fn;}}; }
const app=node(), tabs=node(), apps=node(), foot=node();
global.document={activeElement:null,getElementById(id){return {tabs,apps,foot}[id];}};
global.window={scrollY:0,scrollTo(){}};
let targetClosed=true, target="sample", view="me", last=null, stagger=0, targetPending=false;
const S={questionClosed:"closed",showOpenQuestions:"show"};
function el(tag,cls,text){let n=node();n.textContent=text;return n;}
function empty(ic,title){let n=node();n.title=title;return n;}
function tabsFn(){ app.children.push({answer:true}); }
const tabsRender=tabsFn;
''' + page[start:end].replace('tabs(d);', 'tabsRender(d);') + r'''
function renderAll(){} function renderHere(){} function renderAgents(){} function renderSettings(){}
function appOf(){return true;} function showApp(){} function applyChrome(){} function syncBack(){} function syncClose(){}
render({},true);
assert.equal(app.children.length,1);
assert.equal(app.children[0].title,"closed");
assert.equal(app.children[0].children.length,1);
assert.equal(app.children[0].children[0].textContent,"show");
assert.equal(app.children.some(x=>x.answer),false);
app.children=[];
render({},true);
app.children[0].children[0].tap();
assert.equal(targetClosed,false);
assert.equal(app.children.some(x=>x.answer),true);
'''
    subprocess.run(["node", "-e", js], check=True, capture_output=True, text=True)
    assert 'else{targetClosed=true;targetPending=false;}' in page
    assert 'priorWaiting&&!targetClosed' in page
    assert panel_page.STRINGS["he"]["questionClosed"]
    assert panel_page.STRINGS["en"]["questionClosed"]


def test_slow_send_does_not_hold_store_lock(monkeypatch, tmp_path, bot):
    import threading
    cfg = setup(monkeypatch, tmp_path, bot)
    entered, release = threading.Event(), threading.Event()
    original = bot.call
    def slow(method, *args, **kwargs):
        if method == "sendMessage":
            entered.set()
            assert release.wait(3)
        return original(method, *args, **kwargs)
    monkeypatch.setattr(bot, "call", slow)
    monkeypatch.setattr(alerts, "alert", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    worker = threading.Thread(target=cockpit_notice.update, args=(cfg, REC, True, bot))
    worker.start()
    assert entered.wait(2)
    monkeypatch.setattr(decisions, "_where", lambda *a: (-100123, 12))
    monkeypatch.setattr("ctb.progress.append_boundary", lambda *a, **k: None)
    inbox = SimpleNamespace(put=lambda *a, **k: None)
    assert decisions.answer(cfg, inbox, ID, "yes")["state"] == "answered"
    release.set()
    worker.join(3)
    assert not worker.is_alive()
    assert len(bot.methods("editMessageText")) == 1


def test_unreadable_store_sends_nothing(monkeypatch, tmp_path, bot):
    cfg = setup(monkeypatch, tmp_path, bot)
    (tmp_path / decisions.STATE_FILE).write_text("{broken")
    cockpit_notice.update(cfg, REC, True, bot)
    assert not bot.calls
    assert (tmp_path / decisions.STATE_FILE).read_text() == "{broken"


def test_notice_history_is_bounded_by_decision_history(monkeypatch, tmp_path, bot):
    cfg = setup(monkeypatch, tmp_path, bot)
    monkeypatch.setattr(alerts, "alert", lambda *a, **k: True)
    for n in range(decisions.KEEP_CLOSED + 10):
        ident = f"{n:016x}"
        rec = dict(REC, id=ident, title="Sample", detail="", yes="Yes", no="No", created=n, state="open")
        with decisions._locked() as path:
            data = decisions._load(path)
            data[ident] = decisions._record(ident, rec)
            decisions._save(path, data)
        cockpit_notice.update(cfg, rec, True, bot)
        decisions.cancel("sample", ident, now=n + 1)
        cockpit_notice.update(cfg, rec, False, bot)
    data = decisions._load(tmp_path / decisions.STATE_FILE)
    assert sum(r["state"] != "open" for r in data.values()) == decisions.KEEP_CLOSED
    assert not (tmp_path / "cockpit-notices.json").exists()


def test_failed_alert_is_not_retried(monkeypatch, tmp_path, bot):
    cfg = setup(monkeypatch, tmp_path, bot)
    calls = []
    def fail(*args, **kwargs):
        calls.append(1)
        raise OSError("temporary")
    monkeypatch.setattr(alerts, "alert", fail)
    cockpit_notice.update(cfg, REC, True, bot)
    cockpit_notice.update(cfg, REC, True, bot)
    assert calls == [1]


def test_deleted_message_finishes_pending_removal(monkeypatch, tmp_path, bot):
    from ctb.telegram import TelegramError
    cfg = setup(monkeypatch, tmp_path, bot)
    monkeypatch.setattr(alerts, "alert", lambda *a, **k: True)
    cockpit_notice.update(cfg, REC, True, bot)
    decisions.cancel("sample", ID)
    monkeypatch.setattr(bot, "call", lambda *a, **k: (_ for _ in ()).throw(
        TelegramError("Bad Request: message to edit not found", 400)))
    assert cockpit_notice.update(cfg, REC, False, bot)
    assert not decisions._load(tmp_path / decisions.STATE_FILE)[ID].get("notice_pending")


def test_a_cut_off_send_is_tried_again_by_the_sweep(monkeypatch, tmp_path, bot):
    cfg = setup(monkeypatch, tmp_path, bot)
    monkeypatch.setattr(alerts, "alert", lambda *a, **k: True)
    with decisions._locked() as path:
        data = decisions._load(path)
        data[ID]["notice_message"] = -1  # the process stopped in the middle of the send
        decisions._save(path, data)
    created = decisions._load(tmp_path / decisions.STATE_FILE)[ID]["created"]
    cockpit_notice.sweep(cfg, bot, now=created + 30)
    assert bot.methods("sendMessage") == []  # a send may still be running
    cockpit_notice.sweep(cfg, bot, now=created + 61)
    assert len(bot.methods("sendMessage")) == 1
    assert decisions._load(tmp_path / decisions.STATE_FILE)[ID]["notice_message"] > 0


def test_stuck_removals_do_not_starve_the_others(monkeypatch, tmp_path, bot):
    cfg = setup(monkeypatch, tmp_path, bot)
    with decisions._locked() as path:
        data = {}
        for n in range(10):
            ident = f"{n:016x}"
            data[ident] = decisions._record(ident, dict(REC, id=ident, title="Sample", detail="", yes="Yes", no="No",
                                                         created=1, state="cancelled", notice_pending=True, notice_message=n + 1))
        decisions._save(path, data)
    tried = set()
    def fail(method, params, timeout):
        tried.add(params["message_id"])
        raise OSError("temporary")
    monkeypatch.setattr(bot, "call", fail)
    for _ in range(40):
        cockpit_notice.sweep(cfg, bot)
    assert len(tried) == 10
