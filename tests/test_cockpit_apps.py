"""Cockpit apps: manifests checked strictly, every /app/ route behind the owner's initData, a jailed media root
with HTTP Range, and an API command run without a shell, with a timeout and a small environment.

The sample app lives only here: each test builds it in its own temporary state folder."""
import json
import os
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote

import pytest

from conftest import GOOD_TOKEN
from ctb import cockpit_apps, panel, panel_page
from ctb.config import Config, config_dir, state_dir
from test_panel import OWNER, call, signed

API_SCRIPT = r'''
import json, os, subprocess, sys, time
req = json.load(sys.stdin)
mode = req.get("mode")
if mode == "echo":
    print(json.dumps({"got": req, "cwd": os.getcwd(), "argv": sys.argv[1:], "app": os.environ.get("COCKPIT_APP"),
                      "secret": os.environ.get("SAMPLE_SECRET"), "token": os.environ.get("CTB_BOT_TOKEN")}))
elif mode == "fail":
    sys.exit(3)
elif mode == "junk":
    print("not json")
elif mode == "list":
    print("[1, 2]")
elif mode == "big":
    print(json.dumps({"x": "a" * 5000}))
elif mode == "sleep":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    open("child.pid", "w").write(str(child.pid))
    time.sleep(30)
'''


def make_app(name="sample", with_media=True, with_command=True, **override):
    folder = state_dir() / "cockpit-apps" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "index.html").write_text("<!doctype html><title>Sample</title><p>sample app</p>", encoding="utf-8")
    (folder / "api.py").write_text(API_SCRIPT, encoding="utf-8")
    manifest = {"title": "Sample", "title_he": "דוגמה", "icon": "box", "page": "index.html"}
    if with_media:
        root = state_dir().parent / f"media-{name}"
        root.mkdir(exist_ok=True)
        manifest["media_root"] = str(root)
    if with_command:
        manifest["command"] = [sys.executable, "api.py", "--sample", "$(touch pwned)"]
    manifest.update(override)
    manifest = {k: v for k, v in manifest.items() if v is not None}
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return folder


def media_root(name="sample") -> Path:
    return state_dir().parent / f"media-{name}"


