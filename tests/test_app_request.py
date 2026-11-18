"""The owner's "request an app" action: input checks, the command call, exit-code mapping, and the panel endpoint.

The approval command is always a throwaway script written here; no real config, command or network is touched."""
import http.client
import json
import logging
import os
import sys
import threading
import time

import pytest

from conftest import GOOD_TOKEN
from ctb import app_request, panel, panel_text
from ctb.config import Config, ConfigError, from_dict
from test_panel import OWNER, signed

LINK = "https://apps.apple.com/il/app/some-app/id123456789"


@pytest.fixture(autouse=True)
def fresh_gate():
    app_request.GATE.reset()
    yield
    app_request.GATE.reset()


def script(tmp_path, body, name="approve.py"):
    path = tmp_path / name
    path.write_text("import json, os, sys\n" + body)
    return [sys.executable, str(path)]


def cfg_with(cmd):
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he", app_request_command=cmd)


# --- the text -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,want", [
    (LINK, LINK),
    ("  " + LINK + "\n", LINK),
    ("Look at this app! " + LINK + ".", LINK),
    ("https://apps.apple.com/app/id123456789?mt=8", "https://apps.apple.com/app/id123456789?mt=8"),
    ("123456789", "123456789"),
    ("id123456789", "id123456789"),
    ("please approve id987654321 thanks", "id987654321"),
    # Installs are locked, so the App Store is hidden and the owner types the app's name instead (searched by the command).
    ("Google Maps", "Google Maps"),
    ("  וויז \n", "וויז"),
    ("hello   world", "hello world"),
    ("ab", "ab"),
    ("n" * 60, "n" * 60),
    ("AT&T", "AT&T"), ("Ynet: News", "Ynet: News"), ("מוביט", "מוביט"), ("Tick Tock", "Tick Tock"),
])
def test_valid_text_gives_one_link_or_id(text, want):
    assert app_request.extract(text) == want


@pytest.mark.parametrize("text", [
    "", "   ", None, 12345, ["x"], "a", "n" * 61, "-rf", "--help", "www.waze.com", "waze.com/app https://x",
    "tick", "status", "Status", " tick ", "Yes (TV)", "Maps — Transit", "a/b", "Waze\u202e", "12 34", "!!",
    "12345", "1234567890123",
    "http://apps.apple.com/app/id123456789", "https://example.com/app/id123456789",
    "https://apps.apple.com/us/developer/someone/id123", "https://apps.apple.com/us/developer/x",
    "x" * 501, LINK + " " + "y" * 500, "abc\x00" + LINK, "bell\x07 " + LINK,
])
def test_bad_text_is_rejected(text):
    with pytest.raises(app_request.BadInput):
        app_request.extract(text)


def test_exactly_500_characters_is_allowed():
    text = LINK + " " + "z" * (500 - len(LINK) - 1)
    assert len(text) == 500 and app_request.extract(text) == LINK


# --- the answer -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("code,state", [(0, "approved"), (2, "denied"), (3, "review"), (4, "pending"), (1, "error"),
                                        (5, "error"), (-9, "error"), (None, "error")])
def test_exit_code_decides_the_state(code, state):
    out = '{"status": "something else", "app": "A", "bundle": "b.c", "reason": "r", "until": ""}'
    assert app_request.interpret(code, out)["state"] == state


def test_json_fills_the_card_and_until_only_counts_when_approved():
    line = json.dumps({"status": "approved", "app": "Maps", "bundle": "com.x.maps", "reason": "ok",
                       "until": "2026-11-14T14:35:00+02:00"})
    got = app_request.interpret(0, "noise\n" + line + "\n")
    assert got == {"state": "approved", "app": "Maps", "bundle": "com.x.maps", "reason": "ok",
                   "until": "2026-11-14T14:35:00+02:00"}
    assert app_request.interpret(2, line)["until"] == ""


@pytest.mark.parametrize("out", ["", "not json", "{broken", "[1, 2]", '"text"', "{}", '{"app": 5, "reason": ["x"]}',
                                 "{\"until\": \"yesterday-ish\"}", "\x00\x01", "{" + "a" * 100000 + "}"])
