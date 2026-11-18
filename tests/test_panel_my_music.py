import json
from types import SimpleNamespace

from ctb import panel_my_music


class Response:
    def __init__(self, value): self.value = value
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self, _limit): return json.dumps(self.value).encode()


def test_catalogue_is_bounded_cached_and_never_returns_token():
    panel_my_music.clear_cache()
    calls, token = [], "very-secret-access-token"
    def runner(*_args, **_kwargs): return SimpleNamespace(stdout=token + "\n", returncode=0)
    def opener(request, timeout):
        calls.append((request.full_url, request.headers, timeout))
        if "playlists?" in request.full_url:
            rows = [{"id": "PL" + str(i), "snippet": {"title": "List " + str(i), "thumbnails": {"default": {"url": "https://i.ytimg.com/a.jpg"}}}, "contentDetails": {"itemCount": i}} for i in range(80)]
            return Response({"items": rows})
        return Response({"items": [{"snippet": {"title": "Liked", "thumbnails": {"default": {"url": "https://evil.example/a.jpg"}}}, "contentDetails": {"videoId": "vid"}}]})
    cfg = SimpleNamespace(youtube={"account_token_command": ["token-helper"]})
    first = panel_my_music.load(cfg, now=1, runner=runner, opener=opener)
    second = panel_my_music.load(cfg, now=2, runner=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError()), opener=opener)
    assert first is second and first["ok"] and len(first["playlists"]) == panel_my_music.MAX_PLAYLISTS
    assert first["liked_music"][0]["thumbnail_url"] == ""
    assert token not in json.dumps(first)
    assert len(calls) == 2


def test_missing_helper_is_a_safe_error():
    panel_my_music.clear_cache()
    assert panel_my_music.load(SimpleNamespace(youtube={}), now=1) == {"ok": False, "reason": "not_configured"}
