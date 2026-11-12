import json
import time
from pathlib import Path
import pytest

from ctb import dashboard, fmt, hooks, statusline, translate, usage
from ctb.config import Config


# (Behaviour 6 - Per-account Claude quota)

def test_claude_account_resolution(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "state_dir", lambda: tmp_path)
    # Default is main
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert usage.claude_account() == "main"
    assert hooks.claude_account() == "main"

    # From CLAUDE_CONFIG_DIR with .claude-profiles
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/home/user/.claude-profiles/work")
    assert usage.claude_account() == "work"
    assert hooks.claude_account() == "work"

    # From explicit env dict
    env = {"CLAUDE_CONFIG_DIR": "/home/user/.claude-profiles/analytics"}
    assert usage.claude_account(env=env) == "analytics"

    # The default for NEW sessions never decides a running process's account (review of 3b3c59a)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    (tmp_path / "claude-account").write_text("personal\n", encoding="utf-8")
    assert usage.claude_account() == "main"

    # From session mapping
    (tmp_path / "session-accounts.json").write_text(json.dumps({"sess-1": "work"}), encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude-profiles" / "work"))
    (tmp_path / "claude-account").unlink(missing_ok=True)
    assert usage.claude_account(session="sess-1") == "work"


def test_claude_usage_file_resolution_and_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "state_dir", lambda: tmp_path)
    # When account file exists
    work_file = tmp_path / "claude-usage-work.json"
    work_file.write_text('{"at": "2026-10-30T10:00:00Z", "five_hour": 45}', encoding="utf-8")
    assert usage.claude_usage_file(account="work") == work_file

    # When account file does not exist, fall back to claude-usage.json
    fallback_file = tmp_path / "claude-usage.json"
    fallback_file.write_text('{"at": "2026-10-30T10:00:00Z", "five_hour": 10}', encoding="utf-8")
    assert usage.claude_usage_file(account="other") == fallback_file


def test_claude_five_hour_freshness(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "state_dir", lambda: tmp_path)
    now = 1761825600.0  # arbitrary epoch
    at_fresh = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 3600))  # 1 hour old
    at_stale = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 13 * 3600))  # 13 hours old

    # Fresh reading
    (tmp_path / "claude-usage-work.json").write_text(
        json.dumps({"at": at_fresh, "five_hour": 60, "weekly": 40}), encoding="utf-8"
    )
    assert usage.claude_five_hour(account="work", now=now) is None  # no 5-hour window is ever shown

    # Stale reading (> 12 hours) returns None
    (tmp_path / "claude-usage-work.json").write_text(
        json.dumps({"at": at_stale, "five_hour": 60, "weekly": 40}), encoding="utf-8"
    )
    assert usage.claude_five_hour(account="work", now=now) is None


def test_quota_rows_labels_claude_per_account(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(usage, "codex_limits", lambda *a, **k: None)

    now = 1761825600.0
    at_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 1800))

    # Main account
    (tmp_path / "claude-usage-main.json").write_text(
        json.dumps({"at": at_iso, "five_hour": 25, "weekly": 50}), encoding="utf-8"
    )
    rows_main = usage.quota_rows(account="main", now=now, lang="he")
    assert any(team == "קלוד" for _, team, _, _ in rows_main)
    assert not any(team.startswith("קלוד ") for _, team, _, _ in rows_main)

    rows_main_en = usage.quota_rows(account="main", now=now, lang="en")
    assert any(team == "Claude" for _, team, _, _ in rows_main_en)

    # Non-main account: "work" -> labeled "Claude work" / "קלוד work"
    (tmp_path / "claude-usage-work.json").write_text(
        json.dumps({
            "at": at_iso, "five_hour": 75, "weekly": 80,
            "extra": [{"kind": "Opus", "percent": 90, "resets_at": "rolling"}]
        }), encoding="utf-8"
    )
    rows_work = usage.quota_rows(account="work", now=now, lang="he", full=True)
    assert any(team == "קלוד work" for _, team, _, _ in rows_work)
    assert any(team == "קלוד Opus" for _, team, _, _ in rows_work)

    rows_work_en = usage.quota_rows(account="work", now=now, lang="en", full=True)
    assert any(team == "Claude work" for _, team, _, _ in rows_work_en)
    assert any(team == "Claude Opus" for _, team, _, _ in rows_work_en)


