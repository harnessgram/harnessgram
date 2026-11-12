"""Large files through a self-hosted `telegram-bot-api --local`: limits, local reads, file:// uploads.
The cloud server keeps its old behaviour (20 MB down, 50 MB up)."""
import io
import json
import os
import stat
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from ctb import config as C
from ctb import doctor, inbox as I, mirror as M, telegram as T
from ctb.config import Config
from ctb.creds import relay_socket_path
from ctb.topics import TopicMap

TOKEN = "123456:" + "A" * 35
OWNER = 7
MIB = 1024 * 1024
LOCAL = dict(api_base="http://127.0.0.1:8081", api_local=True)


def local_cfg(files_dir="", **kw):
    return Config(bot_token=TOKEN, owner_chat_id=OWNER, api_files_dir=str(files_dir), **LOCAL, **kw)


def sparse(path, size):
    with open(path, "wb") as f:
        f.truncate(size)
    return str(path)


# --- config ------------------------------------------------------------------------------------

def test_defaults_are_the_cloud_server():
    cfg = Config()
    assert (cfg.api_base, cfg.api_local, cfg.api_files_dir) == ("https://api.telegram.org", False, "")
    assert (cfg.download_limit, cfg.upload_limit) == (20 * MIB, 50 * MIB)
    assert C.from_dict({"bot_token": TOKEN}).api_base == T.DEFAULT_API


def test_local_config_loads_and_raises_both_limits(tmp_path):
    cfg = C.from_dict({"api_base": "http://127.0.0.1:8081/", "api_local": True, "api_files_dir": str(tmp_path)})
    assert cfg.api_base == "http://127.0.0.1:8081" and cfg.api_files_dir == str(tmp_path)
    assert cfg.download_limit == cfg.upload_limit == 2000 * MIB


@pytest.mark.parametrize("raw", [
    {"api_local": True},                                         # --local needs its own server URL
    {"api_base": "http://example.test:8081"},                    # plain http away from this machine
    {"api_base": "ftp://127.0.0.1"},
    {"api_base": "https://user:pw@example.test"},
    {"api_base": "https://example.test/path?x=1"},
    {"api_local": "yes", "api_base": "http://localhost:8081"},
    {"api_files_dir": "relative/dir"},
    {"api_files_dir": "/"},
])
def test_bad_local_settings_are_rejected(raw):
    with pytest.raises(C.ConfigError):
        C.from_dict(raw)


def test_https_server_is_accepted_without_local_mode():
    cfg = C.from_dict({"api_base": "https://bot-api.example.test"})
    assert not cfg.api_local and cfg.upload_limit == 50 * MIB


def test_bot_from_config_carries_the_local_settings(tmp_path):
    bot = T.Bot.from_config(local_cfg(tmp_path))
    assert (bot.api, bot.local, bot.files_dir) == ("http://127.0.0.1:8081", True, str(tmp_path))
    assert (bot.download_limit, bot.upload_limit) == (2000 * MIB, 2000 * MIB)
    cloud = T.Bot.from_config(Config(bot_token=TOKEN, owner_chat_id=OWNER))
    assert cloud._base is None and cloud.api == T.API and not cloud.local
    assert (cloud.download_limit, cloud.upload_limit) == (20 * MIB, 50 * MIB)


# --- downloads ---------------------------------------------------------------------------------

class GetFileBot(T.Bot):
    def __init__(self, file_path, **kw):
        super().__init__(TOKEN, **kw)
        self.file_path = file_path

    def call(self, method, params=None, timeout=15):
        assert method == "getFile"
        return {"file_path": self.file_path}


def local_bot(path, root):
    return GetFileBot(str(path), base="http://127.0.0.1:8081", local=True, files_dir=str(root))


def test_local_download_copies_from_disk_inside_files_dir(tmp_path, monkeypatch):
    root = tmp_path / "srv"
    (root / "tok").mkdir(parents=True)
    src = root / "tok" / "video.mp4"
    src.write_bytes(b"x" * (3 * MIB))
    monkeypatch.setattr(T._OPENER, "open", lambda *a, **k: pytest.fail("a local file is not fetched over HTTP"))
    out = io.BytesIO()
    assert local_bot(src, root).download_to("fid", out) == 3 * MIB
    assert out.getvalue() == src.read_bytes()


