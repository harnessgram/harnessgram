"""Helper agents: fakes only. No real claude, tmux, launchctl, Keychain, network or API, and no real config."""
import json
import logging
import subprocess
import time
from types import SimpleNamespace

import pytest

from ctb import auto_cards, cards, helper_agents as H, helper_jobs as J

SECRET_KEY = "sk-ant-api03-" + "x" * 40
NOW = time.time()


def cfg(**over):
    base = {"card_titles": False, "patrol": False, "ledger_capture": False, "route": "subscription",
            "api_fallback": False, "daily_usd_cap": 0.5, "api_until": ""}
    base.update(over)
    return SimpleNamespace(helper_agents=base, owner_chat_id=1, chat_id=-100, mirror=True)


def today_plus(days):
    from datetime import datetime, timedelta
    from ctb.session_usage import ZONE
    return (datetime.fromtimestamp(NOW, ZONE) + timedelta(days=days)).date().isoformat()


class Fakes:
    """Every outside call the helpers can make, replaced."""

    def __init__(self, monkeypatch):
        self.cli, self.posts, self.key_reads = [], [], 0
        self.cli_answer = lambda stdin: json.dumps({"is_error": False, "subtype": "success", "result": "ok"})
        self.api_reply = lambda: (200, json.dumps({"content": [{"type": "text", "text": "api ok"}],
                                                   "usage": {"input_tokens": 100, "output_tokens": 20}}).encode())
        self.key = SECRET_KEY

        def run_claude(args, stdin, env, cwd, timeout):
            self.cli.append((list(args), stdin, dict(env)))
            return self.cli_answer(stdin)

        def post(url, headers, body, timeout):
            self.posts.append((url, dict(headers), body))
            return self.api_reply()

        def read_key():
            self.key_reads += 1
            return self.key

        monkeypatch.setattr(H, "_run_claude", run_claude)
        monkeypatch.setattr(H, "_claude_binary", lambda: "claude")
        monkeypatch.setattr(H, "_post", post)
        monkeypatch.setattr(H, "_read_key", read_key)
        monkeypatch.setattr(H, "_key_failed_at", {})
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("a real process was started"))

    def say(self, text):
        self.cli_answer = lambda stdin: json.dumps({"is_error": False, "subtype": "success", "result": text})


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    monkeypatch.setattr(J, "_capture_state", {"last": {}, "recent": [], "seen": set()})
    monkeypatch.setattr(J, "_patrol_at", None)


@pytest.fixture
def fk(monkeypatch):
    return Fakes(monkeypatch)


def api_cfg(**over):
    return cfg(ledger_capture=True, route="api", api_fallback=True, api_until=today_plus(30), **over)


# --- the switches and the settings ---------------------------------------------------------------

def test_every_switch_off_is_inert(fk):
    c = cfg()
    assert H.ask(c, "card_titles", "s", "p") is None
    assert J.card_names(c, "Fix the login page", ["Run the tests"]) is None
    assert J.run_patrol(c, NOW, sources={"stale": lambda: pytest.fail("scanned"),
                                         "stuck": lambda: pytest.fail("scanned"),
                                         "failed": lambda: pytest.fail("scanned")}) == 0
    assert J.capture(c, "s1", -100, 5, "please add a dark mode to the settings page") is None
    assert fk.cli == [] and fk.posts == [] and fk.key_reads == 0
    assert cards.open_cards() == []


def test_defaults_are_all_off_and_a_bad_section_reads_as_off():
    assert not any(H.enabled(SimpleNamespace(), j) for j in H.JOBS)
    assert not any(H.enabled(SimpleNamespace(helper_agents={"patrol": True, "daily_usd_cap": "x"}), j) for j in H.JOBS)
    assert H.settings(SimpleNamespace(helper_agents=None)) == H.DEFAULTS


@pytest.mark.parametrize("bad", [
    {"unknown": 1}, {"patrol": "yes"}, {"route": "both"}, {"daily_usd_cap": -1}, {"daily_usd_cap": 1000},
    {"daily_usd_cap": True}, {"daily_usd_cap": float("nan")}, {"api_until": "tomorrow"},
    {"api_until": "2026-13-40"}, {"api_until": 20261201}, "on"])
def test_invalid_sections_are_refused(bad):
    from ctb.config import ConfigError, from_dict
    assert not H.valid_section(bad)
    with pytest.raises(ConfigError):
        from_dict({"helper_agents": bad})