@pytest.fixture
def server():
    cockpit_apps._warned.clear()
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he")
    srv = panel.PanelServer(("127.0.0.1", 0), cfg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def get(srv, path, init=None, headers=None, auth=True):
    if auth:
        init = signed() if init is None else init
        path += ("&" if "?" in path else "?") + "auth=" + quote(init, safe="")
    return call(srv, "GET", path, headers=headers)


def api(srv, body, init=None):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return call(srv, "POST", "/app/sample/api", init=signed() if init is None else init, body=raw,
                headers={"Content-Type": "application/json"})


# --- manifests -----------------------------------------------------------------------------------

def test_a_valid_app_is_listed_in_the_state(server):
    make_app()
    code, _, data = call(server, init=signed())
    assert code == 200
    assert json.loads(data)["apps"] == [{"name": "sample", "title": "Sample", "title_he": "דוגמה", "icon": "box"}]


def test_no_apps_folder_means_an_empty_list(server):
    assert json.loads(call(server, init=signed())[2])["apps"] == []


@pytest.mark.parametrize("override, reason", [
    ({"extra": 1}, "unknown keys"),
    ({"icon": "rocket"}, "icon"),
    ({"title": ""}, "title"),
    ({"title": "x" * 25}, "title"),
    ({"title": "line\nbreak"}, "title"),
    ({"page": "../index.html"}, "page"),
    ({"page": "/etc/hosts"}, "page"),
    ({"page": ".hidden.html"}, "page"),
    ({"page": "api.py"}, "page"),
    ({"page": "missing.html"}, "page"),
    ({"media_root": "relative/folder"}, "media_root"),
    ({"media_root": "/"}, "media_root"),
    ({"command": "python api.py"}, "command"),
    ({"command": []}, "command"),
    ({"command": ["api.py"]}, "executable"),
    ({"command": ["../outside"]}, "command[0]"),
])
def test_an_invalid_manifest_is_ignored_with_one_log_line(server, capsys, override, reason):
    make_app(**override)
    for _ in range(2):
        code, _, data = call(server, init=signed())
        assert code == 200 and json.loads(data)["apps"] == []
    err = capsys.readouterr().err
    assert err.count("cockpit app 'sample' ignored") == 1 and reason in err
    assert GOOD_TOKEN not in err


def test_a_media_root_holding_the_bridges_own_files_is_refused(server, capsys):
    for root in (state_dir(), config_dir(), state_dir().parent, Path.home()):
        Path(root).mkdir(parents=True, exist_ok=True)
        cockpit_apps._warned.clear()
        make_app(media_root=str(root))
        assert cockpit_apps.load_apps() == [], root
    inbox = state_dir() / "inbox"
    inbox.mkdir()
    make_app(media_root=str(inbox))
    assert cockpit_apps.load_apps() == []
    assert "media_root" in capsys.readouterr().err


def test_broken_json_too_large_and_bad_folder_names_never_break_the_server(server, capsys):
    base = state_dir() / "cockpit-apps"
    (base / "broken").mkdir(parents=True)
    (base / "broken" / "manifest.json").write_text("{not json")
    (base / "huge").mkdir()
    (base / "huge" / "manifest.json").write_text(json.dumps({"title": "x" * (cockpit_apps.MAX_MANIFEST + 1)}))
    (base / "Bad_Name").mkdir()
    (base / "empty").mkdir()
    make_app()
    code, _, data = call(server, init=signed())
    assert code == 200 and [a["name"] for a in json.loads(data)["apps"]] == ["sample"]
    err = capsys.readouterr().err
    assert "'broken' ignored: manifest.json is not valid JSON" in err and "'huge' ignored: manifest.json is too large" in err
    assert "invalid folder name" in err and "'empty' ignored" in err


def test_a_page_symlink_out_of_the_app_folder_is_refused(server, tmp_path):
    folder = make_app()
    outside = tmp_path / "outside.html"
    outside.write_text("<p>secret</p>")
    (folder / "link.html").symlink_to(outside)
    make_app(page="link.html")
    assert cockpit_apps.load_apps() == []


# --- auth on every route -------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/app/sample/", "/app/sample/media/clip.mp4", "/app/unknown/", "/app/unknown/media/x.png"])
@pytest.mark.parametrize("init", [
    lambda: None,
    lambda: signed(tamper=True),
    lambda: signed(auth_date=int(time.time()) - panel.MAX_AGE - 5),
    lambda: signed(user_id=OWNER + 1),
    lambda: signed(token="654321:" + "B" * 35),
    lambda: "garbage",
])
def test_every_get_route_needs_the_owners_init_data(server, path, init):
    make_app()
    (media_root() / "clip.mp4").write_bytes(b"\0" * 64)
    value = init()
    code, hdrs, data = get(server, path, init=value, auth=value is not None)
    assert code == 403 and json.loads(data) == {"error": "forbidden"}  # an unknown app looks the same: nothing leaks
    assert hdrs["Cache-Control"] == "no-store"


def test_the_page_is_served_with_query_or_header_auth(server):
    make_app()
    code, hdrs, data = get(server, "/app/sample/")
    assert code == 200 and b"sample app" in data
    assert hdrs["Content-Type"] == "text/html; charset=utf-8" and hdrs["Cache-Control"] == "no-store"
    assert call(server, "GET", "/app/sample/", init=signed())[0] == 200


def test_a_repeated_or_malformed_auth_parameter_is_refused(server):
    make_app()
    good = quote(signed(), safe="")
    assert call(server, "GET", f"/app/sample/?auth={good}&auth={good}")[0] == 403
    assert call(server, "GET", f"/app/sample/?auth={good}&broken")[0] == 403
    assert call(server, "GET", f"/app/sample/?auth={good}&v=2")[0] == 200


def test_the_api_needs_the_header_and_the_fresher_login(server):
    make_app()
    old = signed(auth_date=int(time.time()) - panel.DECIDE_MAX_AGE - 60)
    assert get(server, "/app/sample/", init=old)[0] == 200  # reading keeps the reading window
    assert api(server, {"mode": "echo"}, init=old)[0] == 403  # acting needs the fresher one
    raw = json.dumps({"mode": "echo"}).encode()
    assert call(server, "POST", "/app/sample/api?auth=" + quote(signed(), safe=""), body=raw)[0] == 404
    assert call(server, "POST", "/app/sample/api", body=raw)[0] == 403
    assert api(server, {"mode": "echo"}, init=signed(tamper=True))[0] == 403


def test_other_methods_and_shapes_are_refused(server):
    make_app()
    assert call(server, "PUT", "/app/sample/")[0] == 405
    assert call(server, "DELETE", "/app/sample/api")[0] == 405
    assert call(server, "HEAD", "/app/sample/media/clip.mp4")[0] == 405
    assert get(server, "/app/sample")[0] == 404
    assert get(server, "/app/sample/api")[0] == 404
    assert get(server, "/app/sample/other.html")[0] == 404
    assert get(server, "/app/Sample/")[0] == 404


# --- the page's policy ---------------------------------------------------------------------------

def test_the_app_page_is_sandboxed_and_cannot_call_out(server):
    make_app()
    _, hdrs, _ = get(server, "/app/sample/")
    csp = hdrs["Content-Security-Policy"]
    assert "sandbox allow-scripts allow-forms" in csp and "allow-same-origin" not in csp
    assert "connect-src 'none'" in csp and "default-src 'none'" in csp and "form-action 'none'" in csp
    assert f"frame-ancestors 'self' {panel.FRAME_ANCESTORS}" in csp
    assert "script-src 'unsafe-inline';" in csp  # inline only: no file, host or media URL can run as a script
    assert "Access-Control-Allow-Origin" not in hdrs


def test_a_forged_host_header_never_reaches_the_policy(server):
    make_app()
    _, hdrs, _ = get(server, "/app/sample/", headers={"Host": "evil.example; script-src *"})
    assert "evil" not in hdrs["Content-Security-Policy"]
    _, hdrs, _ = get(server, "/app/sample/", headers={"Host": "box.example.test:10000"})
    assert "media-src 'self' box.example.test:10000 blob:" in hdrs["Content-Security-Policy"]


# --- media ---------------------------------------------------------------------------------------

CLIP = bytes(range(256)) * 40


@pytest.fixture
def media(server):
    make_app()
    root = media_root()
    (root / "clip.mp4").write_bytes(CLIP)
    (root / "notes.json").write_text('{"a": 1}')
    (root / "shots").mkdir()
    (root / "shots" / "one.png").write_bytes(b"\x89PNG....")
    return root


def test_a_media_file_is_served_whole(server, media):
    code, hdrs, data = get(server, "/app/sample/media/clip.mp4")
    assert code == 200 and data == CLIP
    assert hdrs["Content-Type"] == "video/mp4" and hdrs["Accept-Ranges"] == "bytes"
    assert hdrs["Content-Length"] == str(len(CLIP)) and hdrs["X-Content-Type-Options"] == "nosniff"
    assert hdrs["Cache-Control"].startswith("private") and "public" not in hdrs["Cache-Control"]
    assert hdrs["Content-Security-Policy"] == "default-src 'none'; sandbox"
    assert get(server, "/app/sample/media/shots/one.png")[1]["Content-Type"] == "image/png"
    assert get(server, "/app/sample/media/notes.json")[1]["Content-Type"] == "application/json"


@pytest.mark.parametrize("rng, want, span", [
    ("bytes=0-99", CLIP[:100], "0-99"),
    ("bytes=100-", CLIP[100:], f"100-{len(CLIP) - 1}"),
    ("bytes=-50", CLIP[-50:], f"{len(CLIP) - 50}-{len(CLIP) - 1}"),
    ("bytes=10000-99999999", CLIP[10000:], f"10000-{len(CLIP) - 1}"),
])
def test_a_byte_range_is_served_as_partial_content(server, media, rng, want, span):
    code, hdrs, data = get(server, "/app/sample/media/clip.mp4", headers={"Range": rng})
    assert code == 206 and data == want
    assert hdrs["Content-Range"] == f"bytes {span}/{len(CLIP)}" and hdrs["Content-Length"] == str(len(want))


def test_an_open_ended_range_is_cut_to_one_chunk(server, media, monkeypatch):
    monkeypatch.setattr(cockpit_apps, "MEDIA_CHUNK", 1000)
    code, hdrs, data = get(server, "/app/sample/media/clip.mp4", headers={"Range": "bytes=0-"})
    assert code == 206 and data == CLIP[:1000] and hdrs["Content-Range"] == f"bytes 0-999/{len(CLIP)}"


def test_an_unsatisfiable_range_is_416_and_a_strange_one_is_ignored(server, media):
    code, hdrs, data = get(server, "/app/sample/media/clip.mp4", headers={"Range": f"bytes={len(CLIP)}-"})
    assert code == 416 and hdrs["Content-Range"] == f"bytes */{len(CLIP)}" and data == b""
    for rng in ("bytes=0-1,5-9", "items=0-5", "bytes=9-3", "bytes=-"):
        code, _, data = get(server, "/app/sample/media/clip.mp4", headers={"Range": rng})
        assert code == 200 and data == CLIP, rng


def test_a_file_is_streamed_in_blocks_never_read_whole(server, media, monkeypatch):
    sizes = []
    real_fdopen = os.fdopen

    class Spy:
        def __init__(self, f):
            self.f = f

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.f.close()

        def __getattr__(self, name):
            return getattr(self.f, name)

        def read(self, n=-1):
            sizes.append(n)
            return self.f.read(n)
    monkeypatch.setattr(cockpit_apps, "READ_BLOCK", 1024)
    monkeypatch.setattr(os, "fdopen", lambda fd, *a, **k: Spy(real_fdopen(fd, *a, **k)))
    code, _, data = get(server, "/app/sample/media/clip.mp4")
    assert code == 200 and data == CLIP
    assert len(sizes) >= len(CLIP) // 1024 and all(0 < n <= 1024 for n in sizes)


@pytest.mark.parametrize("rel", [
    "../outside.json", "..%2Foutside.json", "%2e%2e/outside.json", "shots/../../outside.json", "shots/%2e%2e/%2e%2e/outside.json",
    ".secret.json", "shots/.hidden.png", ".git/config.json", "./clip.mp4", "shots//one.png", "%2Fetc%2Fhosts",
    "clip.mp4%00.png", "page.html", "drawing.svg", "run.py", "shots", "missing.mp4", "%ff.png", "a%5C..%5Coutside.json",
])
def test_media_outside_the_root_dotfiles_and_other_types_are_refused(server, media, rel):
    (media.parent / "outside.json").write_text('{"secret": true}')
    (media / ".secret.json").write_text("{}")
    (media / "shots" / ".hidden.png").write_bytes(b"x")
    (media / ".git").mkdir(exist_ok=True)
    (media / ".git" / "config.json").write_text("{}")
    (media / "page.html").write_text("<script>alert(1)</script>")
    (media / "drawing.svg").write_text("<svg/>")
    (media / "run.py").write_text("print(1)")
    code, _, data = get(server, "/app/sample/media/" + rel)
    assert code == 404 and b"secret" not in data and b"script" not in data


def test_symlinks_may_not_escape_the_root(server, media):
    secret = media.parent / "secret.json"
    secret.write_text('{"secret": true}')
    (media / "escape.json").symlink_to(secret)
    (media / "dir-escape").symlink_to(media.parent)
    (media / ".private.json").write_text('{"secret": true}')
    (media / "to-dotfile.json").symlink_to(media / ".private.json")
    (media / "alias.mp4").symlink_to(media / "clip.mp4")
    for rel in ("escape.json", "dir-escape/secret.json", "to-dotfile.json"):
        code, _, data = get(server, "/app/sample/media/" + rel)
        assert code == 404 and b"secret" not in data, rel
    code, _, data = get(server, "/app/sample/media/alias.mp4")
    assert code == 200 and data == CLIP  # a link that stays inside the root is fine


def test_directory_replaced_with_a_symlink_during_media_open_is_refused(server, media, monkeypatch):
    outside = media.parent / "outside"
    outside.mkdir()
    (outside / "one.png").write_bytes(b"outside secret")
    real_open = os.open
    swapped = []

    def replace_before_open(path, flags, *args, **kwargs):
        if path == "shots" and kwargs.get("dir_fd") is not None and not swapped:
            (media / "shots").rename(media / "original-shots")
            (media / "shots").symlink_to(outside, target_is_directory=True)
            swapped.append(True)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replace_before_open)
    code, _, data = get(server, "/app/sample/media/shots/one.png")
    assert swapped and code == 404 and b"outside secret" not in data


