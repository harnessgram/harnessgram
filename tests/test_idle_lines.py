"""The idle-line nudger (idle_lines.py): open team ledger items that nobody moved show in the Cockpit and in
`ctb idle-lines`; a configured lead session (never a guessed one) gets one short fixed line, once per item per four
hours, only while it is quiet at an empty Claude prompt; parked, owner and done items are left alone; a shell, a
draft or another foreground process is never typed into; the nudged turn is quiet only for the exact line just
delivered to that session; and the Cockpit block draws the count and up to five rows."""
import datetime
import json
import subprocess
import time

import pytest

from ctb import hooks as H
from ctb import idle_lines as I
from ctb import panel_page
from ctb.config import Config, from_dict, state_dir
from ctb.sessions_menu import Entry
from test_panel_page import func

HOUR = 3600
NOW = 1_800_000_000.0
LEAD = "lead-my-project-1111"
LINE = "[bridge] Idle team lines need you: run `ctb idle-lines --project my-project` and start or park each one."


def row(ident, hours, project="my-project", status="open", wait="team", note="", title=None):
    return {"project": project, "id": ident, "title": title or f"item {ident}", "status": status, "wait": wait,
            "updated": NOW - hours * HOUR, "note": note, "steps": [{"text": note}] if note else []}


def cfg(**idle):
    return Config(idle_lines=idle)


def lead_cfg(**extra):
    return cfg(leads={"my-project": LEAD[:9]}, **extra)


def entry(session, name, age=60.0, state="idle", chat=None):
    return Entry(session, name, 1, age, state, False, chat)


def items(rows):
    return I.idle_items(rows, NOW)


def put_ledger(folder, rows):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "my-project.json").write_text(json.dumps({"next": 999, "items": rows}))


def ledger_item(ident, updated, notes=(), wait="team", text=None):
    return {"id": ident, "text": text or f"item {ident}", "status": "open", "wait": wait, "created": 1,
            "updated": updated, "notes": list(notes)}


# --- detection ---------------------------------------------------------------------------------------------

def test_detection_takes_only_open_team_items_past_the_threshold():
    rows = [row(1, 5), row(2, 3), row(3, 12, wait="owner"), row(4, 30, status="done"), row(5, 4.01)]
    got = I.idle_items(rows, NOW, 4 * HOUR)
    assert [x["id"] for x in got] == [1, 5]  # longest idle first; owner-wait and done excluded
    assert got[0]["idle"] == 5 * HOUR
    assert [x["id"] for x in I.idle_items(rows, NOW, 2 * HOUR)] == [1, 5, 2]


def test_parked_mid_sentence_with_a_date_from_the_real_ledger():
    note = "Stage 1 merged. parked: until 2026-11-16 AGY stage waits for AGY credits"
    end = (datetime.datetime(2026, 11, 16) + datetime.timedelta(days=1)).timestamp()
    assert I.parked_until(note, 0) == end
    before, after = end - HOUR, end + HOUR
    r = {"project": "my-project", "id": 7, "title": "x", "status": "open", "wait": "team", "updated": before - 10 * HOUR,
         "note": note}
    assert I.idle_items([r], before) == []
    r["updated"] = after - 10 * HOUR
    assert [x["id"] for x in I.idle_items([r], after)] == [7]  # the date passed: idle again


def test_parked_without_a_date_holds_for_a_day_and_any_case_counts():
    assert I.idle_items([row(1, 20, note="PARKED: waiting for the owner's Apple login")], NOW) == []
    assert [x["id"] for x in I.idle_items([row(1, 25, note="Parked: waiting")], NOW)] == [1]
    assert I.parked_until("parked: until 2026-13-40 bad date", 100.0) == 100.0 + I.PARKED_DEFAULT


def test_only_the_latest_note_parks():
    r = row(1, 6, note="started again after the review")
    r["steps"] = [{"text": "parked: until 2099-01-01 x"}, {"text": r["note"]}]
    assert [x["id"] for x in I.idle_items([r], NOW)] == [1]


