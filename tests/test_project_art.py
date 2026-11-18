"""The owner's own project pictures: validation, the file guards, the owner-only routes, precedence over scenes, the CLI."""
import base64
import io
import json
import os
from types import SimpleNamespace

import pytest

from ctb import cli, cockpit_v2, panel, project_art
from ctb.config import ProjectConfig
from test_cockpit_v2 import handler
from test_panel import signed

try:
    from PIL import Image as PIL
except ImportError:  # the picture store works without Pillow; only the re-encode tests need it
    PIL = None

needs_pillow = pytest.mark.skipif(PIL is None, reason="Pillow not installed")
BELL = (cockpit_v2.ASSET_ROOT / "bell.webp").read_bytes()  # a real, small webp that needs no Pillow


def image(fmt="WEBP", size=(40, 20), color=(200, 30, 30)):
    if PIL is None:
        assert fmt == "WEBP"
        return BELL
    out = io.BytesIO()
    PIL.new("RGB", size, color).save(out, fmt)
    return out.getvalue()


def webp(size=(512, 512)):
    return image("WEBP", size)


def b64(data):
    return base64.b64encode(data).decode()


def post(body, init=None, path="/api/project-art"):
    h, replies = handler(path, "POST", body, signed() if init is None else init)
    h.do_POST()
    return replies[0][0], replies[0][1], replies[0][2]


# --- validation -------------------------------------------------------------------------------------------------

@needs_pillow
@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP"])
def test_every_accepted_type_becomes_a_square_512_webp(fmt):
    out = project_art.validate(image(fmt, size=(300, 120)))
    assert project_art.kind_of(out) == "webp"
    with PIL.open(io.BytesIO(out)) as img:
        assert img.size == (512, 512)


@pytest.mark.parametrize("data,code", [
    (b"GIF89a" + b"\0" * 40, "type"), (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "type"),
    (b"hello world", "type"), (b"%PDF-1.7 ...", "type"), (b"", "bad")])
def test_wrong_type_is_refused_by_magic_bytes(data, code):
    with pytest.raises(project_art.ArtError) as exc:
        project_art.validate(data)
    assert exc.value.code == code


def test_oversize_is_refused_even_when_the_type_is_right():
    with pytest.raises(project_art.ArtError) as exc:
        project_art.validate(webp() + b"\0" * project_art.MAX_BYTES)
    assert exc.value.code == "size"


@needs_pillow
def test_truncated_image_with_good_magic_is_refused():
    with pytest.raises(project_art.ArtError) as exc:
        project_art.validate(image("PNG")[:30])
    assert exc.value.code == "decode"


def test_without_pillow_or_sips_only_a_small_webp_is_stored(monkeypatch):
    monkeypatch.setattr(project_art, "_pillow", lambda: None)
    monkeypatch.setattr(project_art.shutil, "which", lambda name: None)
    data = BELL
    assert project_art.validate(data) == data
    png = b"\x89PNG\r\n\x1a\n" + b"\0" * 30
    for other in (png, b"\xff\xd8\xff\xe0" + b"\0" * 30):
        with pytest.raises(project_art.ArtError) as exc:
            project_art.validate(other)
        assert exc.value.code == "needs_pillow"
    with pytest.raises(project_art.ArtError) as exc:
        project_art.validate(b"junk")
    assert exc.value.code == "type"


JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 16 + b"\xff\xd9"


def fake_sips(monkeypatch, out=JPEG, code=0, seen=None):
    """A stand-in for macOS sips: writes `out` where --out points, and records its arguments."""
    monkeypatch.setattr(project_art, "_pillow", lambda: None)
    monkeypatch.setattr(project_art.shutil, "which", lambda name: "/usr/bin/sips")
    monkeypatch.setattr(project_art.card_media, "raster", lambda data, dims=None: (dims.extend([1200, 800]) or (data, None)))

    def run(args, **kw):
        if seen is not None:
            seen.extend(args)
        if not code:
            open(args[args.index("--out") + 1], "wb").write(out)
        return SimpleNamespace(returncode=code)
    monkeypatch.setattr(project_art.subprocess, "run", run)