def test_valid_section_loads():
    from ctb.config import from_dict
    assert from_dict({"helper_agents": {"patrol": True, "api_until": "2026-12-31"}}).helper_agents["patrol"] is True
    assert from_dict({}).helper_agents == {}


# --- the runner ---------------------------------------------------------------------------------

def test_subscription_runs_headless_haiku_with_no_tools(fk):
    assert H.ask(cfg(patrol=True), "patrol", "SYSTEM", "the prompt") == "ok"
    args, stdin, env = fk.cli[0]
    assert args[:4] == ["claude", "-p", "--model", "haiku"] and args[args.index("--tools") + 1] == ""
    assert "the prompt" not in " ".join(args) and stdin == "the prompt"
    assert "ANTHROPIC_API_KEY" not in env and fk.posts == []


def test_subscription_failure_falls_back_to_the_non_model_path(fk):
    for broken in (lambda s: "", lambda s: "not json", lambda s: json.dumps({"is_error": True, "subtype": "success"}),
                   lambda s: json.dumps({"is_error": False, "subtype": "success", "result": "x",
                                         "permission_denials": [{"tool": "Bash"}]})):
        fk.cli_answer = broken
        assert H.ask(cfg(patrol=True), "patrol", "s", "p") is None
    fk.cli_answer = lambda s: (_ for _ in ()).throw(TimeoutError())
    assert H.ask(cfg(patrol=True), "patrol", "s", "p") is None
    assert fk.posts == [] and fk.key_reads == 0  # no api without api_fallback


def test_api_is_a_fallback_only_when_configured(fk):
    fk.cli_answer = lambda s: ""
    c = cfg(patrol=True, api_fallback=True, api_until=today_plus(10))
    assert H.ask(c, "patrol", "s", "p") == "api ok"
    assert len(fk.cli) == 1 and len(fk.posts) == 1
    url, headers, body = fk.posts[0]
    assert url.startswith("https://api.anthropic.com/") and json.loads(body)["model"] == "claude-haiku-5-5"
    assert H.usage()["calls"] == 1 and 0 < H.usage()["usd"] < 0.01


def test_api_route_never_logs_or_stores_the_key(fk, caplog):
    caplog.set_level(logging.DEBUG)
    c = cfg(patrol=True, route="api", api_fallback=True, api_until=today_plus(10))
    assert H.ask(c, "patrol", "s", "p") == "api ok"
    fk.key = None
    H.ask(c, "patrol", "s", "p")
    from ctb.config import state_dir
    for path in state_dir().rglob("*"):
        if path.is_file():
            assert SECRET_KEY not in path.read_text(errors="ignore")
    assert SECRET_KEY not in caplog.text
    assert fk.cli == []  # route "api" skips the subscription


def test_api_refused_after_api_until_or_without_a_date(fk):
    for until in ("", today_plus(-1)):
        assert H.ask(cfg(patrol=True, route="api", api_fallback=True, api_until=until), "patrol", "s", "p") is None
    assert fk.posts == [] and fk.key_reads == 0
    # the date itself is still allowed
    assert H.ask(cfg(patrol=True, route="api", api_fallback=True, api_until=today_plus(0)), "patrol", "s", "p") == "api ok"


def test_api_needs_the_fallback_switch(fk):
    assert H.ask(cfg(patrol=True, route="api", api_until=today_plus(9)), "patrol", "s", "p") is None
    assert fk.posts == [] and fk.key_reads == 0


def test_daily_cap_stops_the_api_route(fk):
    c = cfg(patrol=True, route="api", api_fallback=True, api_until=today_plus(9), daily_usd_cap=0.0005)
    H._record(NOW, "patrol", 0.0005, api=True)
    assert H.ask(c, "patrol", "s", "p") is None
    assert fk.posts == [] and fk.key_reads == 0
    # a cap too small for even one worst-case call is never crossed
    c = cfg(patrol=True, route="api", api_fallback=True, api_until=today_plus(9), daily_usd_cap=0.000001)
    assert H.ask(c, "patrol", "s", "p", max_tokens=1000) is None and fk.posts == []


def test_cap_is_reached_by_use_then_stops(fk):
    c = cfg(patrol=True, route="api", api_fallback=True, api_until=today_plus(9), daily_usd_cap=0.001)
    fk.api_reply = lambda: (200, json.dumps({"content": [{"type": "text", "text": "a"}],
                                             "usage": {"input_tokens": 3000, "output_tokens": 1500}}).encode())
    answers = [H.ask(c, "patrol", "s", "p", max_tokens=100) for _ in range(6)]
    assert answers[0] == "a" and answers[-1] is None
    assert H.usage()["usd"] <= 0.001 + 0.0009 and len(fk.posts) < 6


