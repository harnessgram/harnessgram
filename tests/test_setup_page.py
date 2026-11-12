"""`harnessgram setup --web`: the guided setup page. The secret, Host and content-type gates, the body cap, the
token never leaving the server, the four steps against a fake bot, the Hebrew page, the idle stop, and the
terminal wizard still printing exactly what it printed before the steps were shared."""
import http.client
import json
import re
import socket
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from conftest import GOOD_TOKEN
from test_setup_doctor import SetupBot
from ctb import config as C, doctor, setup, setup_page
from ctb.telegram import TelegramError

OWNER = 4242
srv_host = ".".join(["127", "0", "0", "1"])  # the loopback address the server binds
BODIES = []  # every answer any test received, checked for the token at the end


class PageBot(SetupBot):
    """getMe like SetupBot; getUpdates returns what a test pushed (a short sleep keeps the wait loop calm)."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.queue, self.lock, self.next_id = [], threading.Lock(), 1

    def call(self, method, params=None, timeout=15):
        if method == "getUpdates":
            self.calls.append((method, params or {}))
            time.sleep(0.01)
            with self.lock:
                out, self.queue = self.queue, []
            return out
        return super().call(method, params, timeout)

    def push(self, text, user_id=OWNER, first="Dana", last="Sample", username="dana_sample"):
        with self.lock:
            self.next_id += 1
            self.queue.append({"update_id": self.next_id, "message": {
                "date": int(time.time()) + 1, "text": text, "chat": {"id": user_id, "type": "private"},
                "from": {"id": user_id, "first_name": first, "last_name": last, "username": username,
                         "is_bot": False}}})


class RejectBot:
    def call(self, method, params=None, timeout=15):
        raise TelegramError("Unauthorized", code=401)


@pytest.fixture
def make(tmp_path):
    made = []

    def build(bot=None, idle_timeout=setup_page.IDLE_SECONDS, page_lang="", **kw):
        bot = bot or PageBot()
        lines = []
        args = dict(install_hooks=False, service=False, start=False, test_message=False, run_doctor=False,
                    settings_file=tmp_path / "claude-settings.json", env={}, bot_factory=lambda token: bot,
                    pair_timeout=5, say=lines.append)
        args.update(kw)
        flow = setup_page.SetupFlow(**args)
        flow.load_saved()
        srv = setup_page.SetupServer(flow, page_lang=page_lang, idle_timeout=idle_timeout)
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        made.append(srv)
        srv.bot, srv.lines = bot, lines
        return srv

    yield build
    for srv in made:
        srv.flow.close()
        srv.shutdown()
        srv.server_close()


def req(srv, method, path, body=None, ctype="application/json", host=None, length=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
    conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    if host != "-":  # "-": no Host header at all
        conn.putheader("Host", f"127.0.0.1:{srv.port}" if host is None else host)
    data = b"" if body is None else body if isinstance(body, bytes) else json.dumps(body).encode()
    if ctype:
        conn.putheader("Content-Type", ctype)
    if method == "POST":
        conn.putheader("Content-Length", str(len(data) if length is None else length))
    conn.endheaders(data or None)
    resp = conn.getresponse()
    out, headers = resp.read(), {k.lower(): v for k, v in resp.getheaders()}
    conn.close()
    BODIES.append(out)
    return resp.status, headers, out


def api(srv, name, body=None):
    status, _, out = req(srv, "POST", f"/{srv.secret}/api/{name}", body if body is not None else {})
    return status, json.loads(out)


def confirm(srv, yes=True):
    """What the page sends: the answer plus the bot and pairing generations and the account it showed."""
    state = api(srv, "state")[1]["state"]
    account = (state["pair"].get("account") or {}).get("id", 0)
    return api(srv, "confirm", {"yes": yes, "bot_gen": state["gens"]["bot"], "pair_gen": state["gens"]["pair"],
                                "account_id": account})


def wait_for(srv, state, seconds=5.0):
    end = time.time() + seconds
    while time.time() < end:
        _, body = api(srv, "state")
        if body["state"]["pair"]["state"] == state:
            return body["state"]
        time.sleep(0.02)
    pytest.fail(f"pairing never reached {state}")


# --- the gates -----------------------------------------------------------------------------------------------

def test_every_request_needs_the_one_time_secret(make):
    srv = make()
    assert len(srv.secret) >= 32
    for path in ("/", "/index.html", "/api/state", f"/{srv.secret[:-1]}/", f"/{srv.secret}x/", f"/x{srv.secret}/"):
        assert req(srv, "GET", path, ctype=None)[0] == 404, path
        assert req(srv, "POST", path.rstrip("/") + "/api/state", {})[0] == 404, path
    assert req(srv, "GET", f"/{srv.secret}/", ctype=None)[0] == 200
    assert req(srv, "GET", f"/{srv.secret}", ctype=None)[0] == 200
    assert req(srv, "GET", f"/{srv.secret}/api/state", ctype=None)[0] == 404  # actions are POST only
    assert req(srv, "POST", f"/{srv.secret}/api/nothing", {})[0] == 404
    assert srv.server_address[0] == "127.0.0.1"
    assert srv.url == f"http://127.0.0.1:{srv.port}/{srv.secret}/"


def test_two_servers_never_share_a_secret_or_a_fixed_port(make):
    a, b = make(), make()
    assert a.secret != b.secret and a.port != b.port


@pytest.mark.parametrize("host", ["evil.example", "evil.example:{port}", "127.0.0.1", "127.0.0.1:1",
                                  "localhost.evil.example:{port}", "0.0.0.0:{port}", "", "-"])
def test_a_foreign_host_header_is_refused(make, host):
    srv = make()
    status, _, out = req(srv, "GET", f"/{srv.secret}/", ctype=None, host=host.format(port=srv.port))
    assert status == 403 and out == b""
    assert req(srv, "POST", f"/{srv.secret}/api/state", {}, host=host.format(port=srv.port))[0] == 403


def test_localhost_is_a_valid_host_too(make):
    srv = make()
    assert req(srv, "GET", f"/{srv.secret}/", ctype=None, host=f"localhost:{srv.port}")[0] == 200


@pytest.mark.parametrize("ctype", [None, "text/plain", "application/x-www-form-urlencoded", "multipart/form-data",
                                   "application/jsonx"])
def test_actions_need_a_json_body(make, ctype):
    srv = make()
    assert req(srv, "POST", f"/{srv.secret}/api/token", {"token": GOOD_TOKEN}, ctype=ctype)[0] == 415
    assert srv.flow.bot is None  # nothing happened


def test_the_body_is_capped_and_must_be_an_object(make):
    srv = make()
    path = f"/{srv.secret}/api/state"
    assert req(srv, "POST", path, b"", length=setup_page.MAX_BODY + 1)[0] == 413
    assert req(srv, "POST", path, b"x" * (setup_page.MAX_BODY + 1))[0] == 413
    assert req(srv, "POST", path, b"{not json")[0] == 400
    assert req(srv, "POST", path, b"[1, 2]")[0] == 400
    assert req(srv, "POST", path, {}, ctype="application/json; charset=utf-8")[0] == 200


def test_the_page_is_locked_down(make):
    srv = make()
    status, headers, out = req(srv, "GET", f"/{srv.secret}/", ctype=None)
    page = out.decode()
    nonce = re.search(r"<script nonce=\"([^\"]+)\"", page).group(1)
    csp = headers["content-security-policy"]
    assert csp.startswith(f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                          "connect-src 'self'")
    assert "frame-ancestors 'none'" in csp and "unsafe" not in csp
    assert headers["cache-control"] == "no-store" and headers["referrer-policy"] == "no-referrer"
    assert not any(h.startswith("access-control-") for h in headers)
    assert page.count(f'nonce="{nonce}"') == 2
    assert "<link" not in page and " src=" not in page and "@import" not in page and "url(" not in page
    assert "innerHTML" not in page and "eval(" not in page and "document.write" not in page
    # a fresh nonce per answer
    assert req(srv, "GET", f"/{srv.secret}/", ctype=None)[1]["content-security-policy"] != csp
    _, api_headers, _ = req(srv, "POST", f"/{srv.secret}/api/state", {})
    assert api_headers["cache-control"] == "no-store" and api_headers["content-type"].startswith("application/json")
    assert not any(h.startswith("access-control-") for h in api_headers)


def closed(sock) -> bool:
    """The server closed the connection: an end of stream, or a reset when it left bytes unread."""
    try:
        return sock.recv(100) == b""
    except ConnectionResetError:
        return True


def test_half_sent_requests_cannot_use_up_the_server(make, monkeypatch):
    """Security review of #211: a local process holding partial requests open hung threads before the secret
    check until the page stopped answering. Now at most MAX_CONNECTIONS are served at once (more are closed at
    once) and each ends after READ_TIMEOUT without data."""
    monkeypatch.setattr(setup_page.SetupHandler, "timeout", 1.5)
    srv = make()
    held = []
    for _ in range(setup_page.MAX_CONNECTIONS):
        s = socket.create_connection(("127.0.0.1", srv.port), timeout=5)
        s.sendall(b"GET /" + srv.secret.encode() + b"/ HTTP/1.1\r\nHost: 127.0.0")  # never finished
        held.append(s)
    time.sleep(0.2)
    extra = socket.create_connection(("127.0.0.1", srv.port), timeout=5)
    extra.sendall(b"GET / HTTP/1.1\r\n")
    started = time.time()
    assert closed(extra) and time.time() - started < 1.0  # closed at once, well before the read timeout
    extra.close()
    for s in held:  # the server drops each idle half request by itself
        assert closed(s)
        s.close()
    assert req(srv, "GET", f"/{srv.secret}/", ctype=None)[0] == 200


def test_the_request_line_read_has_a_timeout():
    assert 0 < setup_page.SetupHandler.timeout <= 10 and 1 <= setup_page.MAX_CONNECTIONS <= 16


# --- the steps -----------------------------------------------------------------------------------------------

def test_the_whole_setup_with_a_fake_bot(make, tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "collect", lambda offline=False, bot=None, listener=None: [
        ("ok", "config is valid"), ("ok", "bot answers")])
    srv = make(install_hooks=True, test_message=True, run_doctor=True)
    status, body = api(srv, "state")
    assert status == 200 and body["state"]["bot"] is None and body["state"]["finished"] is None

    status, body = api(srv, "token", {"token": "  " + GOOD_TOKEN + "\n"})
    assert status == 200 and "error" not in body
    assert body["state"]["bot"] == {"username": "demo_bot", "name": None} and body["state"]["threaded"] is True
    assert not C.config_path().exists()  # nothing is saved before the last step

    status, body = api(srv, "pair")
    pair = body["state"]["pair"]
    assert pair["state"] == "waiting" and re.fullmatch(r"[0-9a-f]{8}", pair["code"])
    assert pair["link"] == f"https://t.me/demo_bot?start={pair['code']}"
    srv.bot.push("/start " + pair["code"])
    found = wait_for(srv, "found")["pair"]["account"]
    assert found == {"name": "Dana Sample", "username": "dana_sample", "id": OWNER}

    status, body = api(srv, "finish", {"lang": "he"})
    assert status == 409 and body["error"] == "order"  # not before the account is confirmed
    status, body = confirm(srv)
    assert body["state"]["paired"] == {"name": "Dana Sample", "username": "dana_sample"}

    status, body = api(srv, "finish", {"lang": "he"})
    assert status == 200 and "error" not in body, body
    done = body["state"]["finished"]
    # hooks confirmed; this test asks for no background service, and the page says so instead of "installed"
    assert [i["key"] for i in done["items"]] == ["r_saved", "r_hooks_ok", "r_service_skipped", "r_test_sent"]
    assert [i.get("warn", False) for i in done["items"]] == [False, False, True, False]
    assert done["doctor"] == [{"level": "ok", "text": "config is valid"}, {"level": "ok", "text": "bot answers"}]
    assert done["code"] == 0 and done["threaded"] is True

    raw = json.loads(C.config_path().read_text())
    assert raw["bot_token"] == GOOD_TOKEN and raw["owner_chat_id"] == OWNER
    assert raw["target_language"] == "he" and raw["ui_language"] == "he"  # the page language, as the wizard asks
    assert (C.config_path().stat().st_mode & 0o777) == 0o600
    hooks = json.loads((tmp_path / "claude-settings.json").read_text())["hooks"]
    assert "Stop" in hooks and "UserPromptSubmit" in hooks
    sent = srv.bot.methods("sendMessage")
    assert sent[0]["chat_id"] == OWNER and sent[0]["text"] == setup.t("setup_connected", lang="he")

    # a second finish changes nothing, and the server stops itself after a short grace
    assert api(srv, "finish", {"lang": "en"})[1]["state"]["finished"] == done
    now = [1000.0]
    srv.clock = lambda: now[0]
    srv.touch()
    assert srv.should_stop() is None
    now[0] += setup_page.DONE_GRACE
    assert srv.should_stop() == "done"


def test_the_token_never_leaves_the_server(make, capsys):
    srv = make(install_hooks=False, test_message=True, run_doctor=False)
    api(srv, "token", {"token": GOOD_TOKEN})
    api(srv, "pair")
    srv.bot.push("/start " + srv.flow.pair_code)
    wait_for(srv, "found")
    confirm(srv)
    api(srv, "finish", {"lang": "en"})
    req(srv, "GET", f"/{srv.secret}/", ctype=None)
    for body in BODIES:
        assert GOOD_TOKEN.encode() not in body and b"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" not in body
    printed = capsys.readouterr()
    assert GOOD_TOKEN not in printed.out + printed.err + "\n".join(srv.lines)


@pytest.mark.parametrize("text,bot,error", [
    ("not-a-token", None, "token_format"),
    ("", None, "token_format"),
    (12345, None, "token_format"),
    (GOOD_TOKEN, RejectBot(), "token_rejected"),
])
def test_a_wrong_token_gets_a_fixed_answer(make, text, bot, error):
    srv = make(bot=bot)
    status, body = api(srv, "token", {"token": text})
    assert status == 400 and body["error"] == error and body["state"]["bot"] is None
    assert set(body) == {"error", "state"} and "Unauthorized" not in json.dumps(body)


def test_no_connection_to_telegram_is_its_own_answer(make):
    bot = PageBot()
    bot.fail["getMe"] = "network error"
    srv = make(bot=bot)
    status, body = api(srv, "token", {"token": GOOD_TOKEN})
    assert status == 400 and body["error"] == "network"


def test_the_token_step_reports_threaded_mode_and_checks_again(make):
    bot = PageBot(topics=False)
    srv = make(bot=bot)
    assert api(srv, "token", {"token": GOOD_TOKEN})[1]["state"]["threaded"] is False
    bot.topics = True
    assert api(srv, "recheck")[1]["state"]["threaded"] is True


def test_a_saved_token_starts_the_token_step_done(make):
    C.save_raw({"bot_token": GOOD_TOKEN, "owner_chat_id": OWNER})
    srv = make()
    state = api(srv, "state")[1]["state"]
    assert state["bot"]["username"] == "demo_bot" and state["kept_id"] == OWNER  # kept, like the wizard


def test_pairing_states_waiting_expired_retry_and_decline(make):
    srv = make(pair_timeout=0.3)
    status, body = api(srv, "pair")
    assert status == 409 and body["error"] == "order"  # no bot yet
    api(srv, "token", {"token": GOOD_TOKEN})
    assert confirm(srv)[0] == 409  # nothing to confirm
    api(srv, "pair")
    first = srv.flow.pair_code
    srv.bot.push("/start wrongcode")  # a stranger's or an old /start never pairs
    expired = wait_for(srv, "expired")
    assert "code" not in expired["pair"] and "link" not in expired["pair"]

    srv.flow.pair_timeout = 5
    api(srv, "pair")
    second = srv.flow.pair_code
    assert second != first and wait_for(srv, "waiting")["pair"]["code"] == second
    srv.bot.push("/start " + second)
    wait_for(srv, "found")
    status, body = confirm(srv, False)
    assert body["state"]["pair"] == {"state": "idle"} and body["state"]["paired"] is None
    assert api(srv, "finish", {"lang": "en"})[1]["error"] == "order"
    assert not C.config_path().exists()


class OtherBot(PageBot):
    def call(self, method, params=None, timeout=15):
        out = super().call(method, params, timeout)
        if method == "getMe":
            out = dict(out, id=2, username="other_bot")
        return out


def test_a_bot_change_resets_pairing_and_a_new_pair_request_works(make):
    """Review of #211: after a bot change the page waited for a pairing that the server had reset."""
    first, second = PageBot(), OtherBot()
    bots = {GOOD_TOKEN: first, "654321:" + "B" * 35: second}
    srv = make(bot_factory=lambda token: bots[token])
    api(srv, "token", {"token": GOOD_TOKEN})
    api(srv, "pair")
    old_code = srv.flow.pair_code
    assert wait_for(srv, "waiting")["pair"]["link"].startswith("https://t.me/demo_bot?start=")
    state = api(srv, "token", {"token": "654321:" + "B" * 35})[1]["state"]
    assert state["bot"]["username"] == "other_bot" and state["pair"] == {"state": "idle"}
    api(srv, "pair")
    waiting = wait_for(srv, "waiting")["pair"]
    assert waiting["code"] != old_code and waiting["link"] == f"https://t.me/other_bot?start={waiting['code']}"
    first.push("/start " + waiting["code"])  # the old bot's wait has ended; only the new bot pairs
    second.push("/start " + waiting["code"])
    assert wait_for(srv, "found")["pair"]["account"]["id"] == OWNER