def test_a_jpeg_without_pillow_is_resized_by_sips_to_a_square_jpeg(monkeypatch):
    seen = []
    fake_sips(monkeypatch, seen=seen)
    got = project_art.validate(b"\xff\xd8\xff\xe0" + b"\0" * 40)
    assert got == JPEG
    # 1200x800 scales so the short side is 512 (768 wide), then the centre 512 square is cut.
    assert seen[seen.index("--resampleHeightWidth") + 1:seen.index("--resampleHeightWidth") + 3] == ["512", "768"]
    assert seen[seen.index("-c") + 1:seen.index("-c") + 3] == ["512", "512"]


def test_a_png_without_pillow_goes_through_sips_and_is_served_as_a_jpeg(monkeypatch):
    fake_sips(monkeypatch)
    png = b"\x89PNG\r\n\x1a\n" + b"\0" * 40
    status, body, _ = post({"action": "set", "project": "alpha", "data": b64(png)})
    assert status == 200
    status, data, ctype = post({"name": "alpha"}, path="/api/project-art-image")
    assert status == 200 and ctype == "image/jpeg" and data == JPEG


def test_sips_failures_and_oversize_output_are_refused(monkeypatch):
    data = b"\xff\xd8\xff\xe0" + b"\0" * 40
    fake_sips(monkeypatch, code=1)
    with pytest.raises(project_art.ArtError) as exc:
        project_art.validate(data)
    assert exc.value.code == "decode"
    fake_sips(monkeypatch, out=b"not an image")
    with pytest.raises(project_art.ArtError) as exc:
        project_art.validate(data)
    assert exc.value.code == "decode"
    fake_sips(monkeypatch, out=JPEG + b"\0" * project_art.MAX_BYTES)
    with pytest.raises(project_art.ArtError) as exc:
        project_art.validate(data)
    assert exc.value.code == "size"


def test_a_jpeg_without_pillow_or_sips_is_refused_with_a_clear_code(monkeypatch):
    monkeypatch.setattr(project_art, "_pillow", lambda: None)
    monkeypatch.setattr(project_art.shutil, "which", lambda name: None)
    with pytest.raises(project_art.ArtError) as exc:
        project_art.validate(b"\xff\xd8\xff\xe0" + b"\0" * 40)
    assert exc.value.code == "needs_pillow"
    assert post({"action": "set", "project": "alpha", "data": b64(b"\xff\xd8\xff\xe0" + b"\0" * 40)})[0] == 415


@needs_pillow
def test_an_image_with_too_many_pixels_is_refused_even_when_small():
    out = io.BytesIO()
    PIL.new("L", (7000, 7000)).save(out, "PNG")  # 49 megapixels, a few dozen kilobytes
    assert len(out.getvalue()) < project_art.MAX_BYTES
    with pytest.raises(project_art.ArtError) as exc:
        project_art.validate(out.getvalue())
    assert exc.value.code == "decode"


def test_the_version_changes_with_every_rewrite_even_within_one_second():
    first = project_art.store("alpha", image())
    path = project_art.folder() / "alpha.webp"
    os.utime(path, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_001))
    second = project_art.stamp("alpha")
    os.utime(path, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_002))
    third = project_art.stamp("alpha")
    assert isinstance(first["v"], str) and len({first["v"], second["v"], third["v"]}) == 3


def test_hebrew_picture_messages_isolate_the_english_words():
    from ctb import panel_page
    he = panel_page.V2_STRINGS["he"]["art"]
    for key, words in (("badType", ("webp", "png", "jpeg")), ("needsPillow", ("webp",))):
        for w in words:
            assert "\u2068" + w + "\u2069" in he[key]


def test_store_writes_a_private_file_and_clear_removes_it():
    stamp = project_art.store("Alpha", image())
    assert stamp["name"] == "alpha" and project_art.stamp("ALPHA") == stamp
    path = project_art.folder() / "alpha.webp"
    assert path.stat().st_mode & 0o777 == 0o600 and project_art.folder().stat().st_mode & 0o777 == 0o700
    assert project_art.clear("alpha") is True and project_art.stamp("alpha") is None
    assert project_art.clear("alpha") is False