def test_malformed_output_never_raises(out):
    got = app_request.interpret(3, out)
    assert got["state"] == "review" and got["until"] == ""
    assert all(isinstance(got[k], str) for k in ("app", "bundle", "reason", "until"))


def test_last_json_line_wins_and_text_is_cleaned():
    out = '{"app": "old"}\n{"app": "New\\u0007 name", "reason": "line\\none"}\n'
    got = app_request.interpret(0, out)
    assert got["app"] == "New name" and got["reason"] == "line one"


@pytest.mark.parametrize("status,delivery,expected", [("approved", False, False), ("approved", True, True),
                                                       ("pending_device", True, False), ("open", False, False),
                                                       ("open", True, True)])
def test_delivery_status_reaches_telegram(status, delivery, expected):
    result = app_request.interpret(0, json.dumps({"status": status, "delivered": delivery,
                                                  "until": "2026-11-14T12:35:00Z"}))
    assert result["state"] == "approved" and result["delivered"] is expected
    he = app_request.telegram_lines(result, "he")[0]
    en = app_request.telegram_lines(result, "en")[0]
    if expected:
        assert "פתוח להתקנה עד" in he and "open for installing until" in en
    else:
        assert he == "אושר - ממתין שהאייפון יקבל את פתיחת החנות"
        assert en == "Approved - waiting for the iPhone to receive the App Store opening"
        assert "פתוח להתקנה" not in he and not any(c.isascii() and c.isalpha() for c in he)


def test_legacy_approval_and_invalid_delivery_values():
    legacy = app_request.interpret(0, '{"status":"approved"}')
    assert "delivered" not in legacy
    assert "פתוח להתקנה" in app_request.telegram_lines(legacy, "he")[0]
    for value in (None, 0, "true"):
        result = app_request.interpret(0, json.dumps({"delivered": value}))
        assert result["delivered"] is False
        assert "פתוח להתקנה" not in app_request.telegram_lines(result, "he")[0]


def test_long_fields_are_cut():
    got = app_request.interpret(2, json.dumps({"app": "a" * 1000, "reason": "r" * 5000}))
    assert len(got["app"]) == 120 and len(got["reason"]) == 400


# --- running the command --------------------------------------------------------------------------------------------

def test_command_gets_the_text_as_one_list_element_without_a_shell(tmp_path):
    cmd = script(tmp_path, "print(json.dumps({'app': json.dumps(sys.argv[1:])}))\nsys.exit(0)\n")
    out = app_request.run(cfg_with(cmd), "id123456789")
    assert json.loads(out["app"]) == ["id123456789"]
    seen = []

    def runner(argv):
        seen.append(argv)
        return 0, "{}"
    app_request.run(cfg_with(cmd), LINK, runner=runner)
    assert isinstance(seen[0], list) and seen[0][:-1] == cmd and seen[0][-1] == LINK


def test_shell_metacharacters_stay_inside_one_argument(tmp_path):
    marker = tmp_path / "pwned"
    cmd = script(tmp_path, "print(json.dumps({'app': str(len(sys.argv))}))\n")
    nasty = "https://apps.apple.com/app/id123456789?x=$(touch%s);touch%s" % (marker, marker)
    got = app_request.run(cfg_with(cmd), nasty)
    assert got["app"] == "2" and not marker.exists()


def test_environment_is_minimal_and_stdin_is_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPER_SECRET_TOKEN", "hunter2")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    cmd = script(tmp_path, "print(json.dumps({'app': ','.join(sorted(os.environ)), 'reason': repr(sys.stdin.read())}))\n")
    got = app_request.run(cfg_with(cmd), "id123456789")
    assert "SUPER_SECRET_TOKEN" not in got["app"] and "TELEGRAM_BOT_TOKEN" not in got["app"]
    assert "PATH" in got["app"] and got["reason"] == "''"


@pytest.mark.parametrize("code,state", [(0, "approved"), (2, "denied"), (3, "review"), (4, "pending"), (1, "error")])
def test_real_process_exit_codes(tmp_path, code, state):
    cmd = script(tmp_path, "print(json.dumps({'status': 'x', 'app': 'App', 'bundle': 'b', 'reason': 'why', "
                           "'until': '2026-11-14T14:35:00+02:00'}))\nsys.exit(%d)\n" % code)
    assert app_request.run(cfg_with(cmd), "id123456789")["state"] == state