def test_statusline_saves_per_account_usage(tmp_path, monkeypatch):
    monkeypatch.setattr(statusline, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(usage, "state_dir", lambda: tmp_path)
    env = {"CLAUDE_CONFIG_DIR": "/home/.claude-profiles/staging"}

    raw = json.dumps({
        "rate_limits": {
            "five_hour": {"used_percentage": 55, "resets_at": "2026-10-30T20:00:00Z"},
            "seven_day": {"used_percentage": 30, "resets_at": "2026-11-06T20:00:00Z"}
        },
        "model": {"display_name": "Opus 5.5"},
        "session_id": "00000000-0000-0000-0000-000000000001"
    })
    out = statusline.run(raw, env=env)
    assert "Opus 5.5" in out
    assert (tmp_path / "claude-usage-staging.json").exists()
    assert (tmp_path / "claude-usage.json").exists()
    saved = json.loads((tmp_path / "claude-usage-staging.json").read_text(encoding="utf-8"))
    assert saved["five_hour"] == 55
    assert saved["weekly"] == 30


# (Behaviour 9 - unmix and job_hebrew)

def test_unmix_arrow_flipping():
    # Hebrew line without backtick flips arrows
    assert fmt.unmix("שלב 1 -> שלב 2") == "שלב 1 ← שלב 2"
    assert fmt.unmix("שלב 1 → שלב 2") == "שלב 1 ← שלב 2"
    assert fmt.unmix("שלב 1 ← שלב 2") == "שלב 1 → שלב 2"
    assert fmt.unmix("תוצאה ⇒ המשך") == "תוצאה ⇐ המשך"
    assert fmt.unmix("תוצאה ⇐ המשך") == "תוצאה ⇒ המשך"

    # Hebrew line WITH backtick does NOT flip arrow (keeps code intact)
    assert fmt.unmix("הקוד `x -> y` פועל") == "הקוד\n`x -> y`\nפועל"


def test_unmix_prefix_hyphen_removal():
    # Prefix hyphen before Hebrew words removed
    assert fmt.unmix("ה-סשן נסגר בהצלחה") == "הסשן נסגר בהצלחה"
    assert fmt.unmix("ו-הודעה נשלחה כ-מצופה") == "והודעה נשלחה כמצופה"
    assert fmt.unmix("ל-משתמש ב-מערכת") == "למשתמש במערכת"

    # Prefix hyphen before English word is preserved on separate lines
    result = fmt.unmix("עודכן ב-PR האחרון")
    assert "ב-" in result
    assert "PR" in result
    # A short name stays inline in a bidi isolate, glued to its prefix.
    assert result == "עודכן ב-\u2068PR\u2069 האחרון"


def test_unmix_splits_mixed_lines():
    # English and Hebrew never share a line
    text = "הקובץ the main build script עודכן"
    result = fmt.unmix(text)
    assert result == "הקובץ\nthe main build script\nעודכן"
    assert fmt.unmix("הקובץ script.py עודכן") == "הקובץ \u2068script.py\u2069 עודכן"

    # Numbers and marks stay with preceding words
    err = "שגיאה: 404 the page was Not Found"
    res_err = fmt.unmix(err)
    assert res_err == "שגיאה: 404\nthe page was Not Found"

    # Markdown links and URLs treated as English
    link_line = "קרא ב- [Guide](https://example.com) לפרטים"
    res_link = fmt.unmix(link_line)
    lines = res_link.splitlines()
    assert lines[0] == "קרא ב-"
    assert lines[1] == "[Guide](https://example.com)"
    assert lines[2] == "לפרטים"


def test_unmix_preserves_bold_markers_across_splits():
    mixed_bold = "**הקובץ the big data file נשמר**"
    res = fmt.unmix(mixed_bold)
    for line in res.splitlines():
        # Every split segment has balanced bold markers
        assert line.count("**") % 2 == 0
    assert "**הקובץ**" in res
    assert "**the big data file**" in res
    assert "**נשמר**" in res


def test_job_hebrew_gerund_translation():
    # Mock translate function that simulates Hebrew agent translation
    def mock_tr(phrase):
        if "reviewing PR 11" in phrase:
            return "הסוכן בוחן את PR 11."
        if "building and testing" in phrase:
            return "הסוכן בונה ובודק."
        return phrase

    assert translate.job_hebrew("Review PR 11", translate_fn=mock_tr) == "בוחן את PR 11"
    assert translate.job_hebrew("Build and test (codex exec)", translate_fn=mock_tr) == "בונה ובודק"

    # Fixes glossary quirks
    def mock_tr_quirks(phrase):
        return "הסוכן מאציל סמכויות במסה."
    assert translate.job_hebrew("Delegate tasks", translate_fn=mock_tr_quirks) == "מעביר סמכויות במכסה"

    # Non-English labels returned untouched
    assert translate.job_hebrew("בודק את הקוד") == "בודק את הקוד"


# Optional quota sources: off by default, enabled by config only

def test_quota_rows_only_claude_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "state_dir", lambda: tmp_path)
    now = 1761825600.0
    at_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 600))
    (tmp_path / "claude-usage-main.json").write_text(
        json.dumps({"at": at_iso, "five_hour": 10, "weekly": 20}), encoding="utf-8")
    rows = usage.quota_rows(account="main", now=now, lang="en", cfg=Config())
    assert {team for _, team, _, _ in rows} == {"Claude"}