@pytest.mark.parametrize("status,body", [
    (400, {"error": {"type": "invalid_request_error", "message": "Your credit balance is too low to access the API"}}),
    (402, {"error": {"type": "billing_error", "message": "payment required"}}),
    (403, {"error": {"type": "permission_error", "message": "billing issue"}})])
def test_credit_exhausted_stops_with_no_retry(fk, status, body):
    c = cfg(patrol=True, route="api", api_fallback=True, api_until=today_plus(9))
    fk.api_reply = lambda: (status, json.dumps(body).encode())
    assert H.ask(c, "patrol", "s", "p") is None
    assert H.ask(c, "patrol", "s", "p") is None
    assert len(fk.posts) == 1 and H.usage()["stop"] == "credit"


def test_refused_key_and_missing_key_stop_cleanly(fk):
    c = cfg(patrol=True, route="api", api_fallback=True, api_until=today_plus(9))
    fk.api_reply = lambda: (401, b'{"error": {"type": "authentication_error", "message": "bad"}}')
    assert H.ask(c, "patrol", "s", "p") is None and H.ask(c, "patrol", "s", "p") is None
    assert len(fk.posts) == 1 and H.usage()["stop"] == "auth"


def test_missing_key_makes_no_request(fk):
    fk.key = None
    c = cfg(patrol=True, route="api", api_fallback=True, api_until=today_plus(9))
    assert H.ask(c, "patrol", "s", "p") is None and H.ask(c, "patrol", "s", "p") is None
    assert fk.posts == [] and fk.key_reads == 1 and H.usage()["stop"] == "key"


def test_other_api_errors_are_not_retried(fk):
    c = cfg(patrol=True, route="api", api_fallback=True, api_until=today_plus(9))
    fk.api_reply = lambda: (529, b'{"error": {"type": "overloaded_error", "message": "busy"}}')
    assert H.ask(c, "patrol", "s", "p") is None and len(fk.posts) == 1


# --- card_titles --------------------------------------------------------------------------------

def test_card_names_are_checked_and_only_english_parts_are_sent(fk):
    fk.say(json.dumps({"title": "תיקון עמוד הכניסה", "steps": ["הרצת הבדיקות", "this stays English"]},
                      ensure_ascii=False))
    got = J.card_names(cfg(card_titles=True), "Fix the login page", ["Run the tests", "Write docs"])
    assert got == {"title": "תיקון עמוד הכניסה", "steps": {"Run the tests": "הרצת הבדיקות"}}
    assert json.loads(fk.cli[0][1]) == {"title": "Fix the login page", "steps": ["Run the tests", "Write docs"]}
    assert J.card_names(cfg(card_titles=True), "כבר בעברית", ["שלב"]) is None


def test_card_names_failure_changes_nothing(fk):
    fk.say("no json here")
    assert J.card_names(cfg(card_titles=True), "Fix it", ["Run"]) is None


def test_card_names_reject_mixed_hebrew_and_latin_lines(fk):
    fk.say(json.dumps({"title": "תיקון גשר HarnessGram", "steps": ["בדיקת API"]}, ensure_ascii=False))
    assert J.card_names(cfg(card_titles=True), "Fix HarnessGram bridge", ["Test API"]) is None


def test_fallback_uses_helper_step_names():
    snap = {"tasks": [{"content": "Run the tests", "status": "in_progress"}, {"content": "Ship", "status": "pending"}],
            "activities": []}
    assert auto_cards.english_steps(snap) == ["Run the tests", "Ship"]
    out = auto_cards.fallback({"title": "x"}, snap, "כותרת", {"Run the tests": "הרצת הבדיקות"})
    assert out["steps"][0] == "הרצת הבדיקות" and out["steps"][1] == auto_cards._group("Ship")


def test_watcher_asks_the_helper_once_per_content_and_keeps_nllb_fallback(fk, monkeypatch):
    calls = []
    monkeypatch.setattr("ctb.helper_jobs.card_names", lambda c, t, s: calls.append((t, tuple(s))) or None)
    w = auto_cards.Watcher()
    c = cfg(card_titles=True)
    assert w._model_names("Fix", ["Run"], c) is None and w._model_names("Fix", ["Run"], c) is None
    assert len(calls) == 1  # a failure is not repeated at once