def test_parked_is_found_in_the_whole_latest_note_past_the_display_cut(tmp_path):
    from ctb import owner_requests
    folder = tmp_path / "ledger"
    long_note = "x " * owner_requests.TEXT_CHARS + "parked: until 2099-01-01 waiting for credits"
    put_ledger(folder, [ledger_item(1, NOW - 9 * HOUR, ["earlier", long_note]), ledger_item(2, NOW - 9 * HOUR)])
    rows = owner_requests.read(NOW, folder)
    assert "parked:" not in rows[0]["note"]  # the display copy is cut before the marker
    assert [x["id"] for x in I.idle_items(rows, NOW)] == [1, 2]
    assert [x["id"] for x in I.idle_items(rows, NOW, notes=owner_requests.latest_notes(folder))] == [2]
    c = Config(brief={"ledger_dir": str(folder)})
    assert [x["id"] for x in I.read_items(c, NOW)] == [2]


def test_settings_default_on_and_can_be_turned_off():
    assert I.settings(Config())["on"] and I.settings(Config())["threshold"] == 4 * HOUR
    assert not I.settings(cfg(on=False))["on"]
    assert I.settings(cfg(hours=2))["threshold"] == 2 * HOUR
    assert I.settings(cfg(hours="x"))["threshold"] == 4 * HOUR
    assert from_dict({"idle_lines": {"on": False}}).idle_lines == {"on": False}


# --- nudge, throttle, state ---------------------------------------------------------------------------------

def test_one_short_fixed_line_per_project_then_throttled_for_four_hours(monkeypatch):
    sent = []
    send = lambda session, line, before_enter: sent.append((session, line)) or before_enter() or "sent"
    rows = [row(i, 4 + i, title=f"task {i}") for i in range(1, 8)] + [row(144, 12, title="iOS app")]
    monkeypatch.setattr(I, "read_items", lambda cfg, now: items(rows))
    entries = [entry(LEAD, "my-project: iOS release", age=3600), entry("other-1", "studio: cut")]
    assert I.check(lead_cfg(), NOW, items(rows), entries, send) == 1
    assert sent == [(LEAD, I.nudge_line("my-project"))]
    saved = json.loads((state_dir() / I.STATE_FILE).read_text())
    assert len(saved["items"]) == 8 and "my-project|144" in saved["items"]
    assert saved["delivered"][LEAD]["project"] == "my-project" and saved["delivered"][LEAD]["line"] == I.nudge_line("my-project")
    assert I.check(lead_cfg(), NOW + 120, items(rows), entries, send) == 0
    assert I.check(lead_cfg(), NOW + 4 * HOUR + 1, items(rows), entries, send) == 1


def test_a_busy_session_is_tried_again_and_nothing_is_recorded_until_sent(monkeypatch):
    answers = iter(["busy", "draft", "stale", "sent"])
    rows, entries = [row(1, 9)], [entry(LEAD, "my-project: x")]
    monkeypatch.setattr(I, "read_items", lambda cfg, now: items(rows))
    for n in range(3):
        assert I.check(lead_cfg(), NOW + n, items(rows), entries, lambda s, line, be: be() or next(answers)) == 0
        assert not I._read().get("items") and not I._read().get("delivered")  # the early record is gone again
    assert I.check(lead_cfg(), NOW + 9, items(rows), entries, lambda s, line, be: be() or next(answers)) == 1
    assert "my-project|1" in I._read()["items"]


def test_no_configured_lead_means_cockpit_only_and_no_guessing():
    sent = []
    rows = [row(1, 9)]
    # a live, recent session on the my-project line is NOT a lead unless configured
    assert I.check(cfg(), NOW, items(rows), [entry("bbbbbbbb-2", "my-project: build")], lambda s, line, be: sent.append(s)) == 0
    assert I.check(lead_cfg(), NOW, items([row(2, 9, project="beta")]), [entry(LEAD, "x")],
                   lambda s, line, be: sent.append(s)) == 0
    assert sent == []
    v = I.view(cfg(), rows, NOW, notes={})
    assert v["count"] == 1 and v["items"][0]["no_lead"] and not v["items"][0]["nudged"]
    assert not I.view(lead_cfg(), rows, NOW, notes={})["items"][0]["no_lead"]


def test_off_means_no_nudge_and_no_block():
    sent = []
    assert I.check(lead_cfg(on=False), NOW, items([row(1, 9)]), [entry(LEAD, "x")], lambda s, line, be: sent.append(s)) == 0
    assert sent == [] and I.view(cfg(on=False), [row(1, 9)], NOW, notes={}) == {"count": 0, "items": []}