def test_the_page_resets_the_bot_change_before_drawing_the_answer():
    page = setup_page.PAGE
    submit = page[page.index('f.addEventListener("submit"'):page.index("return f;")]
    assert "act(" not in submit  # act() draws before the flags could be reset
    ok = submit.index("if(!r.error){changing=false;pairAsked=false;}")
    assert ok < submit.index("apply(2,r)")
    # step 3 asks for a pairing again whenever it is idle and none was asked since
    assert 'if(!pairAsked&&working!==3){pairAsked=true;setTimeout(function(){act(3,"pair");},0);}' in page


def test_a_broken_claude_settings_file_is_a_controlled_install_error(make, tmp_path):
    """Review of #211: the installer raises SystemExit for a broken settings file; the request died unanswered."""
    settings = tmp_path / "claude-settings.json"
    settings.write_text("{ not json SECRET-LOOKING-CONTENT")
    srv = make(install_hooks=True, settings_file=settings)
    api(srv, "token", {"token": GOOD_TOKEN})
    api(srv, "pair")
    srv.bot.push("/start " + srv.flow.pair_code)
    wait_for(srv, "found")
    confirm(srv)
    status, body = api(srv, "finish", {"lang": "en"})
    assert status == 400 and body["error"] == "install_failed" and body["state"]["finished"] is None
    assert "SECRET-LOOKING-CONTENT" not in json.dumps(body) and str(settings) not in json.dumps(body)
    assert json.loads(C.config_path().read_text())["owner_chat_id"] == OWNER  # saved before the install
    assert settings.read_text() == "{ not json SECRET-LOOKING-CONTENT"  # untouched
    assert req(srv, "GET", f"/{srv.secret}/", ctype=None)[0] == 200  # still answering
    settings.write_text("{}")
    assert api(srv, "finish", {"lang": "en"})[1]["state"]["finished"]["items"][1] == {"key": "r_hooks_ok"}