def test_media_stream_uses_the_checked_descriptor_even_after_directory_replacement(server, media, monkeypatch):
    outside = media.parent / "outside"
    outside.mkdir()
    (outside / "one.png").write_bytes(b"outside secret")
    real_media_file = cockpit_apps.media_file
    opened = []

    def replace_after_open(app, rel):
        found = real_media_file(app, rel)
        assert found is not None
        opened.append(found[0])
        (media / "shots").rename(media / "original-shots")
        (media / "shots").symlink_to(outside, target_is_directory=True)
        return found

    monkeypatch.setattr(cockpit_apps, "media_file", replace_after_open)
    real_close, closed = os.close, []
    monkeypatch.setattr(os, "close", lambda fd: (closed.append(fd), real_close(fd))[1])
    code, _, data = get(server, "/app/sample/media/shots/one.png")
    assert code == 200 and data == b"\x89PNG...."
    # ownership was transferred to the HTTP stream, which closed it (a number check alone races fd reuse)
    assert opened[0] in closed


def test_a_control_character_in_a_media_path_never_reaches_a_header(server, media):
    code, hdrs, _ = get(server, "/app/sample/media/clip.mp4%0d%0aX-Injected:%201")
    assert code == 404 and "X-Injected" not in hdrs


def test_an_app_without_a_media_root_serves_no_media(server):
    make_app(with_media=False)
    assert get(server, "/app/sample/media/clip.mp4")[0] == 404


