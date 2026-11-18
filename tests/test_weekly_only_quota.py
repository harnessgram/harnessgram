"""Reported Claude 5-hour windows appear in quota rows and account lines; Codex stays weekly-only.
The footer and status line continue to show the weekly number and banked resets."""
import json
import time
from pathlib import Path

from ctb import accounts, dashboard, statusline, usage
from ctb.config import Config, state_dir, write_private

NOW = 1_800_000_000.0


def save(name="main", **fields):
    record = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 60)), "five_hour": 88, "weekly": 41,
              "five_hour_resets": int(NOW + 3600), "weekly_resets": int(NOW + 3 * 86400), **fields}
    state_dir().mkdir(parents=True, exist_ok=True)
    write_private(state_dir() / f"claude-usage-{name}.json", json.dumps(record))


def rollout(tmp_path: Path, primary_minutes: int, secondary=None) -> Path:
    folder = tmp_path / "codex" / "2026" / "11" / "10"
    folder.mkdir(parents=True)
    second = ("null" if secondary is None else
              '{"used_percent":%s,"window_minutes":%s,"resets_at":1800090000}' % secondary)
    event = ('{"rate_limits":{"limit_id":"codex","limit_name":null,"primary":{"used_percent":81.0,'
             '"window_minutes":%d,"resets_at":1800300000},"secondary":%s,"credits":{"has_credits":false}}}'
             % (primary_minutes, second))
    (folder / "rollout-x.jsonl").write_text(event + "\n")
    return tmp_path / "codex"


def test_footer_quota_line_is_weekly_with_reset_and_no_five_hour():
    save()
    for lang in ("en", "he"):
        line = usage.quota_line(lang, account="main", now=NOW)
        assert "41%" in line and "88" not in line and "↻" in line
        assert "5h" not in line and "5 שעות" not in line


def test_banked_resets_are_shown_only_when_the_record_has_them():
    save()
    assert "banked" not in usage.quota_line("en", account="main", now=NOW)
    save(banked_resets=3)
    assert "3 banked resets" in usage.quota_line("en", account="main", now=NOW)
    assert "3 איפוסים שמורים" in usage.quota_line("he", account="main", now=NOW)
    for bad in (-1, "3", True, None):
        save(banked_resets=bad)
        assert usage.banked_resets(json.loads((state_dir() / "claude-usage-main.json").read_text())) is None


def test_codex_window_is_named_by_its_real_length(tmp_path):
    # The Pro plan reports its WEEKLY limit as the primary window (10080 minutes): never a "5h".
    assert usage.codex_limits(rollout(tmp_path / "a", 10080)) == {"week": (81.0, 1800300000)}
    both = usage.codex_limits(rollout(tmp_path / "b", 300, (12.0, 10080)))
    assert both == {"5h": (81.0, 1800300000), "week": (12.0, 1800090000)}


def test_pinned_quota_card_shows_reported_claude_window_but_not_codex(tmp_path):
    save()
    gemini = tmp_path / "gemini.json"
    gemini.write_text(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 60)),
                                  "rows": [{"window": "5h", "percent": 40}, {"window": "week", "percent": 10}]}))
    cfg = Config(codex_sessions_dir=str(rollout(tmp_path, 300, (12.0, 10080))),
                 quota_files=[{"label": "Gemini", "path": str(gemini)}])
    rows = usage.quota_rows(full=True, account="main", now=NOW, lang="en", cfg=cfg)
    assert [r for r in rows if r[0] == "5h"] == [("5h", "Claude", 88.0, int(NOW + 3600)),
                                                ("5h", "Gemini", 40.0, "rolling")]
    assert ("week", "Claude", 41.0, int(NOW + 3 * 86400)) in rows
    assert ("week", "Codex", 12.0, 1800090000) in rows
    text = "\n".join(dashboard.quotas(accounts_flag=False, full=True, account="main", now=NOW, cfg=cfg, lang="en"))
    five_block = text.split("5 hours", 1)[1].split("</pre>", 1)[0] if "5 hours" in text else ""
    assert "Claude" in five_block and "Codex" not in five_block and "Gemini" in five_block
    save(five_hour=0, five_hour_resets=0)
    assert not any(r[0] == "5h" and r[1] == "Claude" for r in
                   usage.quota_rows(account="main", now=NOW, lang="en", cfg=cfg))


def test_claude_extra_five_hour_rows_are_dropped():
    save(extra=[{"kind": "five_hour_opus", "percent": 70, "resets_at": NOW + 3600},
                {"kind": "seven_day_opus", "percent": 30, "resets_at": NOW + 86400}])
    rows = usage.quota_rows(full=True, account="main", now=NOW, lang="en", cfg=Config())
    assert [r for r in rows if r[0] == "5h"] == [("5h", "Claude", 88.0, int(NOW + 3600))]
    assert ("week", "Claude Opus", 30.0, NOW + 86400) in rows


def test_accounts_lines_show_reported_windows(monkeypatch):
    monkeypatch.setattr(accounts, "multiple", lambda: True)
    monkeypatch.setattr(accounts, "names", lambda: ["main"])
    monkeypatch.setattr(accounts, "sign_in_unfinished", lambda name: False)
    monkeypatch.setattr(accounts, "label", lambda name: name)
    monkeypatch.setattr(accounts, "_read", lambda path, default: {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 60)), "five_hour": 95, "weekly": 41,
        "five_hour_resets": NOW + 3600})
    lines = accounts.usage_lines(now=NOW, lang="en")
    assert any("5h 95% · week 41%" in line and line.startswith("🔴") for line in lines)
    monkeypatch.setattr(accounts, "_read", lambda path, default: {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 60)), "weekly": 41})
    lines = accounts.usage_lines(now=NOW, lang="en")
    assert any("week 41%" in line and line.startswith("🟢") and "5h" not in line for line in lines)


def test_status_line_prints_the_weekly_number_only():
    out = statusline.run(json.dumps({"model": {"display_name": "Opus 5.5"}, "session_id": "s",
                                     "rate_limits": {"five_hour": {"used_percentage": 77},
                                                     "seven_day": {"used_percentage": 41}}}))
    assert out == "Opus 5.5 · week 41%"