def test_a_system_exit_inside_any_action_still_gets_an_answer(make, monkeypatch):
    srv = make()

    def leave(flow, body):
        raise SystemExit("from deep inside")

    monkeypatch.setitem(setup_page.ACTIONS, "state", leave)
    status, body = api(srv, "state")
    assert status == 400 and body["error"] == "internal" and "deep inside" not in json.dumps(body)
    monkeypatch.undo()
    assert api(srv, "state")[0] == 200


def test_only_a_literal_true_or_false_with_what_the_page_showed_is_accepted(make):
    srv = make()
    api(srv, "token", {"token": GOOD_TOKEN})
    api(srv, "pair")
    srv.bot.push("/start " + srv.flow.pair_code)
    shown = wait_for(srv, "found")
    seen = {"bot_gen": shown["gens"]["bot"], "pair_gen": shown["gens"]["pair"],
            "account_id": shown["pair"]["account"]["id"]}
    for body in ({"yes": True}, {"yes": "true", **seen}, {"yes": 1, **seen}, {"yes": True, "bot_gen": 0},
                 {"yes": True, **seen, "account_id": str(OWNER)}, {"yes": True, **seen, "pair_gen": True}):
        status, answer = api(srv, "confirm", body)
        assert status == 400 and answer["error"] == "bad_request", body
    assert srv.flow.account is None and srv.flow.chat_id == 0 and srv.flow.pair_state == "found"
    for wrong in ({"account_id": OWNER + 1}, {"pair_gen": seen["pair_gen"] + 1}, {"bot_gen": seen["bot_gen"] + 1}):
        status, answer = api(srv, "confirm", {"yes": True, **seen, **wrong})
        assert status == 400 and answer["error"] == "stale", wrong
    assert srv.flow.account is None and srv.flow.pair_state == "found"
    assert api(srv, "confirm", {"yes": True, **seen})[0] == 200 and srv.flow.chat_id == OWNER


def test_a_failed_save_is_a_fixed_answer_and_can_be_retried(make, monkeypatch):
    srv = make()
    api(srv, "token", {"token": GOOD_TOKEN})
    api(srv, "pair")
    srv.bot.push("/start " + srv.flow.pair_code)
    wait_for(srv, "found")
    confirm(srv)

    def broken(*a, **k):
        raise setup.SetupError("refusing to write /secret/path", kind="save")

    monkeypatch.setattr(setup, "save_settings", broken)
    status, body = api(srv, "finish", {"lang": "en"})
    assert status == 400 and body["error"] == "save_failed" and "/secret/path" not in json.dumps(body)
    monkeypatch.undo()
    assert api(srv, "finish", {"lang": "en"})[1]["state"]["finished"]["code"] == 0


def test_the_server_stops_after_idle_time(make):
    srv = make(idle_timeout=0.2)
    t = threading.Thread(target=setup_page.watch, args=(srv, 0.02), daemon=True)
    t.start()
    t.join(3)
    assert not t.is_alive() and srv.reason == "idle" and srv.flow.stop.is_set()


def test_a_request_keeps_it_alive_and_a_wrong_secret_does_not(make):
    now = [100.0]
    srv = make(idle_timeout=10)
    srv.clock = lambda: now[0]
    srv.touch()
    now[0] += 8
    req(srv, "GET", "/wrong/", ctype=None)
    req(srv, "GET", f"/{srv.secret}/", ctype=None, host="evil.example")
    assert srv.should_stop() is None
    now[0] += 3
    assert srv.should_stop() == "idle"
    srv.touch()
    assert srv.should_stop() is None


def test_run_web_prints_the_link_when_no_browser_opens_and_stops_when_idle(monkeypatch):
    lines = []
    code = setup_page.run_web(env={"CTB_BOT_TOKEN": GOOD_TOKEN}, say=lines.append, open_browser=lambda url: False,
                              bot_factory=lambda token: PageBot(), idle_timeout=0.3, install_hooks=False)
    out = "\n".join(lines)
    assert code == 1 and "http://127.0.0.1:" in out and "30 minutes without activity" in out
    assert GOOD_TOKEN not in out


def test_run_web_over_ssh_prints_the_link_and_the_port_forward():
    lines, opened = [], []
    code = setup_page.run_web(env={"SSH_CONNECTION": "10.0.0.1 1 10.0.0.2 22"}, say=lines.append,
                              open_browser=opened.append, idle_timeout=0.3)
    out = "\n".join(lines)
    assert code == 1 and not opened and "ssh -L" in out and "http://127.0.0.1:" in out


