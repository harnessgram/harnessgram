"""The generic adapter boundary uses fake data only; private paths/assets never enter the repository."""
import io
import json
import subprocess
import sys
import os
from pathlib import Path
from types import SimpleNamespace
import pytest

from ctb import config, cockpit_apps, panel_page
from ctb.studio import adapter, api, install, provider


@pytest.fixture
def fake(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter, "_cached", None)
    monkeypatch.setattr(adapter, "_memory_limit_failed", False)
    monkeypatch.setattr(adapter, "MEDIA_ROOT_OVERRIDE", None)
    cfg = tmp_path / "settings.json"
    monkeypatch.setattr(adapter, "CONFIG_PATH", cfg)
    root = tmp_path / "media"
    root.mkdir()
    module = tmp_path / "local_adapter.py"
    desk = provider.sample()
    desk["sample"] = False
    project = provider._leaves()
    project["sample"] = False

    def write(d=desk, p=project, source=None, extra=None):
        code = source if source is not None else (
            f"MEDIA_ROOT = {str(root)!r}\n"
            f"def desk():\n    return {d!r}\n"
            f"def project(pid):\n    return {p!r} if pid == 'weekly-12' else None\n")
        module.write_text(code, encoding="utf-8")
        cfg.write_text(json.dumps({"studio_adapter": str(module), **(extra or {})}), encoding="utf-8")
        return module

    return write, desk, project, root, cfg


@pytest.fixture(scope="module")
def memory_limits_supported():
    # Probe in a child: never constrain pytest itself. Some macOS/sandbox combinations reject both limits.
    probe = subprocess.run([sys.executable, "-c", f'''
import resource
for name in ("RLIMIT_AS", "RLIMIT_DATA"):
    try:
        kind = getattr(resource, name)
        soft, hard = resource.getrlimit(kind)
        cap = min([{adapter.MEMORY_LIMIT}] + [n for n in (soft, hard) if n != resource.RLIM_INFINITY])
        resource.setrlimit(kind, (cap, hard))
    except (AttributeError, OSError, ValueError):
        continue
    raise SystemExit(0)
raise SystemExit(1)
'''], capture_output=True, timeout=5)
    return probe.returncode == 0


def test_unconfigured_adapter_keeps_the_sample(fake):
    _, _, _, _, cfg = fake
    cfg.write_text('{}')
    assert provider.desk() == provider.sample()
    assert provider.project("weekly-12")["sample"] is True
    assert provider.project("missing") is None


def test_valid_adapter_desk_project_and_media(fake):
    write, desk, project, root, _ = fake
    (root / "frame.png").write_bytes(b"existing image")
    (root / "sentence.wav").write_bytes(b"existing clip")
    project["scenes"][0]["frame"] = "frame.png"
    project["sentences"][0].update(frame="frame.png", audio="sentence.wav")
    desk["private_extra"] = "must not leave the adapter"
    project["private_extra"] = "must not leave the adapter"
    write()
    got = provider.desk()
    assert got["sample"] is False and got["waiting"] == desk["waiting"]
    assert "private_extra" not in got
    got = api.handle({"op": "project.get", "project": "weekly-12"})["project"]
    assert got["sample"] is False and "private_extra" not in got
    assert got["scenes"][0]["frame"] == "frame.png"
    assert got["sentences"][0]["audio"] == "sentence.wav"
    assert provider.project("missing") is None


def test_malformed_items_are_dropped_and_unknown_fields_are_removed(fake):
    write, desk, project, _, _ = fake
    desk["waiting"].extend([None, {}, {**desk["waiting"][0], "id": "bad", "length_s": float('nan')}])
    desk["working"].append({**desk["working"][0], "id": "wrong", "pct": True})
    desk["series"].extend([{}, {"id": [], "name": "bad"}])
    desk["shelf"].append({**desk["shelf"][0], "id": "unknown-series", "series": "missing"})
    project["sentences"][1]["said"] = []
    project["sentences"][2]["scene_i"] = 999
    # repr(float('nan')) is not a Python literal, so use JSON via the adapter's own decoder.
    code = ("import json\n"
            f"def desk():\n    return json.loads({json.dumps(desk)!r})\n"
            f"def project(pid):\n    return {project!r}\n")
    write(source=code)
    got = provider.desk()
    assert got["sample"] is False and len(got["waiting"]) == 2 and len(got["working"]) == 1
    assert len(got["series"]) == 4 and len(got["shelf"]) == 6
    p = provider.project("weekly-12")
    assert len(p["sentences"]) == len(project["sentences"]) - 2
    assert [s["i"] for s in p["sentences"]] == [s["i"] for i, s in enumerate(project["sentences"]) if i not in (1, 2)]