def test_the_lead_must_be_configured_live_and_unique():
    entries = [entry("aaaaaaaa-1111", "my-project: old", age=900), entry("aaaaaaaa-2222", "my-project: new"),
               entry("cccccccc-3333", "my-project: gone", state="ended")]
    assert I.find_lead(cfg(), "my-project", entries) is None
    assert I.find_lead(cfg(leads={"My-project": "aaaaaaaa-1111"}), "my-project", entries) == "aaaaaaaa-1111"
    assert I.find_lead(cfg(leads={"my-project": "aaaaaaaa"}), "my-project", entries) is None  # ambiguous prefix
    assert I.find_lead(cfg(leads={"my-project": "cccccccc"}), "my-project", entries) is None  # an ended lead is no lead
    assert I.find_lead(cfg(leads={"my-project": "aaaa"}), "my-project", entries) is None  # too short to be a prefix


def test_read_decide_deliver_and_record_happen_under_one_lock(monkeypatch):
    events = []
    real = I._locked

    class Spy:
        def __enter__(self):
            events.append("lock")
            self.cm = real()
            return self.cm.__enter__()

        def __exit__(self, *a):
            events.append("unlock")
            return self.cm.__exit__(*a)

    write = I._write
    monkeypatch.setattr(I, "_locked", Spy)
    monkeypatch.setattr(I, "_write", lambda d: events.append("write") or write(d))
    monkeypatch.setattr(I, "read_items", lambda cfg, now: items([row(1, 9)]))
    I.check(lead_cfg(), NOW, items([row(1, 9)]), [entry(LEAD, "x")], lambda s, line, be: events.append("send") or be() or events.append("enter") or "sent")
    assert events == ["lock", "send", "write", "enter", "write", "unlock"]  # the delivery record before Enter
    assert (state_dir() / I.LOCK_FILE).exists()


def test_cli_lists_items_with_the_parking_instruction(tmp_path, capsys):
    from ctb import cli
    from ctb.config import config_path
    folder = tmp_path / "ledger"
    put_ledger(folder, [ledger_item(144, time.time() - 12 * HOUR - 60, text="iOS app")])
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"brief": {"ledger_dir": str(folder)}}))
    assert cli.main(["idle-lines"]) == 0
    out = capsys.readouterr().out
    assert "my-project #144 idle 12h: iOS app" in out and "parked: until <date> <reason>" in out
    assert cli.main(["idle-lines", "--project", "beta"]) == 0
    assert "No idle team lines in beta." in capsys.readouterr().out


# --- delivery safety ----------------------------------------------------------------------------------------

def _fake_pane(monkeypatch, command="2.1.30", pending=False, working=False, box="sent", quiet=3600.0,
               boxes=("", ""), foreground=(4242,)):
    from ctb import compact, inbox, sessions_menu, wake
    typed = []
    reads = iter(boxes)
    monkeypatch.setattr(inbox.Inbox, "pending", lambda self, s: pending)
    monkeypatch.setattr(inbox, "main_working", lambda s: working)
    monkeypatch.setattr(wake, "limit_active", lambda s: False)
    monkeypatch.setattr(inbox, "recently_moving", lambda s: False)
    monkeypatch.setattr(compact, "command_pending", lambda s: False)
    monkeypatch.setattr(sessions_menu, "_last_activity", lambda s: NOW - quiet)
    monkeypatch.setattr(inbox, "_pane_from_process", lambda s: "%7")
    monkeypatch.setattr(inbox, "session_pid", lambda s: 4242)
    monkeypatch.setattr(inbox, "_pane_command", lambda p: command)
    monkeypatch.setattr(inbox, "pane_runs_session", lambda p, s: True)
    monkeypatch.setattr(inbox, "_pane_claude_targets", lambda p: [(pid, "start") for pid in foreground])
    monkeypatch.setattr(inbox, "input_box", lambda p: (next(reads, "x"), True))

    def type_command(s, p, line, **kw):
        typed.append((s, p, line, kw.get("verify_session"), kw["still_ok"]()))
        return box
    monkeypatch.setattr(inbox, "type_command", type_command)
    return typed