def test_run_web_end_to_end_through_the_browser_launcher(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_page, "DONE_GRACE", 0.1)
    monkeypatch.setattr(doctor, "collect", lambda offline=False, bot=None, listener=None: [("ok", "all good")])
    bot, lines, seen = PageBot(), [], {}

    def browser(uri):
        launcher = Path(uri[len("file://"):])
        seen["launcher"] = launcher
        seen["mode"] = launcher.stat().st_mode & 0o777
        url = re.search(r'url=(http://[^"]+)"', launcher.read_text()).group(1)
        parts = urlsplit(url)
        assert parts.hostname == srv_host and parts.path.endswith("/")
        port, secret = parts.port, parts.path.strip("/")

        class Srv:  # what req() needs
            pass

        srv = Srv()
        srv.port, srv.secret = int(port), secret

        def drive():
            assert req(srv, "GET", f"/{secret}/", ctype=None)[0] == 200
            seen["after_page"] = launcher.exists()
            api(srv, "token", {"token": GOOD_TOKEN})
            api(srv, "pair")
            for _ in range(200):
                code = api(srv, "state")[1]["state"]["pair"].get("code")
                if code:
                    break
                time.sleep(0.01)
            bot.push("/start " + code)
            wait_for(srv, "found")
            confirm(srv)
            seen["final"] = api(srv, "finish", {"lang": "en"})[1]["state"]["finished"]

        threading.Thread(target=drive, daemon=True).start()
        return True

    code = setup_page.run_web(say=lines.append, open_browser=browser, bot_factory=lambda token: bot, env={},
                              install_hooks=False, test_message=True, settings_file=tmp_path / "s.json",
                              idle_timeout=20)  # a broken driver ends the run instead of hanging the suite
    out = "\n".join(lines)
    assert code == 0, out
    assert seen["mode"] == 0o600 and seen["after_page"] is False and not seen["launcher"].exists()
    assert "http://" not in out  # the browser opened: the link is not printed
    assert seen["final"]["items"][0] == {"key": "r_saved"} and "All set." in out
    assert json.loads(C.config_path().read_text())["owner_chat_id"] == OWNER


# --- preferences ---------------------------------------------------------------------------------------------

def ready(srv):
    """Token checked and account confirmed: only the last step is left."""
    api(srv, "token", {"token": GOOD_TOKEN})
    api(srv, "pair")
    srv.bot.push("/start " + srv.flow.pair_code)
    wait_for(srv, "found")
    confirm(srv)


def test_the_page_gets_the_bridge_languages_and_no_saved_choices_on_a_first_run(make):
    from ctb import lang as L
    state = api(make(), "state")[1]["state"]
    assert state["languages"] == [[code, name] for code, name in L.LANG_NAMES.items()]
    assert state["prefs"] == {"activity": "normal"}  # the page fills the rest from its language


def test_choices_an_earlier_setup_saved_are_preselected(make):
    C.save_raw({"bot_token": GOOD_TOKEN, "owner_chat_id": OWNER, "target_language": "ru", "translate": True,
                "ui_language": "en", "context_footer": False})
    C.set_activity_level("full")
    assert api(make(), "state")[1]["state"]["prefs"] == {
        "target_language": "ru", "translate": True, "ui_language": "en", "context_footer": False, "activity": "full"}


def test_the_language_option_preselects_the_reply_language(make):
    assert api(make(language="es"), "state")[1]["state"]["prefs"]["target_language"] == "es"


def test_the_page_defaults_save_as_one_tap(make):
    """What the page sends when nothing is touched, for a Hebrew page: Hebrew replies translated, Hebrew menus."""
    srv = make()
    ready(srv)
    prefs = {"target_language": "he", "translate": True, "ui_language": "he", "context_footer": True,
             "activity": "normal"}
    status, body = api(srv, "finish", {"lang": "he", "prefs": prefs})
    assert status == 200 and body["state"]["finished"]["code"] == 0, body
    raw = json.loads(C.config_path().read_text())
    assert (raw["target_language"], raw["translate"], raw["ui_language"], raw["context_footer"]) == ("he", True, "he", True)
    assert C.activity_level() == "normal"


@pytest.mark.parametrize("prefs,expect,activity", [
    ({"target_language": "ru"}, {"target_language": "ru", "translate": False, "ui_language": "en"}, "normal"),
    ({"target_language": "en", "translate": True}, {"target_language": "en", "translate": True}, "normal"),
    ({"target_language": "he", "ui_language": "en"}, {"target_language": "he", "ui_language": "en"}, "normal"),
    ({"context_footer": False}, {"context_footer": False}, "normal"),
    ({"activity": "quiet"}, {}, "quiet"),
    ({"activity": "full"}, {}, "full"),
])
def test_each_choice_is_saved(make, prefs, expect, activity):
    srv = make()
    ready(srv)
    assert api(srv, "finish", {"lang": "en", "prefs": prefs})[0] == 200
    raw = json.loads(C.config_path().read_text())
    for key, value in expect.items():
        assert raw[key] == value, key
    assert C.activity_level() == activity
    C.from_dict(raw)  # a valid config


@pytest.mark.parametrize("prefs", [
    {"target_language": "xx"}, {"target_language": "HE"}, {"target_language": ""}, {"target_language": 1},
    {"translate": "true"}, {"translate": 1}, {"translate": None},
    {"ui_language": "ru"}, {"ui_language": "he "},
    {"context_footer": "yes"}, {"activity": "loud"}, {"activity": None},
    {"bot_token": "123:abc"}, {"owner_chat_id": 1}, {"engines": ["google"]},  # never through preferences
    ["he"], "he",
])
def test_values_outside_the_allowed_sets_are_refused(make, prefs):
    srv = make()
    ready(srv)
    status, body = api(srv, "finish", {"lang": "en", "prefs": prefs})
    assert status == 400 and body["error"] == "bad_request" and body["state"]["finished"] is None
    assert not C.config_path().exists() and C.activity_level() == "normal"  # nothing saved


def test_the_allowed_sets_are_the_bridges_own():
    from ctb import lang as L
    assert setup.PREFERENCES["target_language"] == tuple(L.LANG_NAMES)
    assert setup.PREFERENCES["activity"] == C.ACTIVITY_LEVELS
    for code in setup.PREFERENCES["ui_language"]:
        C.from_dict({"bot_token": GOOD_TOKEN, "owner_chat_id": OWNER, "ui_language": code})
    with pytest.raises(C.ConfigError):
        C.from_dict({"bot_token": GOOD_TOKEN, "owner_chat_id": OWNER, "ui_language": "ru"})


def test_the_last_step_offers_the_choices_with_native_controls():
    page = setup_page.PAGE
    step4 = page[page.index("function step4()"):page.index("function results(")]
    assert step4.index("prefsBox()") < step4.index('act(4,"finish",{lang:lang,prefs:prefsBody()})')
    box = page[page.index("function prefsBox()"):page.index("function step4()")]
    for key in ("target_language", "translate", "ui_language", "context_footer", "activity"):
        assert f'"{key}"' in box, key
    assert 'el("select")' in page and 'c.type="checkbox"' in page and 'c.setAttribute("role","switch")' in page
    assert "Intl.DisplayNames" in page  # language names in the page language
    # defaults: page language for replies and menus, translation on unless the replies are English
    assert 'if(k==="target_language")return lang;' in page and 'if(k==="ui_language")return lang;' in page
    assert 'if(k==="translate")return pref("target_language")!=="en";' in page
    assert 'if(k==="context_footer")return true;' in page and 'return "normal";' in page
    for lang in ("en", "he"):
        for key in ("prefs_title", "p_reply", "p_translate", "p_translate_hint", "p_menu", "p_footer", "p_activity",
                    "act_quiet", "act_normal", "act_full"):
            assert setup_page.STRINGS[lang][key].strip(), (lang, key)


OTHER = "654321:" + "B" * 35


def settled_flow(make, **kw):
    first, second = PageBot(), OtherBot()
    bots = {GOOD_TOKEN: first, OTHER: second}
    srv = make(bot_factory=lambda token: bots[token], **kw)
    srv.bot = first
    ready(srv)
    return srv, first, second


def assert_still_bot_a(srv, first):
    flow = srv.flow
    assert flow.bot is first and flow._token == GOOD_TOKEN and flow.chat_id == OWNER
    assert flow.me["id"] == 1 and flow.account["chat_id"] == OWNER and flow.pair_state == "done"


def test_a_bot_change_while_the_finish_runs_is_refused(make, monkeypatch):
    """Security review of #211: switching to bot B while bot A installed was accepted; the finish saved A and
    reported success while the page pointed at B with no paired account."""
    srv, first, second = settled_flow(make, install_hooks=True)
    started, release = threading.Event(), threading.Event()

    def slow_install(*a, **k):
        started.set()
        assert release.wait(10)
        return GOOD_REPORT

    monkeypatch.setattr(setup, "install_step", slow_install)
    answer = {}
    runner = threading.Thread(target=lambda: answer.update(finish=api(srv, "finish", {"lang": "en"})), daemon=True)
    runner.start()
    assert started.wait(5)
    status, body = api(srv, "token", {"token": OTHER})
    assert status == 409 and body["error"] == "order" and body["state"]["busy"] is True
    assert body["state"]["bot"]["username"] == "demo_bot" and body["state"]["paired"]["name"] == "Dana Sample"
    assert api(srv, "pair")[1]["error"] == "order" and confirm(srv, False)[1]["error"] == "order"
    assert not second.methods("getMe") and not [c for c in second.calls if c[0] == "getMe"]  # B never checked
    assert_still_bot_a(srv, first)
    release.set()
    runner.join(10)
    status, body = answer["finish"]
    assert status == 200 and body["state"]["finished"]["code"] == 0
    assert body["state"]["bot"]["username"] == "demo_bot"
    assert json.loads(C.config_path().read_text())["bot_token"] == GOOD_TOKEN
    assert_still_bot_a(srv, first)