def test_local_download_is_not_capped_at_20_mb(tmp_path):
    root = tmp_path / "srv"
    root.mkdir()
    big = sparse(root / "big.bin", 30 * MIB)
    assert local_bot(big, root).download_to("fid", io.BytesIO()) == 30 * MIB
    with pytest.raises(T.TelegramError, match="too large"):
        local_bot(big, root).download_to("fid", io.BytesIO(), limit=20 * MIB)


def test_local_download_refuses_paths_outside_files_dir(tmp_path):
    root, other = tmp_path / "srv", tmp_path / "other"
    root.mkdir()
    other.mkdir()
    secret = other / "secret.txt"
    secret.write_bytes(b"secret")
    (root / "link").symlink_to(secret)  # a symlink inside the folder pointing out
    (root / "dotdot").mkdir()
    for path in (secret, root / "link", root / "dotdot" / ".." / ".." / "other" / "secret.txt", root):
        with pytest.raises(T.TelegramError):
            local_bot(path, root).download_to("fid", io.BytesIO())
    # a sibling folder whose name merely starts with the root's name is outside too
    sibling = tmp_path / "srv-evil"
    sibling.mkdir()
    (sibling / "f").write_bytes(b"x")
    with pytest.raises(T.TelegramError):
        local_bot(sibling / "f", root).download_to("fid", io.BytesIO())


def test_local_download_needs_files_dir_and_a_regular_file(tmp_path):
    f = tmp_path / "f"
    f.write_bytes(b"x")
    with pytest.raises(T.TelegramError):
        local_bot(f, "").download_to("fid", io.BytesIO())
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(T.TelegramError):
        local_bot(fifo, tmp_path).download_to("fid", io.BytesIO())


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def test_cloud_download_is_unchanged(monkeypatch):
    seen = []

    def fake_open(url, timeout=None):
        seen.append(url)
        return FakeResponse(b"x" * 100)

    monkeypatch.setattr(T._OPENER, "open", fake_open)
    bot = GetFileBot("photos/file_1.jpg")
    assert bot.download("fid") == b"x" * 100
    assert seen == [f"{T.API}/file/bot{TOKEN}/photos/file_1.jpg"]
    assert (T.DOWNLOAD_LIMIT, bot.download_limit) == (20 * MIB, 20 * MIB)
    monkeypatch.setattr(T._OPENER, "open", lambda url, timeout=None: FakeResponse(b"x" * (20 * MIB + 1)))
    with pytest.raises(T.TelegramError, match="too large"):
        bot.download("fid")
    # even an absolute path from the server is not read from disk in cloud mode
    monkeypatch.setattr(T._OPENER, "open", lambda url, timeout=None: FakeResponse(b"remote"))
    assert GetFileBot("/etc/hostname").download("fid") == b"remote"


def test_media_streams_to_disk_with_the_local_limit_and_cleans_up(tmp_path):
    root = tmp_path / "srv"
    root.mkdir()
    src = sparse(root / "v.mp4", 25 * MIB)
    cfg = local_cfg(root)
    media = I.Media(cfg, local_bot(src, root), tmp_path / "dl")
    path = media.save("fid", ".mp4")
    assert os.path.getsize(path) == 25 * MIB
    # a refused download leaves no half-written file behind
    bad = I.Media(cfg, local_bot(tmp_path / "elsewhere", root), tmp_path / "dl2")
    with pytest.raises(T.TelegramError):
        bad.save("fid", ".mp4")
    assert list((tmp_path / "dl2").iterdir()) == []


def test_video_note_uses_the_effective_limit(tmp_path):
    class B:
        def download(self, fid):
            return b"data"
        download_to = None

    video = {"video": {"file_id": "v", "file_size": 30_000_000}}
    cloud = I.Media(Config(bot_token=TOKEN, owner_chat_id=OWNER), B(), tmp_path / "dl")
    assert "video over 20 MB" in cloud.content_of(video)
    small = {"video": {"file_id": "v", "file_size": 19_000_000}}
    assert "video over" not in cloud.content_of(small)
    stream_bot = type("S", (), {"download_to": lambda self, fid, f, limit: f.write(b"data")})()
    local = I.Media(local_cfg(), stream_bot, tmp_path / "dl3")
    assert "video over" not in local.content_of(video)
    huge = {"video": {"file_id": "v", "file_size": 2100 * 1_000_000}}
    assert "video over 2000 MB" in local.content_of(huge)