def test_delivery_types_the_fixed_line_after_two_empty_reads(monkeypatch):
    typed = _fake_pane(monkeypatch)
    naps = []
    assert I.deliver("sess-1", LINE, now=NOW, sleep=naps.append) == "sent"
    assert typed == [("sess-1", "%7", LINE, True, True)] and naps == [I.SNAPSHOT_GAP]


def test_delivery_never_types_into_a_plain_shell_or_another_foreground_process(monkeypatch):
    for shell in ("zsh", "-zsh", "bash", "sh", "fish", ""):
        typed = _fake_pane(monkeypatch, command=shell)
        assert I.deliver("sess-1", LINE, now=NOW, sleep=lambda s: None) == "no_pane" and typed == []
    for fg in ((), (999,), (4242, 999)):
        typed = _fake_pane(monkeypatch, foreground=fg)
        assert I.deliver("sess-1", LINE, now=NOW, sleep=lambda s: None) == "no_pane" and typed == []


def test_delivery_waits_for_thirty_quiet_minutes_and_a_still_empty_box(monkeypatch):
    typed = _fake_pane(monkeypatch, quiet=I.QUIET_FOR - 1)
    assert I.deliver("sess-1", LINE, now=NOW, sleep=lambda s: None) == "busy"
    typed += _fake_pane(monkeypatch, boxes=("", "h"))  # someone started typing between the two reads
    assert I.deliver("sess-1", LINE, now=NOW, sleep=lambda s: None) == "busy"
    typed += _fake_pane(monkeypatch, boxes=("draft", ""))
    assert I.deliver("sess-1", LINE, now=NOW, sleep=lambda s: None) == "busy"
    typed += _fake_pane(monkeypatch, working=True)
    assert I.deliver("sess-1", LINE, now=NOW, sleep=lambda s: None) == "busy"
    typed += _fake_pane(monkeypatch, pending=True)
    assert I.deliver("sess-1", LINE, now=NOW, sleep=lambda s: None) == "busy"
    assert typed == []


@pytest.mark.real_type_command
def test_type_command_asks_still_ok_before_typing_and_before_enter(monkeypatch):
    import contextlib
    from ctb import inbox
    keys, asked = [], []
    box = {"text": ""}
    monkeypatch.setattr(inbox, "_nudge_guard", lambda s, wait=None: contextlib.nullcontext())
    monkeypatch.setattr(inbox, "_pane_from_process", lambda s: "%7")
    monkeypatch.setattr(inbox, "dismiss_tip", lambda p: False)
    monkeypatch.setattr(inbox, "typed_mark", lambda t: None)
    monkeypatch.setattr(inbox, "input_box", lambda p: (box["text"], True))
    monkeypatch.setattr(inbox, "_line_gone", lambda p, w: True)

    def tmux(*args):
        keys.append(args)
        if "-l" in args:
            box["text"] = args[-1]
        return type("R", (), {"returncode": 0})()
    monkeypatch.setattr(inbox, "_tmux", tmux)
    answers = iter([True, False])
    assert inbox.type_command("s", "%7", "hello", verify_session=True,
                              still_ok=lambda: asked.append(1) or next(answers)) == "stale"
    assert len(asked) == 2 and not any("Enter" in k for k in keys)  # typed, then refused before Enter
    keys.clear()
    box["text"] = ""
    assert inbox.type_command("s", "%7", "hello", still_ok=lambda: False) == "stale" and keys == []
    box["text"] = ""
    assert inbox.type_command("s", "%7", "hello", verify_session=True, still_ok=lambda: True) == "sent"
    assert any("Enter" in k for k in keys)


def test_the_heartbeat_checks_at_most_every_five_minutes(monkeypatch):
    calls = []
    monkeypatch.setattr(I, "check", lambda c, now: calls.append(now) or 0)
    monkeypatch.setattr(I, "_LAST_CHECK", [0.0])
    I.tick(Config(), NOW)
    I.tick(Config(), NOW + 60)
    I.tick(Config(), NOW + I.CHECK_EVERY)
    assert calls == [NOW, NOW + I.CHECK_EVERY]


# --- the nudged turn stays out of the topic -------------------------------------------------------------------

def _ev(role, text):
    return json.dumps({"type": role, "message": {"role": role, "content": text if role == "user"
                                                 else [{"type": "text", "text": text}]}})