def test_a_bot_change_that_raced_the_finish_start_is_refused(make, monkeypatch):
    """The token was being checked with Telegram when the finish began: the check's result is not applied."""
    srv, first, second = settled_flow(make)
    real = setup.check_bot

    def check_then_finish(bot):
        me = real(bot)
        if bot is second:
            with srv.flow.lock:
                srv.flow.busy = True  # the finish took the lock first
        return me

    monkeypatch.setattr(setup, "check_bot", check_then_finish)
    status, body = api(srv, "token", {"token": OTHER})
    assert status == 409 and body["error"] == "order"
    assert_still_bot_a(srv, first)


def test_no_bot_or_pairing_change_after_the_setup_finished(make):
    srv, first, second = settled_flow(make)
    done = api(srv, "finish", {"lang": "en"})[1]["state"]["finished"]
    assert done["code"] == 0
    for name, call in (("token", lambda: api(srv, "token", {"token": OTHER})), ("pair", lambda: api(srv, "pair")),
                       ("confirm", lambda: confirm(srv)), ("confirm", lambda: confirm(srv, False))):
        status, answer = call()
        assert status == 409 and answer["error"] == "order", name
        assert answer["state"]["finished"] == done and answer["state"]["bot"]["username"] == "demo_bot"
    assert_still_bot_a(srv, first)
    assert json.loads(C.config_path().read_text())["bot_token"] == GOOD_TOKEN


def test_the_page_hides_the_bot_change_while_finishing_or_finished():
    page = setup_page.PAGE
    line = page[page.index('else if(s2==="done")'):page.index('ol.appendChild(stepItem(2,')]
    assert '(!fin&&!st.busy&&working!==4)?add(el("div","row"),button(t("s2_change")' in line


# --- stale results: one gate for every network result -----------------------------------------------------

THIRD = "111222:" + "C" * 35


class GateBot(PageBot):
    """A fake bot whose next call of an armed method stops until released and then answers with what it would
    have answered when it started (a slow Telegram)."""

    def __init__(self, bot_id=1, username="demo_bot", topics=True):
        super().__init__(topics=topics, username=username)
        self.bot_id, self.armed = bot_id, set()
        self.entered, self.release = threading.Event(), threading.Event()

    def arm(self, method):
        self.armed.add(method)
        self.entered.clear()
        self.release.clear()

    def _hold(self, method):
        if method in self.armed:
            self.armed.discard(method)
            self.entered.set()
            assert self.release.wait(10)

    def call(self, method, params=None, timeout=15):
        if method == "getMe":
            self.calls.append((method, {}))
            answer = {"id": self.bot_id, "username": self.username, "has_topics_enabled": self.topics}
            self._hold(method)
            return answer
        if method == "getUpdates":
            self._hold(method)
        return super().call(method, params, timeout)


def background(fn):
    out = {}
    t = threading.Thread(target=lambda: out.update(answer=fn()), daemon=True)
    t.start()
    return t, out


def three_bots(make, saved=False, **kw):
    a, b, c = GateBot(1, "demo_bot"), GateBot(2, "other_bot", topics=False), GateBot(3, "third_bot")
    if saved:  # an earlier setup: bot A and the owner are already saved, so no pairing is needed
        C.save_raw({"bot_token": GOOD_TOKEN, "owner_chat_id": OWNER})
    bots = {GOOD_TOKEN: a, OTHER: b, THIRD: c}
    srv = make(bot_factory=lambda token: bots[token], **kw)
    srv.bot = a
    return srv, a, b, c


def test_a_slow_recheck_of_the_old_bot_never_lands_on_the_new_one(make):
    """Verification review of #211: a recheck of bot A returned after the switch to bot B and the finish; it
    wrote A's details over B's, so the page showed A and the finish reported Threaded Mode on for B."""
    srv, a, b, _ = three_bots(make, saved=True)
    assert srv.flow.bot is a
    a.arm("getMe")
    t, out = background(lambda: api(srv, "recheck"))
    assert a.entered.wait(5)
    assert api(srv, "token", {"token": OTHER})[0] == 200  # B: Threaded Mode off
    status, body = api(srv, "finish", {"lang": "en"})
    assert status == 200 and body["state"]["finished"]["threaded"] is False and body["state"]["finished"]["code"] == 1
    a.release.set()
    t.join(5)
    assert out["answer"][0] == 409 and out["answer"][1]["error"] == "order"
    flow = srv.flow
    assert flow.bot is b and flow.me["id"] == 2 and flow.result["threaded"] is False
    assert api(srv, "state")[1]["state"]["bot"]["username"] == "other_bot"
    assert json.loads(C.config_path().read_text())["bot_token"] == OTHER


# after a bot change ----------------------------------------------------------------------------------------

def test_a_token_check_that_returns_after_another_bot_won_is_dropped(make):
    srv, a, b, c = three_bots(make)
    b.arm("getMe")
    t, out = background(lambda: api(srv, "token", {"token": OTHER}))
    assert b.entered.wait(5)
    assert api(srv, "token", {"token": THIRD})[0] == 200
    b.release.set()
    t.join(5)
    assert out["answer"][0] == 400 and out["answer"][1]["error"] == "stale"
    assert srv.flow.bot is c and srv.flow.me["id"] == 3 and srv.flow._token == THIRD


def test_a_recheck_that_returns_after_a_bot_change_is_dropped(make):
    srv, a, b, _ = three_bots(make)
    api(srv, "token", {"token": GOOD_TOKEN})
    a.arm("getMe")
    t, out = background(lambda: api(srv, "recheck"))
    assert a.entered.wait(5)
    api(srv, "token", {"token": OTHER})
    a.release.set()
    t.join(5)
    assert out["answer"][0] == 400 and out["answer"][1]["error"] == "stale"
    assert srv.flow.me["id"] == 2 and srv.flow.bot is b


def test_a_pairing_that_returns_after_a_bot_change_is_dropped(make):
    srv, a, b, _ = three_bots(make)
    api(srv, "token", {"token": GOOD_TOKEN})
    a.arm("getUpdates")
    api(srv, "pair")
    assert a.entered.wait(5)
    a.push("/start " + srv.flow.pair_code)
    api(srv, "token", {"token": OTHER})  # another bot: pairing starts over
    a.release.set()
    time.sleep(0.3)
    state = api(srv, "state")[1]["state"]
    assert state["pair"] == {"state": "idle"} and srv.flow.found is None and srv.flow.chat_id == 0


def test_the_same_bots_token_pasted_again_is_no_bot_change(make):
    """Results about the same bot stay valid and a waiting pairing keeps waiting: a second poll of the same bot
    would clash with it (Telegram allows one getUpdates at a time)."""
    srv, a, _, _ = three_bots(make)
    api(srv, "token", {"token": GOOD_TOKEN})
    gen = srv.flow.bot_gen
    api(srv, "pair")
    code = srv.flow.pair_code
    assert api(srv, "token", {"token": GOOD_TOKEN})[0] == 200
    assert srv.flow.bot_gen == gen and srv.flow.pair_state == "waiting" and srv.flow.pair_code == code
    a.push("/start " + code)
    assert wait_for(srv, "found")["pair"]["account"]["id"] == OWNER


def test_a_confirmation_after_a_bot_change_is_refused(make):
    srv, a, b, _ = three_bots(make)
    api(srv, "token", {"token": GOOD_TOKEN})
    api(srv, "pair")
    a.push("/start " + srv.flow.pair_code)
    wait_for(srv, "found")
    api(srv, "token", {"token": OTHER})
    status, body = confirm(srv)
    assert status == 409 and srv.flow.account is None and srv.flow.chat_id == 0