def test_names_are_strict_and_odd_project_names_are_hashed():
    assert project_art.name_for("Studio") == "studio"
    for odd in ("../etc/passwd", "פרויקט א", "a/b", "has space", "x" * 59 + "é"):
        name = project_art.name_for(odd)
        assert project_art.NAME.fullmatch(name) and name.startswith("_")
    assert project_art.name_for("פרויקט א") == project_art.name_for(" פרויקט  א ".strip())
    assert project_art.name_for("x" * 80) == ""  # the bridge's own limit on project names
    assert project_art.name_for("") == "" and project_art.name_for(None) == ""


# --- the file guards --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["../x", "..", "a/b", "/etc/passwd", "ALPHA", "alpha.webp", "alpha\0", "", "-a", "a" * 64,
                                  "_zz", "alpha/../beta", None, 5])
def test_read_refuses_every_name_off_the_pattern(name):
    project_art.store("alpha", image())
    with pytest.raises(OSError):
        project_art.read(name)


def test_read_returns_the_stored_bytes():
    project_art.store("alpha", image())
    assert project_art.kind_of(project_art.read("alpha")) == "webp"


def test_read_and_stamp_refuse_a_symlinked_file(tmp_path):
    outside = tmp_path / "secret.webp"
    outside.write_bytes(webp())
    project_art.folder().mkdir(parents=True, exist_ok=True)
    (project_art.folder() / "alpha.webp").symlink_to(outside)
    with pytest.raises(OSError):
        project_art.read("alpha")
    assert project_art.stamp("alpha") is None


def test_read_refuses_a_symlinked_folder(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "alpha.webp").write_bytes(webp())
    folder = project_art.folder()
    folder.parent.mkdir(parents=True, exist_ok=True)
    folder.symlink_to(real, target_is_directory=True)
    with pytest.raises(OSError):
        project_art.read("alpha")
    assert project_art.stamp("alpha") is None


def test_read_refuses_non_regular_and_oversize_files():
    folder = project_art.folder()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "dir.webp").mkdir()
    os.mkfifo(folder / "pipe.webp")
    (folder / "big.webp").write_bytes(b"\0" * (project_art.MAX_BYTES + 1))
    for name in ("dir", "pipe", "big"):
        with pytest.raises(OSError):
            project_art.read(name)
        assert project_art.stamp(name) is None


def test_clear_removes_a_link_without_following_it(tmp_path):
    outside = tmp_path / "keep.webp"
    outside.write_bytes(webp())
    project_art.folder().mkdir(parents=True, exist_ok=True)
    (project_art.folder() / "alpha.webp").symlink_to(outside)
    assert project_art.clear("alpha") is True
    assert outside.exists()


# --- the routes -------------------------------------------------------------------------------------------------

def test_set_then_read_back_through_the_owner_routes():
    status, body, _ = post({"action": "set", "project": "Alpha", "data": b64(image())})
    assert status == 200 and json.loads(body)["art"]["name"] == "alpha"
    status, data, ctype = post({"name": "alpha"}, path="/api/project-art-image")
    assert status == 200 and ctype == "image/webp" and project_art.kind_of(data) == "webp"


@pytest.mark.parametrize("init", ["", "app1.sample.1.0.invalid", signed(user_id=8), signed(tamper=True)])
def test_only_the_owner_can_change_or_read_a_picture(init):
    project_art.store("alpha", image())
    for path, body in [("/api/project-art", {"action": "set", "project": "alpha", "data": b64(image())}),
                       ("/api/project-art", {"action": "clear", "project": "alpha"}),
                       ("/api/project-art", {"action": "scene", "project": "alpha", "scene": "plane"}),
                       ("/api/project-art-image", {"name": "alpha"})]:
        status, data, _ = post(body, init, path)
        assert status == 403 and b"RIFF" not in data
    assert project_art.stamp("alpha")


def test_the_routes_refuse_a_stale_login_for_changes():
    old = signed(auth_date=1)
    status, _, _ = post({"action": "clear", "project": "alpha"}, old)
    assert status == 403


