"""The HTTP client and the mirror, against a throwaway local server (no real network)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from ctb import mirror as M
from ctb import telegram as T
from ctb.config import Config
from ctb.topics import TopicMap

TOKEN = "123456:TESTTOKENVALUE" + "x" * 20


@pytest.fixture
def server(monkeypatch):
    seen = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            method = self.path.rsplit("/", 1)[-1]
            seen.append((self.path, self.headers.get("Content-Type", ""), body))
            if method == "badMethod":
                out, code = {"ok": False, "error_code": 400, "description": "Bad Request: nope"}, 400
            elif method == "createForumTopic":
                out, code = {"ok": True, "result": {"message_thread_id": 77}}, 200
            elif method == "sendMessage":
                out, code = {"ok": True, "result": {"message_id": len(seen)}}, 200  # as real Telegram does
            else:
                out, code = {"ok": True, "result": {"username": "tbot", "has_topics_enabled": True}}, 200
            data = json.dumps(out).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(T, "API", f"http://127.0.0.1:{srv.server_port}")
    yield seen
    srv.shutdown()


def test_call_returns_result(server):
    assert T.Bot(TOKEN).call("getMe")["username"] == "tbot"
    assert server[0][0] == f"/bot{TOKEN}/getMe"


def test_error_carries_description_but_never_the_token(server):
    with pytest.raises(T.TelegramError) as e:
        T.Bot(TOKEN).call("badMethod")
    assert e.value.description == "Bad Request: nope" and e.value.code == 400
    assert "TESTTOKENVALUE" not in repr(e.value) + str(e.value)
    assert "TESTTOKENVALUE" not in repr(T.Bot(TOKEN))


def test_network_error_is_wrapped(monkeypatch):
    monkeypatch.setattr(T, "API", "http://127.0.0.1:9")  # nothing listens here
    with pytest.raises(T.TelegramError, match="network error") as e:
        T.Bot(TOKEN).call("getMe", timeout=2)
    assert "TESTTOKENVALUE" not in str(e.value)


def _mirror(server, tmp_path, **cfg_kw):
    cfg = Config(bot_token=TOKEN, owner_chat_id=42, translate=True, target_language="he", **cfg_kw)
    bot = T.Bot(TOKEN)
    m = M.Mirror(cfg, bot, TopicMap(tmp_path / "topics.json", bot, 42))
    n = m.send_reply("Proj", "sess", "Body text here.\nSummary: Done.", lambda t: "תרגום: " + t)
    return n, [json.loads(b) for p, _, b in server if p.endswith("/sendMessage")]


def test_hebrew_goes_first_and_original_is_folded_in_folded_after_mode(server, tmp_path):
    n, sends = _mirror(server, tmp_path, original="folded-after")  # the default is now "edit" (PR #143)
    assert n == 2
    assert sends[0]["message_thread_id"] == 77 and sends[0]["disable_notification"] is True
    assert sends[0]["text"].startswith("\u200f\U0001F4CC")  # translation with the pinned summary
    assert "<blockquote expandable>" in sends[1]["text"] and "Body text here." in sends[1]["text"]


def test_original_before_goes_first_and_open(server, tmp_path):
    n, sends = _mirror(server, tmp_path, original="before")
    assert n == 2
    assert sends[0]["text"].startswith("\u200f\U0001F310 <b>מקור</b>\n\nBody text here.")
    assert "blockquote" not in sends[0]["text"]
    assert sends[1]["text"].startswith("\u200f\U0001F4CC")


def test_original_off_sends_the_translation_only(server, tmp_path):
    n, sends = _mirror(server, tmp_path, original="off")
    assert n == 1 and sends[0]["text"].startswith("\u200f\U0001F4CC")


def test_mirror_redacts_secrets_before_sending(server, tmp_path):
    cfg = Config(bot_token=TOKEN, owner_chat_id=42, target_language="en")
    bot = T.Bot(TOKEN)
    m = M.Mirror(cfg, bot, TopicMap(tmp_path / "topics.json", bot, 42))
    m.send_reply("P", "s", "key is " + "gh" + "p_" + "q" * 30)
    sent = [b for p, _, b in server if p.endswith("/sendMessage")][0].decode()
    assert "ghp_" not in sent and "redacted" in sent


def test_send_file_is_multipart(server, tmp_path):
    f = tmp_path / "note.txt"
    f.write_bytes(b"hello file")
    cfg = Config(bot_token=TOKEN, owner_chat_id=42)
    bot = T.Bot(TOKEN)
    M.Mirror(cfg, bot, TopicMap(tmp_path / "topics.json", bot, 42)).send_file("P", "s", str(f), "cap")
    path, ctype, body = [s for s in server if s[0].endswith("/sendDocument")][0]
    assert ctype.startswith("multipart/form-data; boundary=")
    assert b'name="document"; filename="note.txt"' in body and b"hello file" in body
    assert b'name="message_thread_id"' in body


def test_png_goes_as_photo(server, tmp_path):
    f = tmp_path / "x.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 20)
    cfg = Config(bot_token=TOKEN, owner_chat_id=42)
    bot = T.Bot(TOKEN)
    M.Mirror(cfg, bot, TopicMap(tmp_path / "topics.json", bot, 42)).send_file("P", "s", str(f))
    assert any(s[0].endswith("/sendPhoto") for s in server)


def test_format_error_falls_back_to_plain_and_deleted_topic_is_recreated(tmp_path, bot):
    cfg = Config(bot_token="t", owner_chat_id=42, target_language="en")
    topics = TopicMap(tmp_path / "topics.json", bot, 42)
    m = M.Mirror(cfg, bot, topics)
    bot.fail["sendMessage"] = "Bad Request: can't parse entities"
    m.send_reply("P", "s", "hello **world**")
    plain = bot.methods("sendMessage")[-1]
    assert "parse_mode" not in plain and plain["text"] == "hello world"
    bot.fail["sendMessage"] = "Bad Request: message thread not found"
    m.send_reply("P", "s", "again")
    assert len(bot.methods("createForumTopic")) == 2  # mapping dropped and topic recreated


def test_https_handler_does_not_use_removed_python_312_attributes(monkeypatch):
    """Python 3.12 dropped HTTPSHandler._check_hostname and HTTPSConnection(check_hostname=...): the handler
    must open HTTPS connections with the context only (every real call failed with "network error")."""
    handler = T._HTTPSHandler()
    seen = {}

    def fake_do_open(self, conn_cls, req, **kwargs):
        seen["conn"], seen["kwargs"] = conn_cls, kwargs
        return "opened"

    monkeypatch.setattr(T.urllib.request.HTTPSHandler, "do_open", fake_do_open)
    assert handler.https_open(object()) == "opened"
    assert seen["conn"] is T._HTTPSConnection
    assert set(seen["kwargs"]) == {"context"}