def _delivered(session, at, line=LINE, project="my-project"):
    from ctb.config import write_private
    write_private(state_dir() / I.STATE_FILE,
                  json.dumps({"delivered": {session: {"at": at, "project": project, "line": line}}}))


def test_each_project_has_its_own_short_line():
    assert I.nudge_line("my-project") == LINE
    long = I.nudge_line("my-gcp-project")
    assert "--project my-gcp-project" in long and len(long) < 120
    for odd in ("two words", "x" * 25, "a;rm", "שלום", ""):
        assert I.nudge_line(odd) == "[bridge] Idle team lines need you: run `ctb idle-lines` and start or park each one."
    assert all("\n" not in I.nudge_line(p) for p in ("my-project", "two words"))


def test_only_that_projects_exact_line_just_delivered_to_this_session_is_quiet():
    now = time.time()
    _delivered(LEAD, now - 60)
    turn = [_ev("user", "  " + LINE + "\n"), _ev("assistant", "Started #1.")]
    assert H.background_only_turn(turn, session=LEAD)
    assert not H.background_only_turn(turn, session="someone-else")
    assert not H.background_only_turn(turn)  # no session known: mirrored
    other = [_ev("user", I.nudge_line("beta")), _ev("assistant", "x")]
    assert not H.background_only_turn(other, session=LEAD)  # another project's line was not delivered here
    # owner text with the line, in the same prompt or the same turn, is never hidden
    assert not H.background_only_turn([_ev("user", LINE + " and also deploy it"), _ev("assistant", "x")],
                                      session=LEAD)
    assert not H.background_only_turn([_ev("user", LINE), _ev("user", "owner typed this"),
                                       _ev("assistant", "x")], session=LEAD)
    _delivered(LEAD, now - I.DELIVERY_WINDOW - 1)
    assert not H.background_only_turn(turn, session=LEAD)  # an old delivery: mirrored


def test_the_record_exists_before_enter_and_goes_when_enter_fails(monkeypatch):
    seen = []

    def send(session, line, before_enter):
        before_enter()
        seen.append(I.is_bridge_prompt(line, session))  # a reply arriving now would already be quiet
        return "failed"  # then Enter failed
    monkeypatch.setattr(I, "read_items", lambda cfg, now: items([row(1, 9)]))
    assert I.check(lead_cfg(), NOW, items([row(1, 9)]), [entry(LEAD, "x")], send) == 0
    assert seen == [True]
    assert not I._read().get("delivered") and not I.is_bridge_prompt(LINE, LEAD)


@pytest.mark.real_type_command
def test_type_command_runs_before_enter_last_and_takes_the_line_back_when_it_fails(monkeypatch):
    import contextlib
    from ctb import inbox
    keys = []
    box = {"text": ""}
    monkeypatch.setattr(inbox, "_nudge_guard", lambda s, wait=None: contextlib.nullcontext())
    monkeypatch.setattr(inbox, "dismiss_tip", lambda p: False)
    monkeypatch.setattr(inbox, "typed_mark", lambda t: None)
    monkeypatch.setattr(inbox, "input_box", lambda p: (box["text"], True))
    monkeypatch.setattr(inbox, "_line_gone", lambda p, w: True)
    erased = []
    monkeypatch.setattr(inbox, "_erase_tail", lambda p, e, n, w: erased.append(e) or True)

    def tmux(*args):
        keys.append(args[-1])
        if "-l" in args:
            box["text"] = args[-1]
        return type("R", (), {"returncode": 0})()
    monkeypatch.setattr(inbox, "_tmux", tmux)
    assert inbox.type_command("s", "%7", "hello", before_enter=lambda: keys.append("record")) == "sent"
    assert keys[-2:] == ["record", "Enter"]
    keys.clear()
    box["text"] = ""

    def boom():
        raise OSError("disk full")
    assert inbox.type_command("s", "%7", "hello", before_enter=boom) == "failed"
    assert "Enter" not in keys and erased == ["hello"]


# --- Cockpit ------------------------------------------------------------------------------------------------