# --- the API command -----------------------------------------------------------------------------

def test_the_api_runs_the_command_without_a_shell_from_the_app_folder(server, monkeypatch):
    monkeypatch.setenv("SAMPLE_SECRET", "do-not-pass")
    monkeypatch.setenv("CTB_BOT_TOKEN", GOOD_TOKEN)
    folder = make_app()
    code, hdrs, data = api(server, {"mode": "echo", "text": "שלום"})
    assert code == 200 and hdrs["Cache-Control"] == "no-store"
    out = json.loads(data)
    assert out["got"] == {"mode": "echo", "text": "שלום"}
    assert os.path.realpath(out["cwd"]) == os.path.realpath(folder)
    assert out["argv"] == ["--sample", "$(touch pwned)"] and not (folder / "pwned").exists()
    assert out["app"] == "sample" and out["secret"] is None and out["token"] is None


@pytest.mark.parametrize("body, status", [
    (b"", 400), (b"not json", 400), (b"[1, 2]", 400), (b'"text"', 400),
    (b"{" + b" " * cockpit_apps.API_BODY + b"}", 413),
])
def test_the_api_refuses_bad_or_oversized_bodies(server, body, status):
    make_app()
    code, _, data = api(server, body)
    assert code == status and "got" not in json.loads(data)