def test_wrong_type_and_oversize_uploads_get_precise_errors():
    assert post({"action": "set", "project": "alpha", "data": b64(b"GIF89a" + b"\0" * 50)})[0] == 415
    assert post({"action": "set", "project": "alpha", "data": b64(webp() + b"\0" * project_art.MAX_BYTES)})[0] == 413
    assert post({"action": "set", "project": "alpha", "data": "x" * 1_450_000})[0] == 413
    assert post({"action": "set", "project": "alpha", "data": "not base64!!"})[0] == 400
    assert post({"action": "set", "project": "alpha", "data": 5})[0] == 400
    assert project_art.stamp("alpha") is None


@pytest.mark.parametrize("body", [
    {"action": "set", "project": "alpha"}, {"action": "nope", "project": "alpha"}, {"action": "clear"},
    {"action": "clear", "project": ""}, {"action": "clear", "project": 5}, {"action": "clear", "project": "a" * 300},
    {"action": "clear", "project": "alpha", "extra": 1}, {"action": "scene", "project": "alpha", "scene": 7}])
def test_malformed_requests_are_refused(body):
    assert post(body)[0] == 400


def test_unknown_scene_is_not_found_and_a_scene_can_be_picked():
    assert post({"action": "scene", "project": "alpha", "scene": "../../config"})[0] == 404
    assert post({"action": "scene", "project": "alpha", "scene": "header"})[0] == 404
    status, body, _ = post({"action": "scene", "project": "alpha", "scene": "plane"})
    assert status == 200 and json.loads(body)["art"]["name"] == "alpha"
    assert project_art.kind_of(project_art.read("alpha")) == "webp"


def test_clear_goes_back_to_automatic():
    project_art.store("alpha", image())
    status, body, _ = post({"action": "clear", "project": "alpha"})
    assert status == 200 and json.loads(body)["art"] is None and project_art.stamp("alpha") is None


@pytest.mark.parametrize("name", ["../alpha", "alpha/..", "ALPHA", "alpha.webp", "", "a" * 70, "/etc/passwd"])
def test_image_route_refuses_path_traversal_and_bad_names(name):
    project_art.store("alpha", image())
    status, data, _ = post({"name": name}, path="/api/project-art-image")
    assert status == 404 and b"RIFF" not in data


def test_image_route_refuses_a_symlink_and_a_missing_file(tmp_path):
    outside = tmp_path / "secret.webp"
    outside.write_bytes(webp())
    project_art.folder().mkdir(parents=True, exist_ok=True)
    (project_art.folder() / "alpha.webp").symlink_to(outside)
    assert post({"name": "alpha"}, path="/api/project-art-image")[0] == 404
    assert post({"name": "beta"}, path="/api/project-art-image")[0] == 404
    assert post({"name": "alpha", "x": 1}, path="/api/project-art-image")[0] == 404


def test_the_art_url_is_not_a_public_asset():
    project_art.store("alpha", image())
    for path in ("/project-art/alpha.webp", cockpit_v2.ASSET_PREFIX + "alpha.webp", cockpit_v2.ASSET_PREFIX + "../project-art/alpha.webp"):
        h, replies = handler(path)
        h.do_GET()
        assert replies[0][0] == 404


# --- the tile: fallback and precedence --------------------------------------------------------------------------

def tiles(names):
    state = {"cards": [], "requests": [], "decisions": [], "sessions": [{"id": "s%07d" % i, "name": n, "state": "idle", "age": 5}
                                                                  for i, n in enumerate(names)], "brief": None}
    cfg = SimpleNamespace(projects=[], brief={})
    entries = []
    return {p["key"]: p for p in cockpit_v2.presentation(cfg, state, entries, 1000)["projects"]}


def test_without_a_custom_file_the_scene_logic_is_unchanged():
    before = {k: p["asset"] for k, p in tiles(["alpha", "bravo", "charlie"]).items()}
    assert all(p["art"] is None for p in tiles(["alpha", "bravo", "charlie"]).values())
    assert before == {k: p["asset"] for k, p in tiles(["alpha", "bravo", "charlie"]).items()}
    assert all(a in cockpit_v2.SCENES for a in before.values())