def test_a_command_that_prints_nothing_or_crashes_is_an_error(tmp_path):
    assert app_request.run(cfg_with(script(tmp_path, "sys.exit(0)\n")), "id123456789")["until"] == ""
    assert app_request.run(cfg_with(script(tmp_path, "raise SystemExit(7)\n", "boom.py")), "id123456789")["state"] == "error"
    missing = app_request.run(cfg_with([str(tmp_path / "does-not-exist")]), "id123456789")
    assert missing["state"] == "error" and missing["error"] == "start"


def test_timeout_kills_the_command_and_its_children(tmp_path, monkeypatch):
    pidfile = tmp_path / "child.pid"
    cmd = script(tmp_path, "import subprocess, time\n"
                           "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                           "open(%r, 'w').write(str(c.pid))\ntime.sleep(60)\n" % str(pidfile))
    monkeypatch.setattr(app_request, "TIMEOUT", 1)
    started = time.monotonic()
    got = app_request.run(cfg_with(cmd), "id123456789")
    assert got["state"] == "error" and got["error"] == "timeout" and time.monotonic() - started < 10
    pid = int(pidfile.read_text())
    time.sleep(0.3)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_nothing_the_owner_typed_is_logged(tmp_path, caplog):
    cmd = script(tmp_path, "print(json.dumps({'app': 'Calc', 'reason': 'secret reason text'}))\nsys.exit(2)\n")
    with caplog.at_level(logging.DEBUG, logger="ctb.app_request"):
        app_request.request(cfg_with(cmd), "see " + LINK)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "state=denied" in text and "123456789" not in text and "secret reason" not in text and "Calc" not in text


# --- configuration, rate limit, translation -------------------------------------------------------------------------

def test_default_is_hidden_and_only_a_list_of_strings_is_accepted():
    assert not app_request.configured(Config())
    assert Config().app_request_command == []
    assert app_request.configured(from_dict({"app_request_command": ["/usr/bin/python3", "/x/app_request.py"]}))
    for bad in ("python3 script.py", [1, 2], {"a": "b"}):
        with pytest.raises(ConfigError):
            from_dict({"app_request_command": bad})
    assert not app_request.configured(cfg_with(["", "x"])) and not app_request.configured(cfg_with(["a\x00b"]))


def test_unconfigured_request_runs_nothing():
    assert app_request.request(Config(), LINK) == {"ok": False, "error": "unconfigured"}


def test_bad_text_never_calls_the_command():
    called = []
    out = app_request.request(cfg_with(["x"]), "https://example.com/app", runner=lambda argv: called.append(argv))
    assert out["ok"] is False and out["error"] == "bad" and not called


def test_an_app_name_goes_to_the_command_as_one_argument(tmp_path):
    argv = []
    out = app_request.request(cfg_with(["x"]), "  Google   Maps -rf  ", runner=lambda a: (argv.append(a), (2, "{}"))[1])
    assert argv and argv[0] == ["x", "Google Maps -rf"] and len(argv[0]) == 2


def test_gate_allows_one_request_every_ten_seconds_and_one_at_a_time():
    now = [100.0]
    gate = app_request.Gate(10, clock=lambda: now[0])
    assert gate.enter() == 0
    assert gate.enter() > 0          # still running
    gate.leave()
    now[0] += 3
    assert 1 <= gate.enter() <= 7    # too soon
    now[0] += 7.1
    assert gate.enter() == 0


def test_request_releases_the_gate_when_the_command_fails(tmp_path):
    gate = app_request.Gate(0)
    out = app_request.request(cfg_with(["x"]), LINK, gate=gate, runner=lambda a: (_ for _ in ()).throw(OSError("no")))
    assert out["ok"] and out["result"]["error"] == "start"
    assert gate.enter() == 0


class FakeTranslator:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), 0

    def he(self, text):
        self.calls += 1
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


def test_reason_is_translated_waiting_a_little():
    tr = FakeTranslator([None, None, "בגלל הסיבה"])
    assert app_request.hebrew_reason(tr, "because", wait=2) == "בגלל הסיבה" and tr.calls == 3


