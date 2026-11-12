"""Exercise the real media request/stream handlers without opening a network listener."""
import io
import os
from types import SimpleNamespace

import pytest

from ctb import cockpit_apps, panel
from ctb.studio import safefs


@pytest.fixture
def media_request(tmp_path, monkeypatch):
    root = tmp_path / "media"
    shots = root / "shots"
    shots.mkdir(parents=True)
    (shots / "one.png").write_bytes(b"original image")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "one.png").write_bytes(b"outside secret")

    app = SimpleNamespace(media_root=root)
    monkeypatch.setattr(cockpit_apps, "find", lambda name: app)
    monkeypatch.setattr(panel, "validate_init_data", lambda *a, **kw: True)
    from ctb import policy
    monkeypatch.setattr(policy, "on", lambda: True)
    handler = panel.PanelHandler.__new__(panel.PanelHandler)
    handler.path = "/app/sample/media/shots/one.png"
    handler.command = "GET"
    handler.headers = {}
    handler.server = SimpleNamespace(bot_token="unused", owner_id=1)
    handler.wfile = io.BytesIO()
    handler._extend = lambda seconds: None
    responses = []
    handler._json = lambda code, data: responses.append(code)
    handler.send_response = responses.append
    handler.send_header = lambda *a: None
    handler.end_headers = lambda: None
    return handler, responses, root, outside


def replace_shots(root, outside):
    (root / "shots").rename(root / "original-shots")
    (root / "shots").symlink_to(outside, target_is_directory=True)


def test_http_media_refuses_an_intermediate_symlink_planted_after_path_validation(media_request, monkeypatch):
    handler, responses, root, outside = media_request
    real_open = safefs.open_readonly

    def swap_before_descriptor_walk(media_root, relative):
        replace_shots(root, outside)
        return real_open(media_root, relative)

    monkeypatch.setattr(safefs, "open_readonly", swap_before_descriptor_walk)
    handler._app_get()
    assert responses == [404] and handler.wfile.getvalue() == b""


@pytest.mark.parametrize("range_header, code, data", [
    (None, 200, b"original image"), ("bytes=0-3", 206, b"orig"), ("bytes=999-", 416, b"")])
def test_http_media_stream_keeps_and_closes_the_verified_file_after_directory_swap(
        media_request, monkeypatch, range_header, code, data):
    handler, responses, root, outside = media_request
    real_media_file = cockpit_apps.media_file
    opened = []
    if range_header:
        handler.headers["Range"] = range_header

    def swap_after_descriptor_walk(app, relative):
        found = real_media_file(app, relative)
        assert found is not None
        opened.append(found[0])
        replace_shots(root, outside)
        return found

    monkeypatch.setattr(cockpit_apps, "media_file", swap_after_descriptor_walk)
    handler._app_get()
    assert responses == [code] and handler.wfile.getvalue() == data
    with pytest.raises(OSError):
        os.fstat(opened[0])
