"""The panel rewrites an installed Studio from current code on start, in its own time-limited process, and never
installs it where it is not."""
import subprocess
import types

from ctb import panel


def _studio(tmp_path, monkeypatch, installed=True):
    import ctb.cockpit_apps as apps
    monkeypatch.setattr(apps, "apps_dir", lambda: tmp_path)
    if installed:
        (tmp_path / "studio").mkdir()
        (tmp_path / "studio" / "manifest.json").write_text("{}")


def test_installed_studio_is_rewritten_in_its_own_process_with_a_time_limit(tmp_path, monkeypatch):
    _studio(tmp_path, monkeypatch)
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        return types.SimpleNamespace(returncode=0)
    assert panel.refresh_installed_apps(run=run) is True
    (argv, kw), = calls
    assert "install.install(sys.argv[1])" in argv[3]
    assert argv[1] == "-I"  # isolated: a `ctb` in the current folder never shadows this install
    assert "cwd" not in kw  # the panel's own folder: relative state settings keep their meaning
    assert argv[-1] == str(tmp_path) and argv[-1].startswith("/")
    assert kw["timeout"] == panel.REFRESH_TIMEOUT and kw["stdin"] == subprocess.DEVNULL


def test_missing_studio_is_not_installed(tmp_path, monkeypatch):
    _studio(tmp_path, monkeypatch, installed=False)
    calls = []
    assert panel.refresh_installed_apps(run=lambda *a, **k: calls.append(a)) is False
    assert calls == []


def test_a_hung_or_failed_refresh_never_stops_the_panel(tmp_path, monkeypatch):
    _studio(tmp_path, monkeypatch)

    def hang(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw["timeout"])
    assert panel.refresh_installed_apps(run=hang) is False
    assert panel.refresh_installed_apps(run=lambda a, **k: types.SimpleNamespace(returncode=1)) is False


def test_serve_starts_the_refresh_off_the_main_thread():
    import inspect
    src = inspect.getsource(panel.serve)
    assert "threading.Thread(target=refresh_installed_apps" in src and "daemon=True" in src
    assert src.index("threading.Thread") < src.index("serve_forever")