@pytest.mark.parametrize("answers", [[""], [None]])
def test_untranslatable_reason_falls_back_to_none(answers):
    started = time.monotonic()
    assert app_request.hebrew_reason(FakeTranslator(answers), "because", wait=0.3) is None
    assert time.monotonic() - started < 2
    assert app_request.hebrew_reason(None, "because") is None and app_request.hebrew_reason(FakeTranslator(["x"]), "") is None


def test_translator_errors_do_not_break_the_answer():
    class Broken:
        def he(self, text):
            raise RuntimeError("engine down")
    assert app_request.hebrew_reason(Broken(), "because") is None


def test_telegram_lines_put_english_on_its_own_line():
    res = {"state": "approved", "app": "Maps", "reason": "fine", "reason_he": "", "until": "2026-11-14T12:35:00+00:00"}
    lines = app_request.telegram_lines(res, "he", tz=None)
    assert "Maps" in lines and lines[0].startswith("‏אושר")
    assert all(not (any("a" <= c.lower() <= "z" for c in line.replace("App Store", "")) and any("א" <= c <= "ת" for c in line))
               for line in lines[1:])
    assert app_request.telegram_lines({"state": "review"}, "he") == ["‏הוועדה בודקת, תקבל תשובה."]
    assert app_request.telegram_lines({"state": "pending"}, "en")[0].startswith("Approved, but remote")


def test_bot_command_replies_from_a_thread(tmp_path):
    cmd = script(tmp_path, "print(json.dumps({'app': 'Maps', 'reason': 'safe'}))\nsys.exit(3)\n")
    replies, done = [], threading.Event()
    app_request.telegram_command(cfg_with(cmd), LINK, lambda text: (replies.append(text), done.set()))
    assert done.wait(10) and "Maps" in replies[0]
    replies.clear()
    app_request.telegram_command(cfg_with(cmd), "", replies.append)
    assert "/app" in replies[0]


# --- the panel endpoint ---------------------------------------------------------------------------------------------

@pytest.fixture
def make_server(tmp_path):
    made = []

    def make(cmd, translator=None):
        cfg = cfg_with(cmd)
        srv = panel.PanelServer(("127.0.0.1", 0), cfg)
        srv.app_request_gate = app_request.Gate(10)
        if translator is not None:
            srv.translator = translator
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        made.append(srv)
        return srv
    yield make
    for srv in made:
        srv.shutdown()
        srv.server_close()


def post(srv, body, init="ok", path="/api/app-request"):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=70)
    headers = {}
    if init is not None:
        headers[panel.INIT_HEADER] = signed() if init == "ok" else init
    payload = json.dumps(body, ensure_ascii=False).encode() if not isinstance(body, bytes) else body
    conn.request("POST", path, body=payload, headers=headers)
    r = conn.getresponse()
    data = r.read()
    conn.close()
    return r.status, json.loads(data)


def test_route_is_declared_with_a_body_limit():
    assert panel.ROUTES["/api/app-request"] >= 1600 and panel.ACTIONS["/api/app-request"] == "_app_request"


def test_owner_login_is_required(make_server, tmp_path):
    srv = make_server(script(tmp_path, "print('{}')\n"))
    called = []
    srv.app_request_runner = lambda argv: called.append(argv) or (0, "{}")
    for init in (None, "", "garbage", signed(tamper=True), signed(user_id=OWNER + 1),
                 signed(auth_date=int(time.time()) - panel.DECIDE_MAX_AGE - 30)):
        code, body = post(srv, {"text": LINK}, init=init)
        assert code == 403 and body == {"error": "forbidden"}, init
    assert not called


def test_hidden_when_not_configured(make_server):
    srv = make_server([])
    srv.app_request_runner = lambda argv: pytest.fail("must not run")
    assert post(srv, {"text": LINK})[0] == 404
    code, state = post(srv, {}, path="/api/state")
    assert code == 200 and state["app_request"] is False


def test_state_tells_the_page_the_feature_exists(make_server, tmp_path):
    srv = make_server(script(tmp_path, "print('{}')\n"))
    assert post(srv, {}, path="/api/state")[1]["app_request"] is True