DOM = r"""
class Element {
 constructor(tag){this.tagName=tag;this.children=[];this.attrs={};this._text="";this.className="";}
 appendChild(c){this.children.push(c);return c;}
 set textContent(t){this._text=String(t);this.children=[];}
 setAttribute(k,v){this.attrs[k]=v;}
 addEventListener(){}
 get textContent(){return this._text+this.children.map(c=>c.textContent).join("");}
}
var document={createElement:t=>new Element(t)};
var S=STRINGS,app=new Element("div"),nf={format:String},FSI="⁨",PDI="⁩";
function icon(n){return new Element("svg");}
function enter(n){return n;}
function source(){}
function haptic(){}
function openLink(){}
function shown(x,f){var h=x[f+"_he"];return typeof h==="string"&&h?h:x[f];}
function he(x,f){return x[f+"_he"];}
function all(n){return [n].concat(n.children.flatMap(all));}
"""


def run_js(state, lang="he"):
    program = (DOM.replace("STRINGS", json.dumps(panel_page.STRINGS[lang])) + "\n"
               + "\n".join(func(n) for n in ("el", "iso", "sec", "item", "idleLines"))
               + "\nidleLines(%s);\nconsole.log(JSON.stringify({n:app.children.length,text:app.textContent,"
                 "rows:all(app).filter(x=>(x.className||'').indexOf('idle-line')>=0&&x.className.indexOf('row')===0).length}));"
               % json.dumps(state))
    out = subprocess.run(["node", "-e", program], capture_output=True, text=True, timeout=10, check=True)
    return json.loads(out.stdout)


def test_the_cockpit_block_shows_the_count_and_rows():
    rows = [row(i, 4 + i, title=f"task {i}") for i in range(1, 8)]
    rows[0]["title_he"] = "משימה אחת"
    view = I.view(cfg(), rows, NOW)
    assert view["count"] == 7 and len(view["items"]) == I.MAX_ITEMS
    out = run_js({"idle_lines": view})
    assert out["n"] == 2 and out["rows"] == I.MAX_ITEMS  # the section head and one card
    assert panel_page.STRINGS["he"]["idleTitle"] in out["text"] and "11 שעות" in out["text"]
    assert "ועוד 2" in out["text"]
    en = run_js({"idle_lines": view}, "en")
    assert "Idle lines" in en["text"] and "No movement for 11h" in en["text"]


def test_the_cockpit_block_draws_nothing_when_no_line_is_idle():
    assert run_js({"idle_lines": {"count": 0, "items": []}})["n"] == 0
    assert run_js({})["n"] == 0


def test_the_block_sits_on_the_home_view_and_uses_logical_css():
    assert "{idleLines(d);if(d.brief)renderBrief(d);else renderAll(d);}" in func("render")
    for key in ("idleTitle", "idleHours", "idleNudged", "idleNoLead", "idleMore"):
        assert panel_page.STRINGS["he"][key] and panel_page.STRINGS["en"][key]
    css = panel_page.PAGE[panel_page.PAGE.index(".idle-more{"):].split("}", 1)[0]
    assert "left" not in css and "right" not in css


def test_from_the_ledger_files_to_the_cockpit_state(tmp_path):
    from ctb import owner_requests, panel
    folder = tmp_path / "ledger"
    folder.mkdir()
    items = [{"id": 144, "text": "iOS app build", "status": "open", "wait": "team", "created": NOW - 20 * HOUR,
              "updated": NOW - 12 * HOUR, "notes": []},
             {"id": 145, "text": "AGY stage", "status": "open", "wait": "team", "created": NOW - 20 * HOUR,
              "updated": NOW - 12 * HOUR,
              "notes": ["Stage 1 merged. parked: until 2099-11-16 AGY stage waits for AGY credits"]},
             {"id": 146, "text": "Owner login", "status": "open", "wait": "owner", "created": NOW - 20 * HOUR,
              "updated": NOW - 12 * HOUR, "notes": []}]
    (folder / "my-project.json").write_text(json.dumps({"next": 147, "items": items}))
    c = Config(brief={"ledger_dir": str(folder)})
    rows = owner_requests.read(NOW, folder)
    assert [x["id"] for x in I.idle_items(rows, NOW)] == [144]
    state = panel.build_state(c, now=NOW, light=True)
    assert state["idle_lines"]["count"] == 1 and state["idle_lines"]["items"][0]["id"] == 144
    assert state["idle_lines"]["items"][0]["hours"] == 12