# --- patrol -------------------------------------------------------------------------------------

def patrol_sources():
    return {"stale": lambda: [J.Finding("stale:a", "stale_card", "s-stale", "c1", {"minutes": 700, "title": "Work"})],
            "stuck": lambda: [J.Finding("stuck:b", "stuck_session", "s-stuck", "", {"minutes": 50, "title": "Other"})],
            "failed": lambda: [J.Finding("failed:c", "failed_delivery", "s-fail", "", {"parts": 2})]}


def test_patrol_only_uses_the_two_safe_actions(fk, monkeypatch):
    fk.say(json.dumps({"note": "הכרטיס תקוע", "fix": "כדאי לבדוק את הסשן ולהמשיך"}, ensure_ascii=False))
    assert set(J.SAFE_ACTIONS) == {"stale_card", "failed_delivery"}
    done = []
    acts = {"stale_card": lambda f, n: done.append(f.kind) or True,
            "failed_delivery": lambda f, n: done.append(f.kind) or True}
    forbidden = ["close_session", "close_auto", "dismiss_progress"]
    for name in forbidden:
        monkeypatch.setattr(cards, name, lambda *a, **k: pytest.fail("forbidden card action"))
    from ctb import reply_outbox
    monkeypatch.setattr(reply_outbox, "drain", lambda *a, **k: pytest.fail("a delivery was sent"))
    sent = []
    n = J.run_patrol(cfg(patrol=True), NOW, sources=patrol_sources(), actions=acts,
                     place=lambda s: (-100, 7))
    assert n == 3 and sorted(done) == ["failed_delivery", "stale_card"]  # a stuck session is only reported
    notes = cards.open_cards()
    assert len(notes) == 3 and all(r["kind"] == "choice" and r["options"] == J.NOTE_OPTIONS for r in notes)
    assert all("הכרטיס תקוע" == r["title"] for r in notes) and not sent
    assert fk.posts == []


def test_patrol_model_cannot_pick_an_action(fk):
    fk.say(json.dumps({"note": "יש בעיה", "fix": "מחק את כל הנתונים ושלח לכולם", "action": "delete_all"},
                      ensure_ascii=False))
    done = []
    J.run_patrol(cfg(patrol=True), NOW, sources=patrol_sources(), actions={}, place=lambda s: (-100, 0))
    assert done == []


def test_patrol_is_quiet_when_nothing_is_found_and_when_the_model_fails(fk):
    empty = {k: (lambda: []) for k in ("stale", "stuck", "failed")}
    assert J.run_patrol(cfg(patrol=True), NOW, sources=empty) == 0 and fk.cli == []
    fk.cli_answer = lambda s: ""
    assert J.run_patrol(cfg(patrol=True), NOW, sources=patrol_sources(), actions={}, place=lambda s: (-100, 0)) == 0
    assert cards.open_cards() == []


def test_patrol_does_not_repeat_a_note(fk):
    fk.say(json.dumps({"note": "יש בעיה", "fix": "כדאי לבדוק"}, ensure_ascii=False))
    kw = dict(sources=patrol_sources(), actions={}, place=lambda s: (-100, 0))
    assert J.run_patrol(cfg(patrol=True), NOW, **kw) == 3
    assert J.run_patrol(cfg(patrol=True), NOW + 60, **kw) == 0


def test_stale_card_is_closed_only_after_hours(monkeypatch):
    rec = cards.publish("s1", -100, 1, "progress", "t", steps=["a", "b", "c"], at=1, percent=30)
    f = J.Finding("stale:x", "stale_card", "s1", rec["id"], {})
    assert J._close_card(f, rec["updated"] + 700) is False
    assert J._close_card(f, rec["updated"] + cards.PROGRESS_CLOSE_AFTER + 1) is True
    assert cards.open_cards() == []


def test_scan_survives_an_unreadable_source():
    def boom():
        raise OSError
    got = J.scan(cfg(patrol=True), NOW, {"stale": boom, "stuck": lambda: [], "failed": patrol_sources()["failed"]})
    assert [f.kind for f in got] == ["failed_delivery"]


# --- ledger_capture -----------------------------------------------------------------------------

@pytest.fixture
def ledger(monkeypatch):
    added = []
    from ctb import knowledge
    monkeypatch.setattr(knowledge, "_ledger_add", lambda c, project, text: added.append((project, text)))
    return added