@pytest.mark.parametrize("body", [
    {"text": ""}, {"text": "   "}, {"text": "h"}, {"text": "www.example.com"}, {"text": "x" * 501}, {"text": 5}, {"text": None}, {},
    {"text": LINK, "extra": 1}, {"txt": LINK}, [LINK], b"not json", b"[1]",
])
def test_bad_input_is_a_400_and_never_runs_the_command(make_server, tmp_path, body):
    srv = make_server(script(tmp_path, "print('{}')\n"))
    called = []
    srv.app_request_runner = lambda argv: called.append(argv) or (0, "{}")
    code, out = post(srv, body)
    assert code == 400 and out["error"] == "bad request" and not called
    assert srv.app_request_gate.enter() == 0  # a refused text does not use up the rate limit


@pytest.mark.parametrize("code,state", [(0, "approved"), (2, "denied"), (3, "review"), (4, "pending"), (1, "error")])
def test_every_exit_code_reaches_the_page(make_server, tmp_path, code, state):
    cmd = script(tmp_path, "print(json.dumps({'status': 'x', 'app': 'Maps', 'bundle': 'com.m', 'reason': 'Hello there', "
                           "'until': '2026-11-14T14:35:00+02:00'}))\nsys.exit(%d)\n" % code)
    srv = make_server(cmd, translator=panel_text.Translator(cfg_with(cmd), translate_fn=lambda t: "שלום שם"))
    status, out = post(srv, {"text": "  " + LINK + "  "})
    assert status == 200 and out["ok"] is True
    res = out["result"]
    assert res["state"] == state and res["app"] == "Maps" and res["bundle"] == "com.m"
    assert res["reason"] == "Hello there" and res["reason_he"] == "שלום שם"
    assert res["until"] == ("2026-11-14T14:35:00+02:00" if state == "approved" else "")


def test_the_command_receives_the_link_as_the_last_list_element():
    seen = []
    out = app_request.request(cfg_with(["/bin/echo", "x"]), "see " + LINK, runner=lambda argv: seen.append(argv) or (0, "{}"))
    assert out["ok"] and seen == [["/bin/echo", "x", LINK]]


def test_endpoint_runs_the_real_command_with_one_argument(make_server, tmp_path):
    cmd = script(tmp_path, "print(json.dumps({'app': json.dumps(sys.argv[1:])}))\nsys.exit(0)\n")
    srv = make_server(cmd)
    code, out = post(srv, {"text": "Check this " + LINK})
    assert code == 200 and json.loads(out["result"]["app"]) == [LINK]


def test_english_reason_stays_when_there_is_no_translation(make_server, tmp_path):
    cmd = script(tmp_path, "print(json.dumps({'reason': 'No chat apps'}))\nsys.exit(2)\n")
    srv = make_server(cmd, translator=panel_text.Translator(cfg_with(cmd), translate_fn=None))
    out = post(srv, {"text": LINK})[1]["result"]
    assert out["reason"] == "No chat apps" and out["reason_he"] is None


def test_rate_limit_one_request_every_ten_seconds(make_server, tmp_path):
    cmd = script(tmp_path, "print(json.dumps({'app': 'A'}))\nsys.exit(0)\n")
    srv = make_server(cmd)
    assert post(srv, {"text": LINK})[0] == 200
    code, out = post(srv, {"text": LINK})
    assert code == 429 and out["error"] == "rate" and 1 <= out["retry"] <= 10
    srv.app_request_gate.reset()
    assert post(srv, {"text": LINK})[0] == 200


def test_command_failures_are_an_answer_not_a_server_error(make_server, tmp_path):
    srv = make_server([str(tmp_path / "missing")])
    code, out = post(srv, {"text": LINK})
    assert code == 200 and out["result"]["state"] == "error" and out["result"]["error"] == "start"


def test_endpoint_timeout_is_an_error_answer(make_server, tmp_path, monkeypatch):
    monkeypatch.setattr(app_request, "TIMEOUT", 1)
    srv = make_server(script(tmp_path, "import time\ntime.sleep(30)\n"))
    code, out = post(srv, {"text": LINK})
    assert code == 200 and out["result"]["state"] == "error" and out["result"]["error"] == "timeout"


# --- the owner's /app command in the bot ---------------------------------------------------------------------------