# after the finish started ----------------------------------------------------------------------------------

def test_a_token_check_that_returns_after_the_finish_started_is_dropped(make):
    srv, a, b, _ = three_bots(make, saved=True)
    b.arm("getMe")
    t, out = background(lambda: api(srv, "token", {"token": OTHER}))
    assert b.entered.wait(5)
    assert api(srv, "finish", {"lang": "en"})[1]["state"]["finished"]["code"] == 0  # with bot A
    b.release.set()
    t.join(5)
    assert out["answer"][0] == 409 and out["answer"][1]["error"] == "order"
    assert srv.flow.bot is a and srv.flow.me["id"] == 1
    assert json.loads(C.config_path().read_text())["bot_token"] == GOOD_TOKEN


def test_a_recheck_that_returns_after_the_finish_started_is_dropped(make):
    srv, a, _, _ = three_bots(make, saved=True)
    a.topics = False
    a.arm("getMe")
    t, out = background(lambda: api(srv, "recheck"))  # will answer "Threaded Mode off", from before
    assert a.entered.wait(5)
    a.topics = True
    done = api(srv, "finish", {"lang": "en"})[1]["state"]["finished"]
    assert done["threaded"] is True and done["code"] == 0
    a.release.set()
    t.join(5)
    assert out["answer"][1]["error"] == "order"
    assert srv.flow.me["has_topics_enabled"] is True and srv.flow.result["threaded"] is True


def test_a_pairing_that_returns_while_the_flow_is_settled_is_dropped(make):
    srv, a, _, _ = three_bots(make)
    api(srv, "token", {"token": GOOD_TOKEN})
    a.arm("getUpdates")
    api(srv, "pair")
    assert a.entered.wait(5)
    a.push("/start " + srv.flow.pair_code)
    with srv.flow.lock:
        srv.flow.busy = True  # a finish is running
    a.release.set()
    time.sleep(0.3)
    assert srv.flow.found is None and srv.flow.pair_state == "waiting"
    with srv.flow.lock:
        srv.flow.busy = False


def test_the_commit_gate(make):
    flow = make().flow
    wrote = []

    def write():
        wrote.append(1)

    step = flow._ticket()
    assert flow._commit(step, write) and wrote == [1]
    with flow.lock:
        flow.bot_gen += 1  # a bot change
    assert not flow._commit(step, write) and wrote == [1]
    step = flow._ticket()
    own = flow._ticket(finish=True)
    assert not flow._commit(own, write)  # a finish ticket only while that finish runs
    flow.busy = True
    assert not flow._commit(step, write) and flow._commit(own, write) and wrote == [1, 1]
    flow.result = {"code": 0}
    assert not flow._commit(own, write) and not flow._commit(step, write)
    flow.busy, flow.result = False, None
    assert flow._commit(step, lambda: False) is False  # the write's own check may still refuse


def test_every_state_write_goes_through_the_gate_or_a_settled_check():
    """Structural guard: a flow attribute is only assigned in __init__, in the commit callbacks, in the two
    helpers they call, or in the documented direct actions (each under the lock, after a settled check)."""
    import ast
    import inspect
    import textwrap
    tree = ast.parse(textwrap.dedent(inspect.getsource(setup_page.SetupFlow)))
    state = {"_token", "bot", "me", "new_token", "bot_gen", "chat_id", "account", "pair_state", "pair_code", "found",
             "pair_gen", "raw", "before", "result", "busy"}
    allowed = {
        "__init__": state,
        "_set_bot": {"_token", "bot", "me", "new_token", "bot_gen"},
        "_reset_pairing": {"pair_gen", "pair_state", "pair_code", "found", "account", "chat_id"},
        "write": {"me", "found", "pair_state", "account", "chat_id"},  # recheck, pairing, confirm (via _commit)
        "keep_saved": {"raw", "before"}, "keep_me": {"me"}, "keep_result": {"result"},  # the finish (via _commit)
        "start_pairing": {"pair_gen", "pair_state", "pair_code", "found"},  # under the lock, after _settled()
        "finish": {"busy"},  # sets and clears the settled flag itself
    }
    seen = []

    def walk(node, func):
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, (ast.FunctionDef, ast.Lambda.__mro__[0])) and hasattr(child, "name") else func
            if isinstance(child, (ast.Assign, ast.AugAssign)):
                targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                for target in targets:
                    for el in (target.elts if isinstance(target, ast.Tuple) else [target]):
                        if isinstance(el, ast.Attribute) and isinstance(el.value, ast.Name) and el.value.id == "self" \
                                and el.attr in state:
                            seen.append((name, el.attr))
            walk(child, name)

    walk(tree, "")
    assert seen
    wrong = [(f, a) for f, a in seen if a not in allowed.get(f, set())]
    assert not wrong, wrong
    # every callback named write/keep_* is handed to _commit
    source = inspect.getsource(setup_page.SetupFlow)
    for name in ("keep_saved", "keep_me", "keep_result"):
        assert f"self._commit(ticket, {name})" in source
    assert source.count("self._commit(ticket, write)") == 2 and "self._commit((bot_gen, False), write)" in source


def test_the_page_hides_check_again_once_finishing_or_finished():
    page = setup_page.PAGE
    box = page[page.index("function threadedBox()"):page.index("function step3()")]
    assert 'if(!st.finished&&!st.busy&&working!==4){' in box


# --- identity: what the person confirmed, and what is saved --------------------------------------------------

STRANGER = 9999


def test_a_late_confirmation_from_another_tab_never_approves_another_account(make):
    """Verification review of #211: a delayed "yes" for the account shown under bot A approved the account found
    later under bot B (confirming 4242 saved 9999)."""
    srv, a, b, _ = three_bots(make)
    api(srv, "token", {"token": GOOD_TOKEN})
    api(srv, "pair")
    a.push("/start " + srv.flow.pair_code)
    tab1 = wait_for(srv, "found")
    assert tab1["pair"]["account"]["id"] == OWNER
    late = {"yes": True, "bot_gen": tab1["gens"]["bot"], "pair_gen": tab1["gens"]["pair"],
            "account_id": tab1["pair"]["account"]["id"]}
    api(srv, "token", {"token": OTHER})  # the other tab moves to bot B
    api(srv, "pair")
    b.push("/start " + srv.flow.pair_code, user_id=STRANGER, first="Someone", last="Else", username="someone")
    assert wait_for(srv, "found")["pair"]["account"]["id"] == STRANGER
    status, body = api(srv, "confirm", late)
    assert status == 400 and body["error"] == "stale"
    assert srv.flow.account is None and srv.flow.chat_id == 0 and srv.flow.pair_state == "found"
    assert api(srv, "finish", {"lang": "en"})[1]["error"] == "order" and not C.config_path().exists()


def test_the_page_sends_what_it_showed_with_the_confirmation():
    page = setup_page.PAGE
    step3 = page[page.index("function step3()"):page.index("function step4()")]
    assert "var seen={bot_gen:st.gens.bot,pair_gen:st.gens.pair,account_id:p.account.id};" in step3
    assert step3.count("bot_gen:seen.bot_gen,pair_gen:seen.pair_gen,account_id:seen.account_id") == 2


def other_setup_saves(token=OTHER, owner=STRANGER):
    C.save_raw({"bot_token": token, "owner_chat_id": owner}, replace_owner=True)


@pytest.fixture
def installs(monkeypatch):
    calls = []
    monkeypatch.setattr(setup, "install_step", lambda *a, **k: calls.append(1) or GOOD_REPORT)
    return calls


def test_a_save_over_another_setups_bot_and_owner_is_a_conflict(make, installs):
    """Verification review of #211: the page loaded bot A / owner 4242, another setup saved bot B / owner 9999,
    and the page's save (only its own changes merged) kept B / 9999 yet reported success with A."""
    srv, a, _, _ = three_bots(make, saved=True, install_hooks=True)
    other_setup_saves()
    status, body = api(srv, "finish", {"lang": "en"})
    assert status == 400 and body["error"] == "conflict" and body["state"]["finished"] is None
    raw = json.loads(C.config_path().read_text())
    assert raw["bot_token"] == OTHER and raw["owner_chat_id"] == STRANGER  # the other setup's save stands
    assert installs == [] and not a.methods("sendMessage")


