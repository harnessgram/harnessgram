"""Nothing waits for a translation: the live card and the interim texts go out in English at once and are edited
to Hebrew afterwards; and the Gemini route no longer fails silently (credential reuse, dead routes skipped, a
second model only after the first failed, an English echo never accepted)."""
import io
import json
import subprocess
import time
import urllib.error

import pytest

from conftest import GOOD_TOKEN, FakeBot
from ctb import hooks
from ctb import progress as P
from ctb import translate as T
from ctb.config import Config, state_dir
from ctb.topics import TopicMap

OWNER = 7


def write_rows(session, rows):
    path = state_dir() / "progress"
    path.mkdir(parents=True, exist_ok=True)
    with open(path / f"{session}.jsonl", "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def he_cfg(**kw):
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he", translate=True,
                  engines=["gemini", "google"], **kw)


class TimedBot(FakeBot):
    def __init__(self):
        super().__init__()
        self.at = []

    def call(self, method, params=None, timeout=15):
        self.at.append((time.monotonic(), method, dict(params or {})))
        return super().call(method, params, timeout)


@pytest.fixture
def slow_gemini(monkeypatch):
    """Gemini as measured live (2026-11-10): about three seconds per short text; here 0.4 s, recorded."""
    calls = []

    def ask(prompt, cfg, **k):
        calls.append(time.monotonic())
        time.sleep(0.4)
        return "מריץ את הבדיקות הממוקדות"

    monkeypatch.setattr(T, "ask", ask)
    monkeypatch.setattr(T, "translate_text", lambda text, cfg, **k: calls.append(time.monotonic()) or time.sleep(0.4)
                        or "עכשיו השלב השני")
    monkeypatch.setattr(hooks, "spawn", lambda args, text=None: None)
    return calls


def test_a_new_step_is_on_the_card_before_any_translation_starts(slow_gemini):
    bot, c = TimedBot(), he_cfg()
    TopicMap(state_dir() / "topics.json", bot, OWNER).ensure("s", "T")
    write_rows("s", [{"kind": "action", "t": 1.0, "l": "🧪 Run the focused tests", "tool": "Bash", "d": "pytest -q"}])
    bot.at.clear()
    P.run_send_progress(c, "s", bot=bot, throttle=0, now_fn=lambda: 1000.0)
    first_card = next(at for at, m, p in bot.at if m == "sendMessage" and "Run the focused tests" in p.get("text", ""))
    assert slow_gemini and first_card < slow_gemini[0]  # shown first, translated after
    # the translation is cached and the card marked due; its next edit (the next step) shows it in Hebrew
    assert P.heading_cached("Run the focused tests", c) == "מריץ את הבדיקות הממוקדות"
    assert not [p for _, m, p in bot.at if m == "editMessageText"]  # no extra edit of its own
    write_rows("s", [{"kind": "action", "t": 2.0, "l": "📖 Read the log", "tool": "Read"}])
    bot.at.clear()
    P.run_send_progress(c, "s", bot=bot, throttle=0, now_fn=lambda: 2000.0)
    edits = [p for _, m, p in bot.at if m == "editMessageText"]
    assert edits and "מריץ את הבדיקות הממוקדות" in edits[0]["text"] and "pytest -q" in edits[0]["text"]
    assert "Read the log" in edits[0]["text"]


def test_steps_after_an_interim_text_do_not_wait_for_its_translation(slow_gemini):
    bot, c = TimedBot(), he_cfg()
    TopicMap(state_dir() / "topics.json", bot, OWNER).ensure("s", "T")
    P.heading_he  # noqa: B018 - the cache below makes headings known, so only the text translation is slow
    from ctb.config import write_private
    write_private(state_dir() / P.HEADINGS_CACHE_FILE, json.dumps({"Read the file": "קורא את הקובץ"}))
    write_rows("s", [{"kind": "text", "uuid": "u1", "text": "Now the second phase"},
                     {"kind": "action", "t": 2.0, "l": "📖 Read the file", "tool": "Read"}])
    bot.at.clear()
    P.run_send_progress(c, "s", bot=bot, throttle=0, now_fn=lambda: 1000.0)
    card = next(at for at, m, p in bot.at if m == "sendMessage" and "קורא את הקובץ" in p.get("text", ""))
    assert card < slow_gemini[0]  # the step after the text went out before the text's translation began
    edited = [p for _, m, p in bot.at if m == "editMessageText" and "עכשיו השלב השני" in p.get("text", "")]
    assert len(edited) == 1  # and the text was then edited to Hebrew, the original folded under it
    assert not P.jobs_waiting("s")