@pytest.mark.parametrize("source", [
    "raise RuntimeError('/private/secret')",
    "def desk():\n    raise RuntimeError('/private/secret')\ndef project(pid):\n    raise RuntimeError('/private/secret')",
    "def desk():\n    return []\ndef project(pid):\n    return []",
    "def desk():\n    return {'sample': False}\ndef project(pid):\n    return {'id': 'wrong'}",
    "def desk():\n    return {}",  # missing project callable
])
def test_adapter_errors_return_a_labelled_sample_without_private_details(fake, source):
    write, _, _, _, _ = fake
    write(source=source)
    for got in (provider.desk(), provider.project("weekly-12"), provider.project("real-production")):
        assert got["sample"] is True and got["notice"]["en"]
        title = got["waiting"][0]["title"] if "waiting" in got else got["title"]
        assert title == got["notice"]  # visible through the existing page's title slot
        assert "/private/secret" not in json.dumps(got)


def test_fallback_sample_cannot_write_marks_for_real_productions(fake, tmp_path):
    write, _, _, _, _ = fake
    write(source="raise RuntimeError('failed')")
    folder = tmp_path / "app"
    folder.mkdir()
    got = api.handle({"op": "marks.set", "project": "real-production", "i": 1, "mark": "remove"}, folder)
    assert got == {"ok": False, "error": "unavailable"}
    assert not (folder / "state").exists()


def test_fallback_sample_never_displays_real_saved_marks(fake, tmp_path):
    write, _, _, _, _ = fake
    write(source="raise RuntimeError('failed')")
    state = tmp_path / "app" / "state"
    state.mkdir(parents=True)
    (state / "marks.json").write_text(json.dumps({"real-production": {"1": {"mark": "remove"}}}))
    got = api.handle({"op": "project.get", "project": "real-production"}, tmp_path / "app")
    assert got["project"]["sample"] is True and got["marks"] == {}


@pytest.mark.parametrize("path", ["/etc/passwd", "../frame.png", "sub/../../frame.png", "sub//frame.png",
                                  "sub/./frame.png", ".hidden.png", "sub\\frame.png", "file\x00.png",
                                  "https://example.test/frame.png", "missing.png", "file.txt", "dir.png"])
def test_media_path_escape_and_non_media_are_dropped(fake, path):
    write, _, project, root, _ = fake
    (root / "dir.png").mkdir()
    (root / "file.txt").write_text("plain text")
    project["scenes"][0]["frame"] = path
    project["sentences"][0].update(frame=path, audio=path)
    write()
    p = provider.project("weekly-12")
    assert p["sample"] is False
    assert p["scenes"][0]["frame"] is None
    assert p["sentences"][0]["frame"] is None and p["sentences"][0]["audio"] is None


def test_media_file_and_directory_symlinks_are_refused(fake, tmp_path):
    write, _, project, root, _ = fake
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "frame.png").write_bytes(b"not in media root")
    (root / "link.png").symlink_to(outside / "frame.png")
    (root / "linked").symlink_to(outside, target_is_directory=True)
    project["scenes"][0]["frame"] = "link.png"
    project["sentences"][0]["frame"] = "linked/frame.png"
    write()
    p = provider.project("weekly-12")
    assert p["scenes"][0]["frame"] is None and p["sentences"][0]["frame"] is None
    assert sorted(p.name for p in outside.iterdir()) == ["frame.png"]


def test_malformed_scene_rows_preserve_sentence_scene_references(fake):
    write, _, project, _, _ = fake
    project["scenes"][0] = None
    project["sentences"][2] = None
    write()
    p = provider.project("weekly-12")
    assert p["sample"] is False
    assert p["scenes"][0]["title"] == project["scenes"][1]["title"]
    assert p["sentences"][0]["scene_i"] == 0 and p["sentences"][0]["start"] == 14


def test_dropping_first_sentence_never_moves_saved_remove_from_second_to_third(fake, tmp_path):
    write, _, project, _, _ = fake
    folder = tmp_path / "app"
    state = folder / "state"
    state.mkdir(parents=True)
    (state / "marks.json").write_text(json.dumps({"weekly-12": {"2": {"mark": "remove"}}}))
    project["sentences"][0]["said"] = None
    write()

    got = api.handle({"op": "project.get", "project": "weekly-12"}, folder)
    assert got["project"]["sample"] is False
    assert [(s["i"], s["said"]) for s in got["project"]["sentences"][:2]] == [
        (2, project["sentences"][1]["said"]), (3, project["sentences"][2]["said"])]
    assert got["marks"] == {"2": {"mark": "remove"}}