def test_quota_files_are_opt_in_and_generic(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "state_dir", lambda: tmp_path)
    now = 1761825600.0
    at_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 600))
    ext = tmp_path / "other.json"
    ext.write_text(json.dumps({"at": at_iso, "rows": [
        {"window": "5h", "percent": 40, "resets": now + 3600},
        {"window": "week", "percent": 12.5, "label": "pro"},
        {"window": "bogus", "percent": 1},
        {"window": "month", "percent": "x"},
    ]}), encoding="utf-8")
    cfg = Config(quota_files=[{"label": "Other tool", "path": str(ext)}])
    rows = usage.quota_rows(account="main", now=now, lang="en", cfg=cfg)
    assert ("5h", "Other tool", 40.0, now + 3600) in rows
    assert ("week", "Other tool pro", 12.5, "rolling") in rows
    assert len([r for r in rows if r[1].startswith("Other tool")]) == 2
    # A stale file is ignored.
    assert usage.file_limits({"path": str(ext)}, now=now + 13 * 3600) == []
    assert usage.file_limits({"path": str(tmp_path / "missing.json")}, now=now) == []


def test_codex_limits_needs_a_configured_folder(tmp_path):
    assert usage.codex_limits() is None
    assert usage.codex_limits("") is None
    day = tmp_path / "2026" / "01" / "01"
    day.mkdir(parents=True)
    (day / "rollout.jsonl").write_text(
        '{"rate_limits":{"limit_id":"codex","primary":{"used_percent":30.0,"window_minutes":300,"resets_at":1761830000},'
        '"secondary":{"used_percent":55.0,"window_minutes":10080,"resets_at":1762400000},"credits"}}\n', encoding="utf-8")
    got = usage.codex_limits(tmp_path)
    assert got == {"5h": (30.0, 1761830000), "week": (55.0, 1762400000)}