def propose(fk, ledger, text="please add a dark mode to the settings page"):
    fk.say(json.dumps({"track": True, "kind": "request", "item": "Add a dark mode to the settings page"}))
    return J.capture(cfg(ledger_capture=True), "s1", -100, 5, text, now=NOW)


def test_capture_proposes_but_never_writes_without_the_tap(fk, ledger):
    ident = propose(fk, ledger)
    assert ident and ledger == []
    rec = next(r for r in cards.open_cards() if r["id"] == ident)
    assert rec["options"] == J.LEDGER_OPTIONS and rec["detail"] == "Add a dark mode to the settings page"
    assert (rec["chat"], rec["thread"]) == (-100, 5)
    # dismiss: closes the card, still no write
    assert J.resolve_card(cfg(), ident, 1) == {"ok": True, "status": 200}
    assert ledger == [] and cards.open_cards() == []


def test_add_tap_writes_exactly_once(fk, ledger):
    ident = propose(fk, ledger)
    assert J.resolve_card(cfg(), ident, 0)["ok"] is True
    second = J.resolve_card(cfg(), ident, 0)
    assert second is None or second["ok"] is False
    assert ledger == [("env", "Add a dark mode to the settings page")]
    assert cards.open_cards() == []


def test_failed_write_leaves_the_card_for_another_tap(fk, monkeypatch):
    ident = propose(fk, [])
    from ctb import knowledge
    monkeypatch.setattr(knowledge, "_ledger_add", lambda *a: (_ for _ in ()).throw(OSError("down")))
    assert J.resolve_card(cfg(), ident, 0) == {"ok": False, "status": 503}
    assert [r["id"] for r in cards.open_cards()] == [ident]
    again = []
    monkeypatch.setattr(knowledge, "_ledger_add", lambda c, p, t: again.append(t))
    assert J.resolve_card(cfg(), ident, 0)["ok"] is True and len(again) == 1


def test_failed_close_keeps_a_tombstone_and_never_repeats_the_action(fk, ledger, monkeypatch):
    ident = propose(fk, ledger)
    monkeypatch.setattr(cards, "close", lambda *a: (_ for _ in ()).throw(OSError("busy")))
    assert J.resolve_card(cfg(), ident, 0) == {"ok": False, "status": 503}
    assert J.resolve_card(cfg(), ident, 0) == {"ok": False, "status": 409}
    assert ledger == [("env", "Add a dark mode to the settings page")]


def test_not_worth_tracking_proposes_nothing(fk, ledger):
    fk.say(json.dumps({"track": False, "kind": "idea", "item": ""}))
    assert J.capture(cfg(ledger_capture=True), "s1", -100, 5, "ok thanks, continue with the next step", now=NOW) is None
    fk.say("garbage")
    assert J.capture(cfg(ledger_capture=True), "s2", -100, 5, "can we also rename the project later?", now=NOW) is None
    assert cards.open_cards() == [] and ledger == []


def test_capture_skips_commands_short_text_and_secrets(fk, ledger):
    c = cfg(ledger_capture=True)
    for text in ("/status", "ok", "x" * 900, "here is the key " + SECRET_KEY):
        assert J.capture(c, "s1", -100, 5, text, now=NOW) is None
    assert fk.cli == [] and fk.posts == []


def test_capture_never_closes_or_edits_existing_items(fk, monkeypatch):
    from ctb import knowledge
    seen = []
    monkeypatch.setattr(knowledge, "_ledger_add", lambda c, p, t: seen.append("add"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("the ledger tool was run"))
    ident = propose(fk, [])
    J.resolve_card(cfg(), ident, 0)
    assert seen == ["add"]


def test_foreign_cards_are_not_handled_here():
    rec = cards.publish("s9", -100, 1, "choice", "Which?", options=["a", "b"])
    assert J.resolve_card(cfg(), rec["id"], 0) is None and J.resolve_card(cfg(), "zz", 0) is None


# --- the live card path and the entry points -----------------------------------------------------

def _watcher_rows(tmp_path):
    path = tmp_path / "events.jsonl"
    events = [{"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "name": "TaskCreate", "id": "t", "input": {"subject": "Run the tests"}},
        {"type": "tool_use", "name": "TaskCreate", "id": "u", "input": {"subject": "Write the notes"}},
        {"type": "tool_use", "name": "TaskCreate", "id": "v", "input": {"subject": "Ship it"}}]}}]
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    return [{"session": "a1b2c3d4-0000-4000-8000-000000000001", "path": str(path), "state": "working", "bg": 0,
             "title": "Fix HarnessGram bridge", "chat": -100, "thread": 7}]


