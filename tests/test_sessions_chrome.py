"""Sessions the bridge starts or resumes get the Chrome tools when the extension's host is installed."""
from ctb import accounts


def _host(home):
    d = home / "Library" / "Application Support" / "Google" / "Chrome" / "NativeMessagingHosts"
    d.mkdir(parents=True)
    (d / accounts.CHROME_HOST).write_text("{}")


def test_no_extension_keeps_the_plain_command(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(accounts, "multiple", lambda: False)
    assert accounts.shell_command() == "exec claude"


def test_an_installed_extension_starts_sessions_with_chrome(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(accounts, "multiple", lambda: False)
    _host(tmp_path)
    assert accounts.shell_command() == "exec claude --chrome"
    assert accounts.shell_command(replace=False) == "claude --chrome"