def test_a_flood_limit_keeps_the_text_job_for_a_later_run(monkeypatch):
    monkeypatch.setattr(T, "translate_text", lambda text, cfg, **k: "עכשיו השלב השני")
    c = he_cfg()
    P.queue_jobs("s", [], [{"ids": [[41, "Now the second phase"]], "chat": OWNER, "thread": 5}])
    real = P.translate_interim
    monkeypatch.setattr(P, "translate_interim", lambda *a, **k: (_ for _ in ()).throw(P.InterimFlood()))
    assert P.run_jobs(c, "s", bot=FakeBot()) == 0 and P.jobs_waiting("s")
    monkeypatch.setattr(P, "translate_interim", real)  # never monkeypatch.undo(): it would drop the test's state dir
    bot = FakeBot()
    assert P.run_jobs(c, "s", bot=bot) == 1 and not P.jobs_waiting("s")


def test_a_card_from_before_the_upgrade_still_renders():
    c = he_cfg()
    assert P.card_texts(["✔️ <b>old</b>", {"row": {"l": "📖 Read x", "tool": "Read", "output": "a<b"}}], c) == [
        "✔️ <b>old</b>", "✔️ <b>📖 ⁨Read⁩: Read x</b>\n<blockquote expandable>a&lt;b</blockquote>"]


# --- the Gemini route ------------------------------------------------------------------------------------

class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def ok(text):
    return Resp(json.dumps({"candidates": [{"content": {"parts": [{"text": text}]}}]}).encode())


def gem_cfg(**kw):
    return Config(gemini_vertex_project="p", gemini_token_command=["tok"], gemini_key_command=["key"],
                  gemini_model="gemini-3.8-flash", **kw)


def test_the_access_token_is_fetched_once_per_process(monkeypatch):
    runs = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: runs.append(cmd[0]) or
                        subprocess.CompletedProcess(cmd, 0, "secret\n", ""))
    monkeypatch.setattr(T.urllib.request, "urlopen", lambda req, timeout=None: ok("שלום"))
    c = gem_cfg()
    assert T.gemini("hello", c) == "שלום" and T.gemini("bye", c) == "שלום"
    assert runs == ["tok"]
    assert not list(state_dir().rglob("*")) or "secret" not in "".join(
        p.read_text(errors="ignore") for p in state_dir().rglob("*") if p.is_file())  # never on disk


def test_a_route_that_refuses_is_skipped_for_a_while_and_the_failure_is_logged(monkeypatch):
    seen = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, "c\n", ""))

    def urlopen(req, timeout=None):
        host = req.full_url.split("/")[2]
        seen.append(host)
        if host.startswith("generativelanguage"):
            raise urllib.error.HTTPError(req.full_url, 429, "quota", {}, io.BytesIO(b""))
        raise TimeoutError()
    monkeypatch.setattr(T.urllib.request, "urlopen", urlopen)
    c = gem_cfg()
    assert T.gemini("hello", c) == ""
    assert T.gemini("hello", c) == ""
    assert [h.split(".")[0] for h in seen] == ["aiplatform", "generativelanguage", "aiplatform"]  # key route skipped
    log = (state_dir() / "gemini-failures.log").read_text()
    assert "vertex gemini-3.8-flash TimeoutError" in log and "key gemini-3.8-flash http 429" in log