def say_app(rig, text, chat=OWNER, chat_type="private", uid=OWNER):
    from ctb import inbox as I
    m = {"message_id": 9, "date": 2_000_000_000, "text": text, "chat": {"id": chat, "type": chat_type}, "from": {"id": uid}}
    return I.handle({"update_id": 3, "message": m}, rig.cfg, rig.bot, rig.topics, rig.box, None, 1000, rig.team_topics)


def sent_texts(rig, wait=0.0):
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline and not rig.bot.methods("sendMessage"):
        time.sleep(0.05)
    return [c["text"] for c in rig.bot.methods("sendMessage")]


def test_app_command_runs_the_request_in_the_owners_private_chat(tmp_path):
    import dataclasses
    from test_team import Rig
    marker = tmp_path / "argv.json"
    cmd = script(tmp_path, "open(%r, 'w').write(json.dumps(sys.argv[1:]))\n"
                           "print(json.dumps({'app': 'Maps', 'reason': 'safe'}))\nsys.exit(3)\n" % str(marker))
    rig = Rig(tmp_path)
    rig.cfg = dataclasses.replace(rig.cfg, app_request_command=cmd)
    assert say_app(rig, "/app " + LINK) == "app-request"
    texts = sent_texts(rig, wait=10)
    assert texts and "Maps" in texts[0] and json.loads(marker.read_text()) == [LINK]
    assert say_app(rig, "/app") == "app-request" and "/app" in sent_texts(rig)[-1]


def test_app_command_is_hidden_when_not_configured_and_never_runs_elsewhere(tmp_path):
    import dataclasses
    from test_team import Rig, GROUP
    marker = tmp_path / "ran"
    cmd = script(tmp_path, "open(%r, 'w').write('x')\nprint('{}')\n" % str(marker))
    rig = Rig(tmp_path)
    assert say_app(rig, "/app " + LINK) != "app-request"                          # not configured
    rig.cfg = dataclasses.replace(rig.cfg, app_request_command=cmd)
    assert say_app(rig, "/app " + LINK, chat=GROUP, chat_type="supergroup") != "app-request"   # not the private chat
    assert say_app(rig, "/app " + LINK, uid=99) != "app-request"                   # not the owner
    time.sleep(0.3)
    assert not marker.exists()


def test_a_slow_command_still_answers_past_the_five_second_connection_limit(make_server, tmp_path, monkeypatch):
    monkeypatch.setattr(panel, "DEADLINE", 1)  # the same limit, scaled down so the test stays short
    cmd = script(tmp_path, "import time\ntime.sleep(2.5)\nprint(json.dumps({'app': 'Slow'}))\nsys.exit(0)\n")
    srv = make_server(cmd)
    code, out = post(srv, {"text": LINK})
    assert code == 200 and out["result"]["state"] == "approved" and out["result"]["app"] == "Slow"


def test_the_connection_limit_still_applies_to_bad_input(make_server, tmp_path, monkeypatch):
    monkeypatch.setattr(panel, "DEADLINE", 1)
    srv = make_server(script(tmp_path, "print('{}')\n"))
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    conn.putrequest("POST", "/api/app-request")
    conn.putheader(panel.INIT_HEADER, signed())
    conn.putheader("Content-Length", "100")  # promises a body that never comes
    conn.endheaders()
    started = time.monotonic()
    try:
        conn.getresponse().read()
    except (http.client.HTTPException, OSError):
        pass
    assert time.monotonic() - started < 5
    conn.close()


@pytest.mark.parametrize("code", [1, 2, 3, 4, None])
def test_status_never_turns_a_refusal_or_error_into_an_approval(code):
    got = app_request.interpret(code, json.dumps({"status": "open", "delivered": True}))
    assert got["state"] != "approved" or code == 0
    assert "פתוח להתקנה" not in "\n".join(app_request.telegram_lines(got, "he"))


def test_old_output_without_delivery_keeps_its_behaviour():
    got = app_request.interpret(0, json.dumps({"status": "approved", "until": "2026-11-14T12:35:00Z"}))
    assert "delivered" not in got and "פתוח להתקנה עד" in app_request.telegram_lines(got, "he")[0]