def test_a_config_created_elsewhere_after_the_page_opened_is_a_conflict(make, installs):
    srv = make(install_hooks=True)
    ready(srv)
    other_setup_saves()
    assert api(srv, "finish", {"lang": "en"})[1]["error"] == "conflict"
    assert json.loads(C.config_path().read_text())["owner_chat_id"] == STRANGER and installs == []


def test_a_change_right_after_the_save_stops_before_installing(make, installs, monkeypatch):
    srv = make(install_hooks=True)
    ready(srv)
    real = setup.save_settings

    def save_then_overwritten(*a, **k):
        path = real(*a, **k)
        other_setup_saves()
        return path

    monkeypatch.setattr(setup, "save_settings", save_then_overwritten)
    assert api(srv, "finish", {"lang": "en"})[1]["error"] == "conflict"
    assert installs == [] and srv.flow.result is None


def test_a_change_during_the_install_is_never_reported_as_success(make, monkeypatch):
    srv = make(install_hooks=True)
    ready(srv)
    monkeypatch.setattr(setup, "install_step", lambda *a, **k: other_setup_saves() or GOOD_REPORT)
    status, body = api(srv, "finish", {"lang": "en"})
    assert status == 400 and body["error"] == "conflict" and srv.flow.result is None


def test_the_terminal_wizard_does_not_guard_identity_and_the_page_does():
    import inspect
    assert "guard_identity=True" in inspect.getsource(setup_page.SetupFlow._finish)
    assert "guard_identity" not in inspect.getsource(setup.run)  # its behaviour is unchanged
    assert setup.IDENTITY == ("bot_token", "bot_token_command", "owner_chat_id")


def test_the_terminal_wizard_applies_no_preferences():
    raw = {}
    setup.apply_settings(raw, OWNER, GOOD_TOKEN, None)
    assert raw == {"owner_chat_id": OWNER, "bot_token": GOOD_TOKEN, "target_language": "en", "ui_language": "en",
                   "translate": False}


# --- the page ------------------------------------------------------------------------------------------------

def test_the_strings_table_has_every_key_the_page_uses():
    he, en = setup_page.STRINGS["he"], setup_page.STRINGS["en"]
    assert he.keys() == en.keys()
    page = setup_page.PAGE
    used = set(re.findall(r'\bt\("([a-z0-9_]+)"', page)) | set(re.findall(r':"(err_[a-z]+)"', page))
    used |= set(re.findall(r'"key": "([a-z0-9_]+)"', Path(setup_page.__file__).read_text()))
    assert used and not used - he.keys(), used - he.keys()
    unused = {k for k in he if f'"{k}"' not in page and f'"key": "{k}"' not in Path(setup_page.__file__).read_text()}
    assert not unused, unused
    assert all(v.strip() for v in list(he.values()) + list(en.values()))
    assert set(setup_page.ERRORS) <= set(re.findall(r'([a-z_]+):"err_', page))


def test_hebrew_strings_never_mix_english_outside_an_isolate():
    for key, value in setup_page.STRINGS["he"].items():
        bare = re.sub("⁨[^⁩]*⁩", "", value)
        assert not re.search(r"[A-Za-z]", bare), key
        for name in re.findall("⁨([^⁩]*)⁩", value):
            assert len(name.split()) <= 3, key


def test_the_page_uses_logical_css_only():
    page = setup_page.PAGE
    css = page[page.index("<style"):page.index("</style>")]
    assert not re.search(r"\b(left|right)\b", css)
    assert not re.search(r"\.style\.", page)
    assert not re.search(r"\b(margin|padding|border|inset)-(left|right)\b", page)
    assert "text-align:start" in css and "margin-inline:auto" in css and "padding-inline-start" in css


def test_the_hebrew_page_is_rtl_and_the_english_page_ltr():
    he = setup_page.render_page("he", "N").decode()
    assert '<html lang="he" dir="rtl">' in he and 'FORCED="he"' in he and "<title>הגדרת" in he
    en = setup_page.render_page("en", "N").decode()
    assert '<html lang="en" dir="ltr">' in en and 'FORCED="en"' in en
    auto = setup_page.render_page("ru", "N").decode()  # not a page language: the browser's language decides
    assert 'FORCED=""' in auto and "navigator.language" in auto
    assert 'h.dir=(l==="he")?"rtl":"ltr"' in auto  # the root dir follows the chosen language
    assert "prefers-color-scheme:dark" in he and "</script>" not in he[he.index("var ALL="):he.index("var CODES")]


def test_an_empty_field_takes_the_page_direction():
    page = setup_page.PAGE
    assert 'dir="auto"' not in page and "dir='auto'" not in page
    assert ('function syncDir(el){if(el.value){el.setAttribute("dir","auto");}else{el.removeAttribute("dir");}}'
            in page)
    assert 'tokenInput.addEventListener("input",function(){syncDir(tokenInput);});' in page
    assert not re.search(r'tokenInput\.(dir\b|setAttribute\("dir")', page)  # only syncDir sets it
    assert page.count("syncDir(tokenInput)") >= 3  # typing, the form, and after a submit clears the field


def test_external_links_carry_no_referrer():
    page = setup_page.PAGE
    assert 'a.rel="noopener noreferrer"' in page and 'a.target="_blank"' in page
    assert '<meta name="referrer" content="no-referrer">' in page


# --- the terminal wizard -------------------------------------------------------------------------------------

def test_the_terminal_wizard_prints_what_it_printed_before(monkeypatch):
    """The steps are shared with the page now; the wizard's words are those it printed before (checked against
    the previous version: interactive, scripted, Threaded Mode off, rejected token, no network, failed test
    message and a declined account all printed the same and saved the same)."""
    bot = PageBot()
    answers = iter(["y", "he", ""])
    monkeypatch.setattr(setup.pairing, "new_code", lambda: "c0ffee00")
    bot.push("/start c0ffee00")
    lines = []

    def answer(prompt):
        lines.append("? " + prompt)
        return next(answers)

    code = setup.run(bot=bot, env={"CTB_BOT_TOKEN": GOOD_TOKEN}, interactive=True, install_hooks=False,
                     run_doctor=False, say=lines.append, input_fn=answer)
    assert code == 0
    assert "\n".join(lines) == "\n".join([
        "Harnessgram setup\n",
        "Step 1 of 4: create your bot (about 1 minute, in the Telegram app)",
        '  1. Open @BotFather and send /newbot. Pick a name and a username that ends in "bot".',
        "  2. Copy the token BotFather sends back (it looks like 123456789:AAE...). Keep it private.",
        "  3. Still in BotFather: /mybots > your bot > Bot Settings > Threaded Mode > turn ON.",
        "     (This gives the private chat with your bot Topics: one topic per Claude Code session.)",
        "",
        "Step 2 of 4: checking the bot with Telegram",
        "  Bot @demo_bot answers.",
        "  Threaded Mode is on.",
        "",
        "Step 3 of 4: pair your Telegram account",
        "  Open @demo_bot in Telegram and send exactly this message (waiting up to 3 minutes):",
        "",
        "      /start c0ffee00",
        "",
        "?   Pair with Dana Sample (@dana_sample, id 4242)? Only this account will be able to control your "
        "sessions. [y/N] ",
        "? ",
        "  Language you want to read replies in, ISO code like en, he, ar, ru, es [en]: ",
        "?   Bot UI language (menus and status), en or he [he]: ",
        f"  Saved {C.config_path()} (mode 600).",
        "",
        "Step 4 of 4: install the listener service and the Claude Code hooks",
        "  Skipped (--no-install). Run `harnessgram install` yourself.",
        "  Sent a test message to your chat with the bot.",
        # the closing names what was checked and, plainly, what was not (audit F5)
        "\nChecked:",
        f"  - config saved to {C.config_path()} (mode 600)",
        "  - a test message was sent to your chat",
        "Not checked:",
        "  - the hooks and the background service were not installed (--no-install)",
        "  - that the listener reaches Telegram: run harnessgram doctor",
        "  - that a Claude Code session reaches your chat (setup starts none)",
        "\nSetup finished. Start a new Claude Code session; its first reply creates its topic.",
    ])
    assert json.loads(C.config_path().read_text())["owner_chat_id"] == OWNER