@pytest.mark.parametrize("change", ["done", "parked", "owner", "removed"])
@pytest.mark.parametrize("stage", ["before_send", "during_wait", "before_typing", "before_enter"])
@pytest.mark.real_type_command
def test_closed_or_changed_item_is_re_read_before_typing_and_enter(tmp_path, monkeypatch, change, stage):
    import contextlib
    from ctb import inbox

    folder = tmp_path / "ledger"
    original = ledger_item(1, NOW - 9 * HOUR)
    put_ledger(folder, [original])
    c = lead_cfg()
    c.brief = {"ledger_dir": str(folder)}
    snapshot = I.read_items(c, NOW)
    real_type = inbox.type_command
    _fake_pane(monkeypatch)
    monkeypatch.setattr(inbox, "type_command", real_type)

    def change_item():
        r = dict(original)
        if change == "done":
            r["status"] = "done"
        elif change == "parked":
            r["notes"] = ["parked: until 2099-01-01 waiting"]
        elif change == "owner":
            r["wait"] = "owner"
        put_ledger(folder, [] if change == "removed" else [r])

    if stage == "before_send":
        find = I.find_lead
        def lead(*args):
            result = find(*args)
            change_item()
            return result
        monkeypatch.setattr(I, "find_lead", lead)
    real_deliver = I.deliver
    def deliver(*args, **kw):
        return real_deliver(*args, sleep=lambda _: change_item() if stage == "during_wait" else None, **kw)
    monkeypatch.setattr(I, "deliver", deliver)
    keys, box = [], {"text": ""}
    monkeypatch.setattr(inbox, "_nudge_guard", lambda s, wait=None: contextlib.nullcontext())
    monkeypatch.setattr(inbox, "dismiss_tip", lambda p: change_item() if stage == "before_typing" else None)
    monkeypatch.setattr(inbox, "typed_mark", lambda t: None)
    monkeypatch.setattr(inbox, "input_box", lambda p: (box["text"], True))
    monkeypatch.setattr(inbox, "_line_gone", lambda p, w: True)
    def tmux(*args):
        keys.append(args)
        if "-l" in args:
            box["text"] = args[-1]
            if stage == "before_enter":
                change_item()
        return type("R", (), {"returncode": 0})()
    monkeypatch.setattr(inbox, "_tmux", tmux)
    assert I.check(c, NOW, snapshot, [entry(LEAD, "my-project")]) == 0
    assert not any("Enter" in k for k in keys)
    if stage != "before_enter":
        assert keys == []
    else:
        assert len(keys) == 1  # typed while open, refused after the close, before Enter
    assert not I._read().get("items") and not I._read().get("delivered")


def test_only_still_open_items_are_recorded_for_a_project(tmp_path):
    folder = tmp_path / "ledger"
    original = [ledger_item(n, NOW - 9 * HOUR) for n in (1, 2)]
    put_ledger(folder, original)
    c = lead_cfg()
    c.brief = {"ledger_dir": str(folder)}
    snapshot = I.read_items(c, NOW)
    put_ledger(folder, [dict(original[0], status="done"), original[1]])
    sent = []
    def send(session, line, before_enter):
        before_enter()
        sent.append(line)
        return "sent"
    assert I.check(c, NOW, snapshot, [entry(LEAD, "my-project")], send) == 1
    assert sent == [I.nudge_line("my-project")]
    assert set(I._read()["items"]) == {"my-project|2"}


def test_stale_push_does_not_nudge_an_item_that_the_team_changed(tmp_path, monkeypatch):
    folder = tmp_path / "ledger"
    original = ledger_item(1, NOW - 9 * HOUR)
    original["notes"] = ["pushed by owner"]
    put_ledger(folder, [original])
    cfg = lead_cfg()
    cfg.brief = {"ledger_dir": str(folder)}
    snapshot = I.read_items(cfg, NOW)
    changed = dict(original, notes=["team is working"], updated=NOW - 8 * HOUR)
    put_ledger(folder, [changed])
    sent = []
    assert I.check(cfg, NOW, snapshot, [entry(LEAD, "my-project")],
                   send=lambda *args: sent.append(args) or "sent") == 0
    assert sent == []
