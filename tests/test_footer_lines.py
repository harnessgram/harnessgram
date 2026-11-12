"""The status footer under a reply: one fact per line, no line mixing Hebrew with long English; the
translated body keeps English apart from Hebrew even when it comes back from the cache."""
import json
import re
import time

from conftest import GOOD_TOKEN
from ctb import fmt, mirror as M, translate as TR, usage
from ctb.config import Config, state_dir

OWNER = 7
HEB = re.compile(r"[֐-׿]")
LAT = re.compile(r"[A-Za-z]")


def cfg(lang, **kw):
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language=lang, **kw)


def transcript(tmp_path, tasks=()):
    lines = [json.dumps({"type": "assistant", "message": {"role": "assistant", "model": "claude-x", "content": [],
                                                          "usage": {"input_tokens": 184_000}}})]
    for i, desc in enumerate(tasks):
        lines.append(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{
            "type": "tool_use", "id": f"tu{i}", "name": "Agent",
            "input": {"run_in_background": True, "description": desc}}]}}))
        lines.append(json.dumps({"type": "user", "message": {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": f"tu{i}", "content": f"started. agentId: ag{i}x"}]}}))
    p = tmp_path / "sess.jsonl"
    p.write_text("\n".join(lines))
    return str(p)


def save_state(model="Opus 5.5|medium", five=0, weekly=45):
    state_dir().mkdir(parents=True, exist_ok=True)
    (state_dir() / "session-models.json").write_text(json.dumps({"sess": model}))
    (state_dir() / "claude-usage.json").write_text(json.dumps(
        {"five_hour": five, "weekly": weekly, "at": time.time() - 30}))


def mixed(line):
    """A line that still mixes Hebrew with Latin letters once short isolated names are taken out."""
    return bool(HEB.search(line) and LAT.search(re.sub("⁨[^⁩]*⁩", "", line)))


TASKS = ["Restart the PR review run", "Build a live agents card", "Fix the findings of PR 103 review", "Fourth"]


def test_english_footer_is_one_fact_per_line(tmp_path):
    save_state()
    lines = usage.footer(cfg("en"), transcript(tmp_path, TASKS)).split("\n")
    assert lines[0] == "🧠 184k · Opus 5.5 · medium"
    assert lines[1].startswith("⏳ weekly 45%") and lines[1].endswith("ago") and "5h" not in lines[1]
    assert lines[2] == "⚙️ Background (4):"
    assert all(l.startswith("• ") for l in lines[3:7]) and len(lines) == 7
    assert not any(HEB.search(l) for l in lines)


def test_hebrew_footer_never_mixes_scripts_on_a_line(tmp_path):
    save_state(model="Claude Sonnet Very Long Name Edition|high")
    lines = usage.footer(cfg("he"), transcript(tmp_path, TASKS)).split("\n")
    assert lines[0] == "🧠 הקשר 184 אלף"
    assert lines[1] == "🤖 Claude Sonnet Very Long Name Edition"      # a long name: a line of its own
    assert lines[2] == "🎚️ גבוהה"
    assert lines[3].startswith("⏳ שבועית 45%") and "5 שעות" not in lines[3]
    assert "⚙️ ברקע (4):" in lines
    assert not [l for l in lines if mixed(l)]
    assert not any(re.search(r"\d+k\b", l) for l in lines)


def test_hebrew_footer_isolates_a_short_model_name(tmp_path):
    save_state()
    lines = usage.footer(cfg("he"), transcript(tmp_path)).split("\n")
    assert lines[1] == "🤖 ⁨Opus 5.5⁩ · 🎚️ בינונית"
    assert not [l for l in lines if mixed(l)]


def test_weekly_reset_clock_is_kept_ltr_in_hebrew(tmp_path):
    (state_dir()).mkdir(parents=True, exist_ok=True)
    reset = time.time() + 3 * 86400
    (state_dir() / "claude-usage.json").write_text(json.dumps(
        {"five_hour": 0, "weekly": 45, "at": time.time(), "five_hour_resets": "", "weekly_resets": reset}))
    q = usage.quota_line("he")
    stamp = time.strftime("%d.%m %H:%M", time.localtime(reset))
    assert f"↻ ⁦{stamp}⁩" in q
    day = time.strftime("%d.%m", time.localtime(reset))  # a later day by its date only: the cell fits a phone
    assert M.live_keyboard(q, lang="he")["keyboard"][0] == [{"text": f"📅 45% · ↻ {day}"}]  # weekly only


def test_compact_footer_is_lines_without_the_task_list(tmp_path):
    save_state()
    out = usage.compact_footer(cfg("en"), transcript(tmp_path, TASKS))
    assert out.split("\n")[0] == "🧠 184k · Opus 5.5 · medium"
    assert "⚙️ Background (4)" in out and "•" not in out and "Background (4):" not in out


def test_keyboard_reads_context_and_quota_from_the_new_footer(tmp_path):
    save_state(five=10, weekly=45)
    he = M.live_keyboard(usage.footer(cfg("he"), transcript(tmp_path)), lang="he")["keyboard"][0]
    en = M.live_keyboard(usage.footer(cfg("en"), transcript(tmp_path)), lang="en")["keyboard"][0]
    assert he == [{"text": "📏 184 אלף"}, {"text": "📅 45%"}]  # weekly only: no 5-hour number
    assert en == [{"text": "📏 184k"}, {"text": "📅 45%"}]


def test_footer_html_english_pre_hebrew_rtl_italic():
    assert M.Mirror._footer("🧠 1k\n⏳ 5h 1%") == "\n\n— — —\n<pre>🧠 1k\n⏳ 5h 1%</pre>"
    he = M.Mirror._footer("🧠 הקשר 1 אלף\n⏳ <5>")
    assert he == "\n\n— — —\n<i>‏🧠 הקשר 1 אלף\n‏⏳ &lt;5&gt;</i>"
    assert M.Mirror._footer("") == ""


# --- translated body -------------------------------------------------------------------------------

LONG = "שלחתי את המשימה ל־AGY Gemini 3.8 Flash high, או OpenCode kimi-k3.1-pro כדי לחסוך"


def test_unmix_handles_a_maqaf_prefix_and_leaves_code_fences_alone():
    out = fmt.unmix(LONG)
    assert "AGY Gemini 3.8 Flash high," in out.split("\n")           # a long English run: its own line
    assert not [l for l in out.split("\n") if mixed(l)]
    fenced = "ראה:\n```\nהדפס AGY Gemini 3.8 Flash high אחרי\n```"
    assert fmt.unmix(fenced) == fenced
    assert "`AGY Gemini 3.8 Flash high`" in fmt.unmix("הרצתי `AGY Gemini 3.8 Flash high` שוב").split("\n")


def test_cached_translation_is_split_again_on_the_way_out():
    c = cfg("he")
    source = "Run it on the models"
    path = TR._cache_path(source, c)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"text": LONG.replace("ל־", "ל-").replace("-AGY", "- AGY"), "engine": "gemini"}))
    out = TR.translate_text(source, c)
    assert "AGY Gemini 3.8 Flash high," in out.split("\n")
    assert not [l for l in out.split("\n") if mixed(l)]


def test_cached_translation_keeps_its_arrow_direction(monkeypatch):
    # The arrow swap is its own inverse: a cached Hebrew line (saved already swapped) must come back
    # as the fresh translation did, not swapped a second time.
    c = cfg("he")
    monkeypatch.setattr(TR, "_engines", lambda text, cfg, fast: "שלב 1 → שלב 2")
    fresh = TR.translate_text("Step 1 -> step 2", c)
    assert TR.translate_text("Step 1 -> step 2", c) == fresh
    assert "←" in fresh


def test_long_engine_list_gets_its_own_line():
    names = "nllb, mlx, gemini, google"
    assert fmt.isolate(names) is None  # too long to sit inside a Hebrew line, so mirror puts it alone


def test_brackets_stay_outside_the_isolate():
    out = fmt.unmix("הוא בבדיקת אבטחה (Claude Opus, מאמץ גבוה) לפני ההתקנה.")
    assert "(⁨Claude Opus⁩, מאמץ גבוה)" in out
    assert fmt.unmix("ראו (Gemini) כאן") == "ראו (⁨Gemini⁩) כאן"


def test_changed_files_and_enabled_footers(tmp_path, monkeypatch):
    path = tmp_path / "sample.jsonl"
    events = [{"type": "assistant", "message": {"model": "sample", "usage": {"input_tokens": 450000},
        "content": [{"type": "tool_use", "name": "Edit", "input": {"file_path": f"/sample/file{i}.py"}}
                    for i in range(5)]}}]
    path.write_text("\n".join(json.dumps(event) for event in events))
    monkeypatch.setattr(usage, "background_of", lambda path: {"job": "⚙️ Sample"})
    monkeypatch.setattr("ctb.dashboard.quotas", lambda **kw: [])
    monkeypatch.setattr("ctb.translate.job_hebrew", lambda label: label)
    lines = usage.changed_lines(str(path), "en")
    assert "5" in lines[0] and "file0.py" in lines[1]
    assert "file0.py" in usage.footer(Config(context_footer=True, target_language="en"), str(path))
    context = usage.context_footer(str(path), Config(context_footer=True, ui_language="en"))
    assert "450" in context and "file0.py" in context and "Sample" in context
    assert "file4.py" not in context


def test_a_bracket_inside_a_short_english_name_stays_outside_the_isolates():
    from ctb import fmt as _f
    out = _f.unmix("כנראה בגלל התזמון. Codex (gpt-6.1-sol, מאמץ גבוה) מחפש עכשיו.")
    assert "⁨Codex⁩ (⁨gpt-6.1-sol⁩, מאמץ גבוה)" in out
    for run in out.split("⁨")[1:]:
        assert not any(b in run.split("⁩")[0] for b in "()[]")