def test_the_threaded_mode_hint_is_the_one_the_wizard_prints():
    lines = []
    assert setup._threaded_mode(None, {"has_topics_enabled": False}, False, input, lines.append) is False
    assert lines == ["  Threaded Mode is OFF. In BotFather: /mybots > your bot > Bot Settings > Threaded Mode > turn on."]


def test_doctor_collect_is_what_run_prints(monkeypatch, capsys):
    monkeypatch.setattr(doctor, "collect", lambda offline=False, bot=None, listener=None: [("ok", "fine"), ("FAIL", "broken")])
    assert doctor.run(offline=True) == 1
    assert capsys.readouterr().out == "[  ok] fine\n[FAIL] broken\n"


# --- the page shows what the install checked, and a failed install is no success (review of #221) -------------

from ctb import install as install_mod

# hooks confirmed and no service asked for (these fakes stand in for an install that started nothing to wait on)
GOOD_REPORT = install_mod.InstallReport(settings=Path("settings.json"), hooks=True, service=False)


def _failing_launchd(monkeypatch, tmp_path):
    home = tmp_path / "svc-home"
    home.mkdir()
    monkeypatch.setattr(install_mod, "HOME", home)
    monkeypatch.setattr(install_mod.sys, "platform", "darwin")
    ran = []
    monkeypatch.setattr(install_mod, "_sh", lambda *cmd: ran.append(cmd) or (5 if cmd[1] == "bootstrap" else 0))
    return ran


def test_a_service_that_did_not_start_is_shown_as_not_working(make, tmp_path, monkeypatch):
    ran = _failing_launchd(monkeypatch, tmp_path)
    monkeypatch.setattr(doctor, "collect", lambda offline=False, bot=None, listener=None: [("ok", "config is valid")])
    srv = make(install_hooks=True, service=True, start=True, run_doctor=True)
    ready(srv)
    status, body = api(srv, "finish", {"lang": "en"})
    assert status == 200 and "error" not in body, body
    done = body["state"]["finished"]
    assert any(c[1] == "bootstrap" for c in ran)  # the real install ran; only launchctl was a fake
    keys = [i["key"] for i in done["items"]]
    assert "r_service_failed" in keys and "r_service_running" not in keys and "r_listener_ok" not in keys
    failed = next(i for i in done["items"] if i["key"] == "r_service_failed")
    assert failed == {"key": "r_service_failed", "warn": True, "cmd": "install"}
    assert done["code"] == 1  # the banner says "Almost done", never "All set"
    assert "All set." not in "\n".join(srv.lines) and "Some checks need attention." in "\n".join(srv.lines)


def test_a_started_service_counts_only_with_the_listener_confirmed(make, tmp_path, monkeypatch):
    _failing_launchd(monkeypatch, tmp_path)
    monkeypatch.setattr(install_mod, "_sh", lambda *cmd: 0)  # launchd loads everything
    waits = []

    def wait(timeout, restarted=False, old_run=None):
        waits.append((timeout, restarted))
        return [("FAIL", "listener is not running")]

    monkeypatch.setattr(doctor, "wait_listener", wait)
    monkeypatch.setattr(doctor, "collect", lambda offline=False, bot=None, listener=None: list(listener))
    srv = make(install_hooks=True, service=True, start=True, run_doctor=True)
    ready(srv)
    done = api(srv, "finish", {"lang": "en"})[1]["state"]["finished"]
    keys = [i["key"] for i in done["items"]]
    assert "r_service_running" in keys and "r_listener_unchecked" in keys and done["code"] == 1
    assert waits and waits[0] == (setup.LISTENER_WAIT, True)
    # the health list and the exit code come from that same listener check
    assert done["doctor"] == [{"level": "FAIL", "text": "listener is not running"}]


def test_install_items_cover_every_outcome():
    R = install_mod.InstallReport
    p = Path("s.json")
    assert setup_page.install_items(None) == [{"key": "r_install_skipped", "warn": True, "cmd": "install"}]
    assert [i["key"] for i in setup_page.install_items(R(p, hooks=False, service=False))] == [
        "r_hooks_failed", "r_service_skipped"]
    assert setup_page.install_items(R(p, hooks=True, platform=False))[1]["cmd"] == "serve"
    assert setup_page.install_items(R(p, hooks=True, start=False))[1]["key"] == "r_service_not_started"
    # the listener runs and only a scheduled job failed: two separate lines, the job named
    assert setup_page.install_items(R(p, hooks=True, running=["listener", "dashboard"], failed=["daily",
                                                                                                "heartbeat"]))[1:] == [
        {"key": "r_service_running"},
        {"key": "r_jobs_failed", "warn": True, "cmd": "install", "vars": {"jobs": "daily, heartbeat"}}]
    assert [i["key"] for i in setup_page.install_items(R(p, hooks=True, running=["dashboard"],
                                                         failed=["listener"]))[1:]] == ["r_service_failed"]
    assert setup_page.install_items(R(p, hooks=True, running=["listener"]))[1] == {"key": "r_service_running"}
    assert setup_page.listener_items(R(p, hooks=True, running=["listener"]), [("ok", "x")]) == [
        {"key": "r_listener_ok"}]
    assert setup_page.listener_items(R(p, hooks=True, failed=["listener"]), None) == []


def test_every_install_line_has_both_languages_and_hebrew_keeps_commands_apart():
    for key in ("r_hooks_ok", "r_hooks_failed", "r_service_running", "r_service_failed", "r_jobs_failed",
                "r_service_not_started",
                "r_service_skipped", "r_service_unsupported", "r_listener_ok", "r_listener_unchecked"):
        en, he = setup_page.STRINGS["en"][key], setup_page.STRINGS["he"][key]
        assert en.strip() and he.strip()
        assert "harnessgram" not in en + he  # the command is drawn on its own line by the page
    assert 'serve:"harnessgram serve"' in setup_page.PAGE and "var warn=!!it.warn;" in setup_page.PAGE


def _started_listener_page(make, tmp_path, monkeypatch, run_doctor, listener):
    _failing_launchd(monkeypatch, tmp_path)
    monkeypatch.setattr(install_mod, "_sh", lambda *cmd: 0)  # launchd loads everything
    monkeypatch.setattr(doctor, "wait_listener", lambda timeout, restarted=False, old_run=None: listener)
    monkeypatch.setattr(doctor, "collect", lambda offline=False, bot=None, listener=None: list(listener or []))
    srv = make(install_hooks=True, service=True, start=True, run_doctor=run_doctor)
    ready(srv)
    return api(srv, "finish", {"lang": "he"})[1]["state"]["finished"], srv


def test_a_listener_only_warned_about_after_the_wait_is_almost_done(make, tmp_path, monkeypatch):
    done, srv = _started_listener_page(make, tmp_path, monkeypatch, True,
                                       [("warn", "listener is running but has not polled Telegram since it was started")])
    assert done["code"] == 1 and "r_listener_unchecked" in [i["key"] for i in done["items"]]
    assert "Some checks need attention." in "\n".join(srv.lines)


def test_a_skipped_health_check_with_a_started_listener_is_almost_done(make, tmp_path, monkeypatch):
    done, srv = _started_listener_page(make, tmp_path, monkeypatch, False, None)
    assert done["code"] == 1 and done["doctor"] == []
    assert "r_listener_unchecked" in [i["key"] for i in done["items"]] and "All set." not in "\n".join(srv.lines)


def test_the_page_banner_follows_the_code_in_both_languages():
    page = setup_page.PAGE
    assert 'var ok=st.finished.code===0' in page and 'ok?t("all_set"):t("almost")' in page
    assert setup_page.STRINGS["he"]["almost"] and setup_page.STRINGS["en"]["almost"]


def test_the_last_step_counts_as_activity_for_the_idle_stop(make):
    srv = make(idle_timeout=0.01)
    srv.flow.busy = True
    time.sleep(0.05)
    assert srv.should_stop() is None  # a long finish (the listener wait) is never cut by the idle stop
    srv.flow.busy = False
    time.sleep(0.05)
    assert srv.should_stop() == "idle"


def test_the_failed_job_names_are_filled_in_both_languages():
    page = setup_page.PAGE
    assert "t(it.key,it.vars)" in page  # names are set as text, never as markup
    for lang in ("en", "he"):
        assert "{jobs}" in setup_page.STRINGS[lang]["r_jobs_failed"]
    assert "\u2068{jobs}\u2069" in setup_page.STRINGS["he"]["r_jobs_failed"]  # English names isolated in Hebrew