def test_missing_and_invalid_adapter_config_falls_back(fake):
    write, _, _, _, cfg = fake
    module = write()
    module.unlink()
    assert provider.desk()["notice"] == adapter.REASONS["load"]
    cfg.write_text('{bad')
    assert provider.desk()["notice"] == adapter.REASONS["config"]


def test_adapter_module_reloads_when_source_changes(fake):
    write, desk, _, _, _ = fake
    write()
    assert provider.desk()["sample"] is False
    desk["waiting"] = []
    write()
    assert provider.desk()["waiting"] == []


def test_adapter_stdout_does_not_corrupt_api_json(fake):
    write, desk, project, _, _ = fake
    write(source=f"print('diagnostic')\ndef desk():\n    print('diagnostic')\n    return {desk!r}\n"
                 f"def project(pid):\n    return {project!r}\n")
    output = io.StringIO()
    api.main(io.StringIO('{"op":"desk.get"}'), output)
    assert json.loads(output.getvalue())["desk"]["sample"] is False


def test_adapter_import_and_calls_discard_both_diagnostic_streams(fake, capsys):
    write, desk, project, _, _ = fake
    noise = ("import sys\n"
             "def noisy():\n"
             "    for stream in (sys.stdout, sys.stderr):\n"
             "        assert not hasattr(stream, 'getvalue')\n"
             "        for _ in range(32):\n"
             "            stream.write('x' * 65536)\n")
    write(source=noise + f"noisy()\ndef desk():\n    noisy()\n    return {desk!r}\n"
                        f"def project(pid):\n    noisy()\n    return {project!r}\n")
    assert provider.desk()["sample"] is False
    assert provider.project("weekly-12")["sample"] is False
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("during_import", [False, True])
def test_memory_exhaustion_falls_back_with_the_call_reason(fake, during_import):
    write, _, _, _, _ = fake
    write(source=("raise MemoryError('private allocation details')" if during_import else
                  "def desk():\n    raise MemoryError('private allocation details')\n"
                  "def project(pid):\n    raise MemoryError('private allocation details')\n"))
    for got in (provider.desk(), provider.project("weekly-12")):
        assert got["sample"] is True and got["notice"] == adapter.REASONS["call"]
        assert "private allocation details" not in json.dumps(got)


@pytest.mark.parametrize("platform, first, second", [("darwin", 2, 1), ("linux", 1, 2)])
def test_memory_limit_tries_supported_resources_and_preserves_stricter_limits(fake, monkeypatch, platform, first, second):
    attempted = []
    cap = adapter.MEMORY_LIMIT // 2

    def set_limit(kind, limits):
        attempted.append((kind, limits))
        if kind == first:
            raise ValueError("unsupported")

    resource = SimpleNamespace(RLIMIT_AS=1, RLIMIT_DATA=2, RLIM_INFINITY=-1,
                               getrlimit=lambda kind: (cap, cap * 3), setrlimit=set_limit)
    monkeypatch.setitem(sys.modules, "resource", resource)
    monkeypatch.setattr(sys, "platform", platform)
    adapter.limit_memory()
    assert attempted == [(first, (cap, cap * 3)), (second, (cap, cap * 3))]
    assert adapter._memory_limit_failed is False


def test_unavailable_memory_limits_start_the_watchdog_and_still_call_the_adapter(fake, monkeypatch):
    write, _, _, _, _ = fake
    write()
    started = []

    def unsupported(*args):
        raise ValueError("unsupported")  # what macOS answers for both limits

    resource = SimpleNamespace(RLIMIT_AS=1, RLIMIT_DATA=2, RLIM_INFINITY=-1,
                               getrlimit=lambda kind: (-1, -1), setrlimit=unsupported)
    monkeypatch.setitem(sys.modules, "resource", resource)
    monkeypatch.setattr(adapter, "_start_watchdog", lambda: started.append(True))
    adapter.limit_memory()
    assert started == [True] and adapter._memory_limit_failed is False
    assert provider.desk()["sample"] is False


def test_the_watchdog_ends_the_process_only_past_the_cap():
    stopped = []
    adapter._watch(limit=10, peak=lambda: 11, stop=stopped.append, every=0, rounds=3)
    assert stopped == [3]
    stopped.clear()
    adapter._watch(limit=10, peak=lambda: 9, stop=stopped.append, every=0, rounds=3)
    assert stopped == []