# --- uploads -----------------------------------------------------------------------------------

class Seen(list):
    base = ""


@pytest.fixture
def server(monkeypatch):
    seen = Seen()

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            seen.append((self.path, self.headers.get("Content-Type", ""), body))
            data = json.dumps({"ok": True, "result": {"message_id": 1}}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    seen.base = f"http://127.0.0.1:{srv.server_port}"
    yield seen
    srv.shutdown()


def test_upload_path_sends_a_file_uri_not_the_bytes(server, tmp_path):
    big = sparse(tmp_path / "movie.mp4", 60 * MIB)
    bot = T.Bot(TOKEN, base=server.base, local=True)
    bot.upload_path("sendDocument", {"chat_id": OWNER, "caption": "c"}, "document", big)
    path, ctype, body = server[0]
    assert path == f"/bot{TOKEN}/sendDocument" and ctype.startswith("multipart/form-data")
    assert len(body) < 2000  # the 60 MB never travelled through this process
    assert f'name="document"\r\n\r\nfile://{big}\r\n'.encode() in body
    assert b"filename=" not in body


def test_file_uri_is_percent_encoded():
    assert T.file_uri("/srv/a b/c#d%e.bin") == "file:///srv/a%20b/c%23d%25e.bin"


def test_upload_path_refuses_cloud_mode_and_bad_files(server, tmp_path):
    f = tmp_path / "f"
    f.write_bytes(b"x")
    with pytest.raises(T.TelegramError, match="local Bot API"):
        T.Bot(TOKEN, base=server.base).upload_path("sendDocument", {}, "document", str(f))
    local = T.Bot(TOKEN, base=server.base, local=True)
    (tmp_path / "link").symlink_to(f)
    for bad in (str(tmp_path), str(tmp_path / "link"), str(tmp_path / "missing")):
        with pytest.raises((T.TelegramError, OSError)):
            local.upload_path("sendDocument", {}, "document", bad)
    with pytest.raises(T.TelegramError):
        local.upload_path("sendDocument", {}, "document", sparse(tmp_path / "huge", 2001 * MIB))
    with pytest.raises(T.TelegramError, match="needs the bot token"):
        T.Bot(None, base=server.base, local=True).upload_path("sendDocument", {}, "document", str(f))
    with pytest.raises(T.TelegramError):
        T.file_uri("relative.txt")
    with pytest.raises(T.TelegramError):
        T.file_uri("/tmp/a\nb")
    assert not server


class RecordingBot:
    direct = True

    def __init__(self):
        self.calls = []

    def call(self, method, params=None, timeout=15):
        self.calls.append((method, params or {}))
        if method == "createForumTopic":
            return {"message_thread_id": 101}
        return {"message_id": len(self.calls)} if method == "sendMessage" else {}

    def upload(self, method, fields, file_field, filename, payload, timeout=120):
        self.calls.append((method, dict(fields, _file=filename, _bytes=len(payload))))
        return {}

    def upload_path(self, method, fields, file_field, path, timeout=1800):
        with open(path, "rb") as f:  # the staged copy exists while the server reads it
            head = f.read(4)
        self.calls.append((method + ":path", dict(fields, _path=path, _field=file_field, _head=head)))
        return {}

    def methods(self, name):
        return [p for m, p in self.calls if m == name]


def mirror_for(cfg, tmp_path, bot):
    return M.Mirror(cfg, bot, TopicMap(tmp_path / "topics.json", bot, OWNER))


def test_local_mode_sends_big_files_by_path_and_small_ones_as_bytes(tmp_path):
    bot = RecordingBot()
    mirror = mirror_for(local_cfg(), tmp_path, bot)
    big = sparse(tmp_path / "big.mov", 120 * MIB)
    mirror.send_file("t", "s", big)
    sent = bot.methods("sendDocument:path")
    assert len(sent) == 1 and sent[0]["_field"] == "document"
    staged = sent[0]["_path"]
    assert staged != big and os.path.basename(staged) == "file.mov"  # the server got a private copy ...
    assert not os.path.exists(os.path.dirname(staged))  # ... which is deleted after the send
    assert not bot.methods("sendDocument")
    small = tmp_path / "small.txt"
    small.write_bytes(b"hello")
    mirror.send_file("t", "s", str(small))
    assert bot.methods("sendDocument")[0]["_bytes"] == 5
    assert len(bot.methods("sendDocument:path")) == 1
    edge = sparse(tmp_path / "edge.bin", 50 * MIB)  # exactly the multipart limit still goes as bytes
    mirror.send_file("t", "s", edge)
    assert bot.methods("sendDocument")[-1]["_bytes"] == 50 * MIB


def test_local_mode_notes_files_over_2000_mb(tmp_path):
    bot = RecordingBot()
    cfg = local_cfg()
    mirror_for(cfg, tmp_path, bot).send_file("t", "s", sparse(tmp_path / "huge.bin", 2001 * MIB))
    text = bot.methods("sendMessage")[-1]["text"]
    assert "2000" in text and not bot.methods("sendDocument:path")


def test_cloud_mode_never_uses_paths_and_keeps_the_45_mb_cut(tmp_path):
    bot = RecordingBot()
    cfg = Config(bot_token=TOKEN, owner_chat_id=OWNER)
    mirror = mirror_for(cfg, tmp_path, bot)
    assert (M.UPLOAD_LIMIT, M.DOCUMENT_LIMIT, M.upload_limit(cfg)) == (45 * MIB, 50 * MIB, 45 * MIB)
    mirror.send_file("t", "s", sparse(tmp_path / "over.bin", 46 * MIB))
    assert not bot.methods("sendDocument") and not bot.methods("sendDocument:path")
    assert "45" in bot.methods("sendMessage")[-1]["text"]
    ok = tmp_path / "ok.bin"
    ok.write_bytes(b"x" * 1000)
    mirror.send_file("t", "s", str(ok))
    assert bot.methods("sendDocument")[0]["_bytes"] == 1000


def test_limit_text_shows_the_effective_limit_and_cloud_wording_is_unchanged(tmp_path):
    en, he = (M.L.strings(lang)["file_too_large_n"] for lang in ("en", "he"))
    assert "over 45 MB" in en.format(mb=45) and "over 2000 MB" in en.format(mb=2000)
    assert he.format(mb=2000).splitlines()[-1] == "2000 MB"
    assert all(not any("a" <= c.lower() <= "z" or c.isdigit() for c in line)
               for line in he.format(mb=2000).splitlines()[:-1])
    # cloud mode: the original fixed strings, byte for byte
    assert M.L.strings("en")["file_too_large"] == "The file is over 45 MB and was not sent via Telegram; it stays on the Mac."
    assert M.L.strings("he")["file_too_large"] == "הקובץ גדול מ־45MB ולכן לא נשלח בטלגרם. הוא נשמר במחשב."
    bot = RecordingBot()
    mirror_for(Config(bot_token=TOKEN, owner_chat_id=OWNER, target_language="en"), tmp_path, bot).send_file(
        "t", "s", sparse(tmp_path / "over.bin", 46 * MIB))
    assert M.L.strings("en")["file_too_large"] in bot.methods("sendMessage")[-1]["text"]


# --- staging, the relay and file: values ----------------------------------------------------------

def test_stage_copies_into_a_private_folder_under_files_dir(tmp_path):
    root = tmp_path / "srv"
    root.mkdir()
    src = tmp_path / "proj" / "my video.mp4"
    src.parent.mkdir()
    src.write_bytes(b"abcdef")
    job, staged = M.stage_upload(local_cfg(root), str(src), 100)
    assert staged.startswith(str(root / M.STAGING_DIR) + os.sep) and os.path.basename(staged) == "file.mp4"
    assert open(staged, "rb").read() == b"abcdef"
    assert stat.S_IMODE(os.stat(root / M.STAGING_DIR).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(job).st_mode) == 0o700 and stat.S_IMODE(os.stat(staged).st_mode) == 0o600
    # without a usable api_files_dir the state dir is used
    job2, staged2 = M.stage_upload(local_cfg(), str(src), 100)
    assert staged2.startswith(str(C.state_dir() / M.STAGING_DIR))
    with pytest.raises(T.TelegramError):  # over the limit
        M.stage_upload(local_cfg(root), str(src), 3)


def test_stage_refuses_unsafe_sources(tmp_path):
    cfg = local_cfg(tmp_path / "srv")
    plain = tmp_path / "plain.bin"
    plain.write_bytes(b"x")
    (tmp_path / "link").symlink_to(plain)
    os.link(plain, tmp_path / "twin")  # two names for one file
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    for bad in (tmp_path / "link", plain, tmp_path / "twin", fifo, tmp_path, tmp_path / "missing"):
        with pytest.raises((T.TelegramError, OSError)):
            M.stage_upload(cfg, str(bad), 100)


def test_stage_refuses_the_bridges_own_folders_and_their_hard_links(tmp_path):
    cfg = local_cfg(tmp_path / "srv")
    for folder in (C.config_dir(), C.state_dir() / "downloads"):
        folder.mkdir(parents=True)
    secret = C.config_dir() / "config.json"
    secret.write_text("{}")
    note = C.state_dir() / "downloads" / "pic.jpg"
    note.write_bytes(b"x")
    for bad in (secret, note):
        with pytest.raises(T.TelegramError, match="own folders"):
            M.stage_upload(cfg, str(bad), 100)
    # a hard link elsewhere to a config file is refused too (two links)
    twin = tmp_path / "innocent.txt"
    os.link(secret, twin)
    with pytest.raises(T.TelegramError, match="hard links"):
        M.stage_upload(cfg, str(twin), 100)
    # a different spelling of the same folder (symlinked parent) is found by inode, not by name
    (tmp_path / "alias").symlink_to(C.config_dir())
    with pytest.raises(T.TelegramError):
        M.stage_upload(cfg, str(tmp_path / "alias" / "config.json"), 100)


def test_staged_copy_is_deleted_even_when_the_send_fails(tmp_path):
    class Failing(RecordingBot):
        def upload_path(self, *a, **k):
            raise T.TelegramError("boom")

    cfg = local_cfg(tmp_path / "srv")
    (tmp_path / "srv").mkdir()
    big = sparse(tmp_path / "big.bin", 60 * MIB)
    with pytest.raises(T.TelegramError):
        mirror_for(cfg, tmp_path, Failing()).send_file("t", "s", big)
    assert list((tmp_path / "srv" / M.STAGING_DIR).iterdir()) == []


def test_session_without_the_token_gets_a_note_for_files_over_50_mb(tmp_path):
    bot = RecordingBot()
    bot.direct = False
    mirror_for(local_cfg(), tmp_path, bot).send_file("t", "s", sparse(tmp_path / "big.bin", 60 * MIB))
    assert not bot.methods("sendDocument:path") and not bot.methods("sendDocument")
    assert bot.methods("sendMessage")[-1]["text"].endswith("\n50 MB")


@pytest.mark.parametrize("value", [
    "file:///etc/hosts", "FILE:///etc/hosts", "File:/etc/hosts", "  file:///etc/hosts",
    {"media": {"type": "document", "media": "FiLe:///etc/hosts"}},
    [{"a": ["x", "file:///etc/hosts"]}],
    {"nested": {"deep": [["file:x"]]}},
])
def test_local_bot_rejects_file_values_in_calls_and_uploads(server, value):
    local = T.Bot(TOKEN, base=server.base, local=True)
    with pytest.raises(T.TelegramError, match="file_uri_not_allowed"):
        local.call("sendDocument", {"chat_id": OWNER, "document": value})
    with pytest.raises(T.TelegramError, match="file_uri_not_allowed"):
        local.upload("sendDocument", {"chat_id": OWNER, "thumbnail": value}, "document", "f", b"x")
    with pytest.raises(T.TelegramError, match="file_uri_not_allowed"):
        local.upload_path("sendDocument", {"chat_id": OWNER, "thumbnail": value}, "document", "/x")
    with pytest.raises(T.TelegramError, match="file_uri_not_allowed"):  # also without a token (relay client)
        T.Bot(None, base=server.base, local=True).call("sendDocument", {"document": value})
    assert not server


def test_file_text_in_messages_still_goes_through(server):
    local = T.Bot(TOKEN, base=server.base, local=True)
    local.call("sendMessage", {"chat_id": OWNER, "text": "File: report.txt is ready",
                               "reply_markup": {"inline_keyboard": [[{"text": "file: open", "callback_data": "x"}]]}})
    assert len(server) == 1
    # cloud mode forwards values untouched, as before
    T.Bot(TOKEN, base=server.base).call("sendDocument", {"chat_id": OWNER, "document": "file:///x"})
    assert len(server) == 2


@pytest.fixture
def short_state(monkeypatch):
    with tempfile.TemporaryDirectory(dir="/tmp") as directory:
        monkeypatch.setenv("CTB_STATE_DIR", directory)
        monkeypatch.setenv("CTB_CONFIG_DIR", directory + "-config")
        yield directory


def run_relay(cfg, bot):
    stop = threading.Event()
    threading.Thread(target=I.relay_serve, args=(cfg,), kwargs={"bot": bot, "stop_event": stop}, daemon=True).start()
    path = relay_socket_path()
    for _ in range(100):
        if path.is_socket():
            break
        time.sleep(0.02)
    return stop


def raw_relay(request):
    from ctb.creds import relay_call
    return relay_call(request)


@pytest.mark.parametrize("cfg", [local_cfg(), Config(bot_token=TOKEN, owner_chat_id=OWNER)], ids=["local", "cloud"])
def test_relay_has_no_path_route_and_only_the_methods_own_file_field(short_state, tmp_path, cfg):
    f = tmp_path / "f"
    f.write_bytes(b"x")
    bot = RecordingBot()
    stop = run_relay(cfg, bot)
    try:
        base = {"kind": "upload", "method": "sendDocument", "params": {"chat_id": OWNER},
                "file_field": "document", "payload": "eA=="}
        raw_relay(base)  # plain bytes still work (and are all the relay carries)
        assert len(bot.methods("sendDocument")) == 1
        with pytest.raises(T.TelegramError, match="method_not_relayed"):
            raw_relay(dict(base, path=str(f)))  # a path is never read for a client
        for field in ("thumbnail", "photo", "media", "video"):
            with pytest.raises(T.TelegramError, match="method_not_relayed"):
                raw_relay(dict(base, file_field=field))
        raw_relay(dict(base, method="sendPhoto", file_field="photo"))
        with pytest.raises(T.TelegramError, match="method_not_relayed"):
            raw_relay(dict(base, method="sendPhoto", file_field="document"))
        assert len(bot.calls) == 2 and not bot.methods("sendDocument:path")
    finally:
        stop.set()


def test_relay_refuses_file_values_in_local_mode(short_state):
    bot = RecordingBot()  # a fake that would not check by itself: the relay does
    stop = run_relay(local_cfg(), bot)
    try:
        for kind, method in (("api", "sendMessage"), ("upload", "sendDocument")):
            for params in ({"chat_id": OWNER, "reply_markup": "FILE:///etc/hosts"},
                           {"chat_id": OWNER, "x": [{"y": "file:///etc/hosts"}]}):
                request = {"kind": kind, "method": method, "params": params, "file_field": "document",
                           "payload": "eA=="}
                with pytest.raises(T.TelegramError, match="file_uri_not_allowed"):
                    raw_relay(request)
        assert not bot.calls
    finally:
        stop.set()


def test_relay_bytes_limit_is_still_50_mb():
    assert I.RELAY_DOCUMENT_LIMIT == 50 * MIB
    assert not hasattr(I, "relay_upload_path")


# --- proxies -----------------------------------------------------------------------------------

def test_plain_http_base_uses_the_no_proxy_opener():
    assert T._opener_for("http://127.0.0.1:8081/bot1/getMe") is T._LOCAL_OPENER
    assert T._opener_for("https://api.telegram.org/x") is T._OPENER


def test_local_server_call_works_with_a_proxy_in_the_environment(server, monkeypatch):
    monkeypatch.setenv("http_proxy", "http://proxy.invalid:3128")
    assert T.Bot(TOKEN, base=server.base, local=True).call("getMe")["message_id"] == 1


# --- doctor ------------------------------------------------------------------------------------

def test_doctor_local_checks(tmp_path):
    assert doctor.check_local_api(Config()) == []
    assert doctor.check_local_api(Config(api_base="https://bot-api.example.test"))[0][0] == "warn"
    missing = doctor.check_local_api(local_cfg())
    assert missing[0][0] == "ok" and missing[-1][0] == "FAIL" and "api_files_dir" in missing[-1][1]
    assert doctor.check_local_api(local_cfg(tmp_path / "nope"))[-1][0] == "FAIL"
    assert [lvl for lvl, _ in doctor.check_local_api(local_cfg(tmp_path))] == ["ok", "ok"]