@pytest.mark.parametrize("mode, status", [("fail", 502), ("junk", 502), ("list", 502)])
def test_a_failing_command_gives_a_bare_error(server, mode, status):
    make_app()
    code, _, data = api(server, {"mode": mode})
    assert code == status and json.loads(data) == {"error": "app failed"}


def test_an_oversized_answer_is_refused(server, monkeypatch):
    monkeypatch.setattr(cockpit_apps, "API_OUT", 1000)
    make_app()
    code, _, data = api(server, {"mode": "big"})
    assert code == 502 and json.loads(data) == {"error": "too large"}


def test_a_slow_command_is_killed_with_its_children(server, monkeypatch):
    monkeypatch.setattr(cockpit_apps, "API_TIMEOUT", 1)
    folder = make_app()
    code, _, data = api(server, {"mode": "sleep"})
    assert code == 504 and json.loads(data) == {"error": "timeout"}
    pid = int((folder / "child.pid").read_text())
    for _ in range(40):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        os.kill(pid, 9)
        pytest.fail("the command's child outlived the timeout")


def test_an_app_without_a_command_has_no_api(server):
    make_app(with_command=False)
    assert api(server, {"mode": "echo"})[0] == 404


# --- the panel page ------------------------------------------------------------------------------

def test_the_panel_page_frames_apps_safely():
    page = panel_page.PAGE
    assert 'f.setAttribute("sandbox","allow-scripts allow-forms")' in page and "allow-same-origin" not in page
    assert 'f.setAttribute("referrerpolicy","no-referrer")' in page
    assert '"/app/"+name+"/?auth="+encodeURIComponent(wa.initData)' in page
    handler = page[page.index("function onAppMessage("):]
    assert 'frameName(e.source)' in handler and 'e.origin!=="null"' in handler
    assert 'm.cockpit==="hello"' in handler and 'm.cockpit!=="api"' in handler
    assert '"X-Telegram-Init-Data":wa.initData' in handler  # the API call goes through the panel, same origin
    assert ".concat(appTabs(d))" in page and "showApp(d);" in page
    assert "frame-src 'self'" in panel_page.csp("n", panel.FRAME_ANCESTORS)
    he, en = panel_page.STRINGS["he"], panel_page.STRINGS["en"]
    assert he["appFrame"].strip() and en["appFrame"].strip()


def test_the_icon_names_come_from_the_page():
    assert {"box", "chart", "layers", "list", "mic"} <= panel_page.ICON_NAMES
    assert all(name.isalpha() and name.islower() for name in panel_page.ICON_NAMES)


def test_no_sample_app_ships_in_the_package():
    src = Path(cockpit_apps.__file__).parent
    assert not any(p.name == "cockpit-apps" or p.name == "manifest.json" for p in src.rglob("*"))