def test_a_custom_file_wins_over_the_scene_and_only_for_its_project():
    scenes = {k: p["asset"] for k, p in tiles(["alpha", "bravo"]).items()}
    project_art.store("alpha", image())
    got = tiles(["alpha", "bravo"])
    assert got["alpha"]["art"]["name"] == "alpha" and got["bravo"]["art"] is None
    assert {k: p["asset"] for k, p in got.items()} == scenes  # the scene stays as the fallback
    project_art.clear("alpha")
    assert tiles(["alpha", "bravo"])["alpha"]["art"] is None


def test_a_broken_custom_file_falls_back_to_the_scene(tmp_path):
    project_art.folder().mkdir(parents=True, exist_ok=True)
    (project_art.folder() / "alpha.webp").symlink_to(tmp_path / "nowhere.webp")
    assert tiles(["alpha"])["alpha"]["art"] is None


def test_decisions_and_cards_carry_the_project_picture():
    project_art.store("alpha", image())
    cfg = SimpleNamespace(projects=[], brief={})
    entry = SimpleNamespace(session="s0000000", chat=1, name="alpha: task")
    state = {"cards": [], "requests": [], "sessions": [], "brief": None,
             "decisions": [dict(id="1" * 16, topic="alpha: task", url="", stale="")]}
    out = cockpit_v2.presentation(cfg, state, [entry], 1000)
    assert state["decisions"][0]["project_art"]["name"] == "alpha"
    assert out["projects"][0]["art"]["name"] == "alpha"


def test_the_page_ships_the_picker_with_hebrew_strings():
    from ctb import panel_page
    page = panel_page.render_page("he", "n").decode()
    for needle in ("v2ArtEdit", "/api/project-art", "data-v2-art", "v2-art-sheet"):
        assert needle in page
    art = panel_page.V2_STRINGS["he"]["art"]
    assert art["upload"] and art["scene"] and art["auto"] and art["approve"]
    assert panel_page.V2_STRINGS["he"]["art"].keys() == panel_page.V2_STRINGS["en"]["art"].keys()


# --- the CLI ----------------------------------------------------------------------------------------------------

def test_cli_set_clear_and_missing(tmp_path, monkeypatch, capsys):
    cfg = SimpleNamespace(projects=[ProjectConfig(name="Alpha", chat_id=-1), ProjectConfig(name="Bravo", chat_id=-2)],
                          brief={})
    monkeypatch.setattr(cli.C, "load", lambda *a, **k: cfg)
    src = tmp_path / "pic.webp"
    src.write_bytes(image())
    assert cli.main(["art", "missing"]) == 0
    assert capsys.readouterr().out.split() == ["Alpha", "Bravo"]
    assert cli.main(["art", "set", "Alpha", str(src)]) == 0
    assert project_art.stamp("alpha")
    capsys.readouterr()
    assert cli.main(["art", "missing"]) == 0
    assert capsys.readouterr().out.split() == ["Bravo"]
    assert cli.main(["art", "missing", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == [{"project": "Bravo", "key": "bravo"}]
    assert cli.main(["art", "clear", "Alpha"]) == 0 and project_art.stamp("alpha") is None


def test_cli_refuses_a_bad_file_and_a_missing_one(tmp_path, capsys):
    bad = tmp_path / "x.gif"
    bad.write_bytes(b"GIF89a" + b"\0" * 20)
    assert cli.main(["art", "set", "alpha", str(bad)]) == 2
    assert cli.main(["art", "set", "alpha", str(tmp_path / "none.png")]) == 1
    assert cli.main(["art", "set", "", str(bad)]) == 2
    assert project_art.stamp("alpha") is None


def test_the_image_read_is_a_read_only_route_so_it_keeps_the_state_cache_and_the_longer_login_age():
    import time
    assert "/api/project-art-image" in panel.READ_ONLY_ROUTES and "/api/project-art" not in panel.READ_ONLY_ROUTES
    project_art.store("alpha", image())
    aged = signed(auth_date=int(time.time()) - panel.DECIDE_MAX_AGE - 60)
    assert aged and panel.DECIDE_MAX_AGE < panel.MAX_AGE
    assert post({"name": "alpha"}, aged, "/api/project-art-image")[0] == 200
    assert post({"action": "clear", "project": "alpha"}, aged)[0] == 403