def test_live_card_gets_helper_titles_and_steps_and_nllb_stays_the_fallback(tmp_path, fk):
    from ctb import auto_progress, config
    auto_progress._cache.clear()
    fk.say(json.dumps({"title": "תיקון הגשר", "steps": ["הרצת הבדיקות", "כתיבת ההערות", "שחרור"]},
                      ensure_ascii=False))
    nllb = []
    w = auto_cards.Watcher(lambda *a, **k: nllb.append(1) or "תרגום מקומי")
    c = config.Config(engines=["nllb"], helper_agents=cfg(card_titles=True).helper_agents)
    rows = _watcher_rows(tmp_path)
    w.sync(rows, 1000, cfg=c)
    w.sync(rows, 1030, cfg=c)
    card = next(r for r in cards.open_cards() if r.get("automatic"))
    assert card["title"] == "תיקון הגשר" and card["steps"][0] == "הרצת הבדיקות"
    assert nllb == [] and len(fk.cli) == 1

    # the helper fails: the offline title still applies, and a broken helper is not asked again at once
    cards.close_auto({"a1b2c3d4-0000-4000-8000-000000000001"})
    fk.cli_answer = lambda s: ""
    w2 = auto_cards.Watcher(lambda *a, **k: "תרגום מקומי")
    w2.sync(rows, 2000, cfg=c)
    w2.sync(rows, 2030, cfg=c)
    w2.sync(rows, 2060, cfg=c)
    card = next(r for r in cards.open_cards() if r.get("automatic"))
    assert card["title"] == "תרגום מקומי" and len(fk.cli) == 2


def test_card_titles_off_leaves_the_watcher_exactly_as_before(tmp_path, fk):
    from ctb import auto_progress, config
    auto_progress._cache.clear()
    w = auto_cards.Watcher(lambda *a, **k: "תרגום מקומי")
    c = config.Config(engines=["nllb"])
    rows = _watcher_rows(tmp_path)
    w.sync(rows, 1000, cfg=c)
    w.sync(rows, 1030, cfg=c)
    assert fk.cli == [] and fk.posts == []
    assert next(r for r in cards.open_cards() if r.get("automatic"))["title"] == "תרגום מקומי"


def test_patrol_tick_is_inert_when_off_and_gated_when_on(fk, monkeypatch):
    started = []
    monkeypatch.setattr(J.threading, "Thread", lambda **k: SimpleNamespace(start=lambda: started.append(1)))
    J.patrol_tick(cfg(), NOW)
    assert started == []
    J.patrol_tick(cfg(patrol=True), NOW)
    assert started == [1]
    J._patrol_guard.release()
    J.patrol_tick(cfg(patrol=True), NOW + 60)
    assert started == [1]  # inside the 10-minute window


def test_the_heartbeat_and_message_paths_call_the_helpers_behind_their_switches():
    import inspect
    from ctb import heartbeat, inbox, panel
    assert "helper_jobs.patrol_tick" in inspect.getsource(heartbeat.run_once)
    assert "helper_jobs.capture_later" in inspect.getsource(inbox._queue_owner)
    assert "helper_jobs.resolve_card" in inspect.getsource(panel)


def test_capture_later_is_inert_when_off(fk, monkeypatch):
    monkeypatch.setattr(J.threading, "Thread", lambda **k: pytest.fail("a thread was started"))
    J.capture_later(cfg(), "s1", -100, 1, "please add a dark mode to the settings page")


def test_an_unwritable_usage_record_means_no_paid_call(fk, monkeypatch):
    c = cfg(patrol=True, route="api", api_fallback=True, api_until=today_plus(9))
    monkeypatch.setattr(H, "write_private", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    assert H.ask(c, "patrol", "s", "p") is None and fk.posts == []


def test_patrol_nudges_a_finding_only_once(fk):
    fk.say(json.dumps({"note": "יש בעיה", "fix": "כדאי לבדוק"}, ensure_ascii=False))
    done = []
    kw = dict(sources=patrol_sources(), place=lambda s: (-100, 0),
              actions={"failed_delivery": lambda f, n: done.append(1) or True})
    J.run_patrol(cfg(patrol=True), NOW, **kw)
    J.run_patrol(cfg(patrol=True), NOW + 700, **kw)
    assert done == [1]