def test_subprocess_over_the_cap_is_ended_by_the_watchdog_where_limits_are_refused(fake, memory_limits_supported):
    if memory_limits_supported:
        pytest.skip("this host enforces a real memory limit; covered by the allocation test")
    write, desk, _, _, cfg = fake
    write(source=("import time\n"
                  f"def desk():\n    large = bytearray(160 * 1024 * 1024)\n    time.sleep(2)\n    return {desk!r}\n"
                  "def project(pid):\n    return None\n"))
    driver = ("import sys\nfrom ctb.studio import adapter\n"
              "adapter.CONFIG_PATH = sys.argv[1]\n"
              "adapter.MEMORY_LIMIT = 96 * 1024 * 1024\nadapter.limit_memory()\n"
              "from ctb.studio.api import main\nmain()\n")
    proc = subprocess.run([sys.executable, "-c", driver, str(cfg)], input='{"op":"desk.get"}',
                          capture_output=True, text=True, timeout=10)
    assert proc.returncode == 3 and proc.stdout == ""


def test_installed_api_applies_memory_limit_before_importing_adapter(fake, tmp_path, memory_limits_supported):
    write, desk, project, _, _ = fake
    write(source=("import resource\n"
                  "limits = [resource.getrlimit(getattr(resource, name))[0]\n"
                  "          for name in ('RLIMIT_AS', 'RLIMIT_DATA') if hasattr(resource, name)]\n"
                  "import threading\n"
                  f"assert (any(0 < n <= {adapter.MEMORY_LIMIT} for n in limits)\n"
                  "        or any(t.name == 'studio-memory-watchdog' for t in threading.enumerate()))\n"
                  f"def desk():\n    return {desk!r}\n"
                  f"def project(pid):\n    return {project!r}\n"))
    # Installation reads the root in-process; the runtime assertion belongs only to the installed command.
    folder = install.install(tmp_path / "apps")
    app = cockpit_apps.load_app(folder, panel_page.ICON_NAMES)
    code, data = cockpit_apps.run_command(app, b'{"op":"desk.get"}')
    assert code == 200 and data["desk"]["sample"] is False  # a real limit, or the watchdog where it is refused


def test_subprocess_allocation_over_its_memory_limit_returns_the_call_sample(fake, memory_limits_supported):
    if not memory_limits_supported:
        pytest.skip("host refuses both adapter memory limits; fail-soft behavior is tested separately")
    write, desk, _, _, cfg = fake
    write(source=(f"def desk():\n    large = bytearray(96 * 1024 * 1024)\n    return {desk!r}\n"
                  "def project(pid):\n    return None\n"))
    # Use a smaller cap in this child so even a broken limit cannot allocate more than 96 MiB in the test.
    driver = ("import sys\nfrom ctb.studio import adapter\n"
              "adapter.CONFIG_PATH = sys.argv[1]\n"
              "adapter.MEMORY_LIMIT = 64 * 1024 * 1024\nadapter.limit_memory()\n"
              "from ctb.studio.api import main\nmain()\n")
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    proc = subprocess.run([sys.executable, "-c", driver, str(cfg)], input='{"op":"desk.get"}',
                          capture_output=True, text=True, timeout=5, env=env)
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout)["desk"]
    assert got["sample"] is True and got["notice"] == adapter.REASONS["call"]


def test_install_pins_media_root_and_config_for_the_real_api_command(fake, tmp_path, memory_limits_supported):
    write, _, project, root, _ = fake
    (root / "frame.png").write_bytes(b"existing frame")
    project["scenes"][0]["frame"] = "frame.png"
    write()
    folder = install.install(tmp_path / "apps")
    app = cockpit_apps.load_app(folder, panel_page.ICON_NAMES)
    assert app.media_root == root.resolve()
    code, data = cockpit_apps.run_command(app, b'{"op":"project.get","project":"weekly-12"}')
    assert code == 200 and data["project"]["sample"] is False
    assert data["project"]["scenes"][0]["frame"] == "frame.png"
    code, data = cockpit_apps.run_command(app, b'{"op":"marks.set","project":"weekly-12","i":1,"mark":"keep"}')
    assert code == 200 and data["marks"] == {"1": {"mark": "keep"}}


def test_config_media_root_overrides_adapter_root(fake, tmp_path):
    write, _, project, root, _ = fake
    other = tmp_path / "other-media"
    other.mkdir()
    (root / "frame.png").write_bytes(b"adapter root")
    project["scenes"][0]["frame"] = "frame.png"
    write(extra={"studio_media_root": str(other)})
    assert provider.project("weekly-12")["scenes"][0]["frame"] is None
    assert install.manifest()["media_root"] == str(other)


@pytest.mark.parametrize("field", ["studio_adapter", "studio_media_root"])
@pytest.mark.parametrize("value", [123, True, "relative/file", "/", "/bad\x00path"])
def test_config_requires_absolute_optional_paths(field, value):
    with pytest.raises(config.ConfigError):
        config.from_dict({field: value})