def test_config_validates_quota_settings():
    import pytest
    from ctb.config import ConfigError, from_dict
    cfg = from_dict({"quota_files": [{"label": "x", "path": "/p"}], "codex_sessions_dir": " ~/s ", "projects_root": "~/code"})
    assert cfg.codex_sessions_dir == "~/s" and cfg.quota_files[0]["path"] == "/p"
    assert str(cfg.projects_dir).endswith("/code")
    with pytest.raises(ConfigError):
        from_dict({"quota_files": ["not-an-object"]})
    with pytest.raises(ConfigError):
        from_dict({"projects_root": 5})
    assert from_dict({}).projects_dir == Path.home()


def test_wrap_reset_formatting():
    # Empty clock -> row alone
    assert dashboard.wrap_reset("🟢 Tool   0%", "") == ["🟢 Tool   0%"]
    assert fmt.wrap_reset("🟢 Tool   0%", "") == ["🟢 Tool   0%"]

    # Short row + clock <= 24 characters -> one line
    # len("🟢 Tool 0%") is 9 chars. Clock is 5. 9 + 3 + 5 = 17 <= 24
    res_short = dashboard.wrap_reset("🟢 Tool 0%", "15:30")
    assert res_short == ["🟢 Tool 0% ↻ 15:30"]

    # Long row + clock > 24 characters -> wraps indented
    # len("🔴 Claude work 100%") is 19. Clock is 5. 19 + 3 + 5 = 27 > 24
    res_long = dashboard.wrap_reset("🔴 Claude work 100%", "15:30")
    assert res_long == ["🔴 Claude work 100%", "     ↻ 15:30"]


def test_pin_quotas_table_rendering(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(usage, "state_dir", lambda: tmp_path)
    now = 1761825600.0
    at_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 600))

    # Account "work"
    (tmp_path / "claude-usage-work.json").write_text(
        json.dumps({"at": at_iso, "five_hour": 50, "weekly": 70,
                    "five_hour_resets": "rolling", "weekly_resets": "rolling"}),
        encoding="utf-8"
    )
    monkeypatch.setattr(usage, "codex_limits", lambda *a, **k: None)

    lines = dashboard.quotas(accounts_flag=False, full=False, account="work", now=now)
    assert not any("5 שעות" in l for l in lines) and any("שבוע" in l for l in lines)  # weekly only
    assert any("<pre>" in l for l in lines)
    text_content = "\n".join(lines)
    # The non-main account must be labeled "Claude work"
    assert "Claude work" in text_content


def test_main_session_usage_never_lands_in_default_account(monkeypatch):
    """Review of 3b3c59a: with `/account use work`, a main-account session (no profile env, no
    record) must still record its usage as main, not as the default for new sessions."""
    from ctb import accounts, usage
    monkeypatch.setattr(accounts, "current", lambda: "work")
    monkeypatch.setattr(accounts, "account_for", lambda session: None)
    assert usage.claude_account("sess-x", env={}) == "main"
    monkeypatch.setattr(accounts, "account_for", lambda session: "work")
    assert usage.claude_account("sess-x", env={}) == "work"  # a moved session keeps its own account


def test_auto_compact_runs_in_its_own_process(monkeypatch):
    """Review of 3b3c59a: a hook's thread dies with the hook; the compaction is a detached process."""
    import subprocess
    from ctb import compact
    seen = []
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kw: seen.append((args, kw)))
    assert compact.spawn_auto_compact("sess-1", 260000.0, cold=True)
    args, kw = seen[0]
    assert args[-3:] == ["sess-1", "260000", "cold"] and "auto-compact" in args
    assert kw.get("start_new_session") is True


def test_auto_compact_command_waits_for_its_threads(monkeypatch):
    """Review of 42ff513: the detached auto-compact process must not exit before its work threads."""
    import threading
    import time
    from ctb import cli, inbox
    done = []

    def fake(session, ctx, cold=False):
        def work():
            time.sleep(0.3)
            done.append(session)
        threading.Thread(target=work, daemon=True).start()
        return True
    monkeypatch.setattr(inbox, "auto_compact", fake)
    assert cli.main(["auto-compact", "sess-1", "260000", "cold"]) == 0
    assert done == ["sess-1"]