def test_the_second_model_runs_only_after_the_first_failed(monkeypatch):
    models = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, "c\n", ""))

    def urlopen(req, timeout=None):
        model = req.full_url.rsplit("/", 1)[1].split(":")[0]
        models.append(model)
        if model == "gemini-3.8-flash":
            raise urllib.error.HTTPError(req.full_url, 500, "x", {}, io.BytesIO(b""))
        return ok("שלום מהשני")
    monkeypatch.setattr(T.urllib.request, "urlopen", urlopen)
    c = gem_cfg(gemini_fallback_model="gemini-3.5-flash-lite", gemini_thinking="low")
    assert T.gemini("hello", c) == "שלום מהשני"
    assert models == ["gemini-3.8-flash", "gemini-3.8-flash", "gemini-3.5-flash-lite"]
    models.clear()
    monkeypatch.setattr(T.urllib.request, "urlopen", lambda req, timeout=None: models.append(1) or ok("שלום"))
    assert T.gemini("hello", c) == "שלום" and models == [1]


def test_translations_get_a_longer_budget_than_a_raw_ask(monkeypatch):
    budgets = []
    monkeypatch.setattr(T, "_gemini_call", lambda prompt, cfg, **k: budgets.append(k.get("budget")) or "שלום")
    T.gemini("hello", gem_cfg())
    T.ask("hi", gem_cfg())
    assert budgets == [T.TRANSLATE_BUDGET, None] and T.TRANSLATE_BUDGET > T.GEMINI_BUDGET


@pytest.mark.parametrize("text,out,bad", [
    ("The tests pass and the batch is ready.", "The tests pass and the batch is ready.", True),  # an echo
    ("The tests pass and the batch is ready.", "הבדיקות עוברות והאצווה מוכנה.", False),
    ("Codex review of PR 131 on boost is done.", "הבדיקה של Codex ל-PR 131 על boost הסתיימה.", False),
    ("First part.\n\nThe second paragraph was never translated by the engine at all.",
     "החלק הראשון.\n\nThe second paragraph was never translated by the engine at all.", True),  # partial
    ("כבר בעברית", "כבר בעברית", False),
])
def test_an_answer_still_in_english_is_no_answer(text, out, bad):
    assert T.still_untranslated(text, out, "he") is bad


def test_an_english_echo_falls_through_to_the_next_engine(monkeypatch):
    c = Config(engines=["gemini", "google"], translate_cache=False, target_language="he")
    monkeypatch.setattr(T, "gemini", lambda text, cfg: text)
    monkeypatch.setattr(T, "google", lambda text, target: "הבדיקות עוברות.")
    assert T._engines("The tests pass.", c, False) == "הבדיקות עוברות."


def test_two_texts_of_one_flush_are_both_translated(monkeypatch):
    monkeypatch.setattr(T, "translate_text", lambda text, cfg, **k: "תורגם")
    monkeypatch.setattr(P.time, "time", lambda: 1_000_000.0)  # review of #154: one clock tick, one shared key
    P.queue_jobs("s2", [], [{"ids": [[41, "First"]], "chat": OWNER, "thread": 5},
                            {"ids": [[42, "Second"]], "chat": OWNER, "thread": 5}])
    done = 0
    for _ in range(3):
        done += P.run_jobs(he_cfg(), "s2", bot=FakeBot())
    assert done == 2 and not P.jobs_waiting("s2")


def test_a_cards_translated_buttons_are_a_new_keyboard():
    from ctb import mirror as M
    kb = {"inline_keyboard": [[{"text": "1. Yes", "callback_data": "dg:o:1"}]]}
    orig = json.loads(json.dumps(kb))
    import unittest.mock as um
    with um.patch.object(M, "translate_text", lambda text, cfg, **k: "1. כן"), \
            um.patch.object(M.L, "needs_translation", lambda *a, **k: True):
        text, out = M.translate_card(he_cfg(), "Q", "Pick one", kb)
    assert kb == orig  # the caller's keyboard is untouched, so it can see what changed
    assert out["inline_keyboard"][0][0]["text"] == "1. כן"
