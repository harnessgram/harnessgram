"""Tests for Chunk 3 ports (sessions, panes, keyboard):
1. Planned restart marker
2. Finding a session's tmux pane via Claude Code process record
3. Preventing reused tmux pane id from making closed session appear alive
4. Trust dialog handling (auto-trusting new clean project vs checking previous trust)
5. New clean project button in project picker
6. Fixed keyboard: Thinking/Model buttons, live values, account button, one-line footer quota
"""
from __future__ import annotations

from ctb.usershell import GUARD_END
import json
import threading
import os
import subprocess
import time
from pathlib import Path

import pytest

from conftest import GOOD_TOKEN, FakeBot
from ctb import accounts, hooks as H, i18n, inbox as I, mirror as M, status
from ctb.config import Config, state_dir, write_private
from ctb.topics import TopicMap

OWNER = 7


def cfg(**kw):
    base = dict(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he")
    base.update(kw)
    return Config(**base)


class TextMedia:
    def content_of(self, m):
        return m.get("text") or ""


def update(text, thread, chat=OWNER, sender=OWNER, **extra):
    msg = {"message_id": 5, "date": int(time.time()), "text": text, "message_thread_id": thread,
           "chat": {"id": chat, "type": "private"}, "from": {"id": sender}}
    msg.update(extra)
    return {"update_id": 1, "message": msg}


# --- 1. Planned restart marker ------------------------------------------------

def test_planned_restart_marker_create_and_consume(tmp_path):
    session = "sess-restart-1"
    I.mark_planned_restart(session)
    marker = I.planned_restart_marker(session)
    assert marker.exists()

    assert I.consume_planned_restart(session) is True
    assert not marker.exists()
    assert I.consume_planned_restart(session) is False


def test_planned_restart_marker_expiry(tmp_path):
    session = "sess-restart-expired"
    I.mark_planned_restart(session)
    marker = I.planned_restart_marker(session)
    assert marker.exists()

    old_time = time.time() - 200
    os.utime(marker, (old_time, old_time))

    assert I.consume_planned_restart(session) is False
    assert not marker.exists()


def test_session_end_hook_skips_when_planned_restart_marked(tmp_path, monkeypatch):
    c = cfg()
    monkeypatch.setattr(H, "load_config", lambda: c)
    spawned = []
    monkeypatch.setattr(H, "spawn", lambda cmd, **kw: spawned.append(cmd))

    session = "sess-hook-end"
    I.mark_planned_restart(session)

    payload = json.dumps({"session_id": session, "hook_event_name": "SessionEnd"})
    ret = H.hook_lifecycle(payload)
    assert ret == 0
    assert spawned == []

    # Second call: marker was consumed, notice runs
    ret2 = H.hook_lifecycle(payload)
    assert ret2 == 0
    assert len(spawned) == 1
    assert spawned[0] == ["lifecycle", f"--session={session}", "--kind=session-end"]


def test_mirror_run_lifecycle_consumes_planned_restart(tmp_path, bot):
    c = cfg()
    session = "sess-mirror-end"
    topics = TopicMap(state_dir() / "topics.json", bot, OWNER)
    topics.ensure(session, "Title")
    bot.calls.clear()  # the topic was created above; only the lifecycle notice is under test

    I.mark_planned_restart(session)
    res = M.run_lifecycle(c, session, "session-end", bot=bot)
    assert res == 0
    assert not bot.calls


# --- 2. Finding tmux pane from process record -----------------------------------

def test_pane_from_process_success(tmp_path, monkeypatch):
    session = "sess-pid-proc"
    sessions_dir = status.projects_dir().parent / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    info = {"sessionId": session, "pid": 4321, "tmux": "name:@0.%77"}
    (sessions_dir / "4321.json").write_text(json.dumps(info), encoding="utf-8")

    monkeypatch.setattr(os, "kill", lambda pid, sig: None)

    def fake_tmux(*args):
        if args[:4] == ("display", "-p", "-t", "%77"):
            return subprocess.CompletedProcess(args, 0, stdout="4321\n")
        return subprocess.CompletedProcess(args, 1, stdout="")

    monkeypatch.setattr(I, "_tmux", fake_tmux)
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="1000\n"))

    assert I._pane_from_process(session) == "%77"
    assert I.pane_for_session(session) == "%77"


def test_pane_from_process_parent_pid_match(tmp_path, monkeypatch):
    session = "sess-parent-match"
    sessions_dir = status.projects_dir().parent / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    info = {"sessionId": session, "pid": 5555, "tmux": "name:@0.%88"}
    (sessions_dir / "5555.json").write_text(json.dumps(info), encoding="utf-8")

    monkeypatch.setattr(os, "kill", lambda pid, sig: None)

    def fake_tmux(*args):
        if args[:4] == ("display", "-p", "-t", "%88"):
            return subprocess.CompletedProcess(args, 0, stdout="2000\n")  # parent pid
        return subprocess.CompletedProcess(args, 1, stdout="")

    monkeypatch.setattr(I, "_tmux", fake_tmux)
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="2000\n"))

    assert I.pane_for_session(session) == "%88"


def test_pane_from_process_mismatch_rejected(tmp_path, monkeypatch):
    session = "sess-mismatch"
    sessions_dir = status.projects_dir().parent / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    info = {"sessionId": session, "pid": 6666, "tmux": "name:@0.%99"}
    (sessions_dir / "6666.json").write_text(json.dumps(info), encoding="utf-8")

    monkeypatch.setattr(os, "kill", lambda pid, sig: None)

    def fake_tmux(*args):
        if args[:4] == ("display", "-p", "-t", "%99"):
            return subprocess.CompletedProcess(args, 0, stdout="99999\n")  # unrelated pid
        return subprocess.CompletedProcess(args, 1, stdout="")

    monkeypatch.setattr(I, "_tmux", fake_tmux)
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="2000\n"))

    assert I.pane_for_session(session) is None


# --- 4. Trust dialog ----------------------------------------------------------

def test_trusted_elsewhere(tmp_path):
    target_dir = str(tmp_path / "trusted_project")
    Path(target_dir).mkdir(parents=True, exist_ok=True)

    claude_json = accounts.claude_json()
    claude_json.parent.mkdir(parents=True, exist_ok=True)
    claude_json.write_text(json.dumps({"projects": {target_dir: {"hasTrustDialogAccepted": True}}}), encoding="utf-8")

    assert I.trusted_elsewhere(target_dir) is True
    assert I.trusted_elsewhere(str(tmp_path / "other_project")) is False


def test_trusted_elsewhere_in_profiles(tmp_path):
    profile_dir = Path(os.environ["CTB_PROFILES_DIR"]) / "work"
    profile_dir.mkdir(parents=True, exist_ok=True)
    target_dir = str(tmp_path / "work_project")
    Path(target_dir).mkdir(parents=True, exist_ok=True)

    (profile_dir / ".claude.json").write_text(
        json.dumps({"projects": {target_dir: {"hasTrustDialogAccepted": True}}}), encoding="utf-8")

    assert I.trusted_elsewhere(target_dir) is True


def trust_screen(cursor: int) -> str:
    """The current Claude Code dialog: numbered options, the cursor starts on "No, exit"."""
    mark = lambda n: "❯ " if cursor == n else "  "
    return ("\n Accessing workspace:\n\n ~/code/project\n\n"
            " Quick safety check: Is this a project you created or one you trust?\n\n"
            f" {mark(1)}1. Yes, I trust this folder\n {mark(2)}2. No, exit\n\n Enter to confirm · Esc to cancel\n")


class TrustPane:
    """A pane showing the trust dialog; Down/Up move the cursor, Enter on option 1 accepts and shows the prompt."""

    def __init__(self, cursor=2, options=2):
        self.cursor, self.options, self.keys, self.accepted, self.exited = cursor, options, [], False, False

    def __call__(self, *args):
        if args[0] == "capture-pane":
            if self.accepted:
                return subprocess.CompletedProcess(args, 0, stdout="❯ ready")
            return subprocess.CompletedProcess(args, 0, stdout=trust_screen(self.cursor))
        if args[0] == "send-keys":
            key = args[-1]
            self.keys.append(key)
            if key == "Down":
                self.cursor = min(self.options, self.cursor + 1)
            elif key == "Up":
                self.cursor = max(1, self.cursor - 1)
            elif key == "Enter":
                if self.cursor == 1:
                    self.accepted = True
                else:
                    self.exited = True
        return subprocess.CompletedProcess(args, 0, stdout="")


def test_pass_trust_moves_the_cursor_to_yes_before_enter(monkeypatch):
    monkeypatch.setattr(I, "trusted_elsewhere", lambda path: True)
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    pane = TrustPane(cursor=2)
    monkeypatch.setattr(I, "_tmux", pane)
    assert I.pass_trust("%1", "/some/path", wait=2) is True
    assert pane.keys == ["Up", "Enter"] and pane.accepted and not pane.exited


def test_pass_trust_never_presses_enter_when_the_cursor_cannot_be_verified(monkeypatch):
    monkeypatch.setattr(I, "trusted_elsewhere", lambda path: True)
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    sent = []

    def fake(*args):
        if args[0] == "send-keys":
            sent.append(args[-1])
        # a dialog whose cursor line cannot be read
        return subprocess.CompletedProcess(args, 0, stdout="1. Yes, I trust this folder\n2. No, exit")

    monkeypatch.setattr(I, "_tmux", fake)
    assert I.pass_trust("%1", "/some/path", wait=2) is False
    assert "Enter" not in sent


def test_pass_trust_rejected_when_untrusted(monkeypatch):
    monkeypatch.setattr(I, "trusted_elsewhere", lambda path: False)
    pane = TrustPane(cursor=2)
    monkeypatch.setattr(I, "_tmux", pane)
    assert I.pass_trust("%1", "/some/untrusted", wait=2) is False
    assert pane.keys == []


# --- 5. New clean project ------------------------------------------------------

def test_project_picker_has_new_clean_button(tmp_path, bot):
    c = cfg()
    m = {"message_id": 10, "message_thread_id": 300, "text": "start new app"}
    res = I.project_picker(c, bot, m, "start new app")
    assert res == "picker"

    call = bot.calls[0][1]
    buttons = [b for row in call["reply_markup"]["inline_keyboard"] for b in row]
    new_btn = next((b for b in buttons if b["callback_data"].endswith(":new")), None)
    assert new_btn is not None
    assert "חדש" in new_btn["text"] or "clean" in new_btn["text"].lower()


def test_project_chosen_new_clean(tmp_path, bot, monkeypatch):
    c = cfg()
    topics = TopicMap(state_dir() / "topics.json", bot, OWNER)
    token = "abcdef0123456789"

    root = tmp_path / "state"
    root.mkdir(parents=True, exist_ok=True)
    pending_path = root / "projects-pending.json"
    pending_path.write_text(json.dumps({"400": {"task": "build something", "dirs": ["/dir/a"], "token": token}}),
                           encoding="utf-8")

    tmux_calls = []

    def fake_tmux(*args):
        tmux_calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="❯")

    monkeypatch.setattr(I, "_tmux", fake_tmux)

    m = {"message_id": 15, "message_thread_id": 400}
    before = set(threading.enumerate())
    res = I.project_chosen(c, bot, m, topics, token, "new")
    assert res == "picker"
    # first_prompt types the task from a thread; left running it outlives this test's fake tmux and
    # sends the keys through the real one (found 2026-11-02 behind a shim)
    for worker in set(threading.enumerate()) - before:
        worker.join(timeout=15)

    new_sess = next(call for call in tmux_calls if call[0] == "new-session")
    full_cmd = new_sess[-1].split(GUARD_END, 1)[-1]  # the session's own line, after the launch preamble
    # one && chain, no -p: Claude never starts unless the new folder was made and entered
    assert "mkdir -p" not in full_cmd
    assert " && cd " in full_cmd and "&& git init -q && " in full_cmd and "; " not in full_cmd.split("--remote-control")[0]
    assert full_cmd.index("mkdir ") < full_cmd.index("git init") < full_cmd.index("--session-id")
    assert "project-" in full_cmd


# --- 6. Fixed keyboard & Live info row ----------------------------------------

def test_fixed_keyboard_layout():
    assert any(b["text"] == "🎚️ חשיבה" for row in M.KEYBOARD["keyboard"] for b in row)
    assert any(b["text"] == "🧠 מודל" for row in M.KEYBOARD["keyboard"] for b in row)
    assert any(b["text"] == "🎚️ Thinking" for row in M.KEYBOARD_EN["keyboard"] for b in row)
    assert any(b["text"] == "🧠 Model" for row in M.KEYBOARD_EN["keyboard"] for b in row)

    more = I.more_markup()
    more_texts = [b["text"] for row in more["inline_keyboard"] for b in row]
    assert any("מצב" in t or "Status" in t for t in more_texts)
    assert any("מסך" in t or "Screen" in t for t in more_texts)


def test_live_keyboard_model_and_thinking_values(tmp_path):
    session = "sess-values"
    write_private(state_dir() / "session-models.json", json.dumps({session: "Claude Sonnet 3.5|high"}))

    topics = TopicMap(state_dir() / "topics.json", None, OWNER)
    topics.ensure(session, "Title", thread=501)

    kb = M.live_keyboard("הקשר 10 אלף · Claude 20%", thread=501)
    all_texts = [b["text"] for row in kb["keyboard"] for b in row]
    assert any("🧠 Sonnet 3.5" in t for t in all_texts)
    assert any("🎚️ גבוהה" in t for t in all_texts)


def test_live_keyboard_account_button(tmp_path, monkeypatch):
    session = "sess-acct-btn"
    topics = TopicMap(state_dir() / "topics.json", None, OWNER)
    topics.ensure(session, "Title", thread=502)

    monkeypatch.setattr(accounts, "account_for", lambda s: "super-account-long-name")
    monkeypatch.setattr(accounts, "email", lambda a: "superdeveloper@example.net")
    monkeypatch.setattr(accounts, "multiple", lambda: True)

    kb = M.live_keyboard("הקשר 2 אלף", thread=502)
    info_row = kb["keyboard"][0]
    acct_btn = next((b for b in info_row if b["text"].startswith("👤 ")), None)
    assert acct_btn is not None
    # 👤 superdeve… (truncated to 10 chars with ellipsis)
    assert len(acct_btn["text"][2:]) <= 10


def test_live_keyboard_quota_short_footer():
    kb = M.live_keyboard("קומפקטי · ⏱️ 64%")
    info_row = kb["keyboard"][0]
    quota_btn = next((b for b in info_row if b["text"].startswith("⏱️ ")), None)
    assert quota_btn is not None
    assert quota_btn["text"] == "⏱️ 64%"


def test_handle_fixed_keyboard_taps(tmp_path, bot, monkeypatch):
    c = cfg()
    topics = TopicMap(state_dir() / "topics.json", bot, OWNER)
    thread = topics.ensure("sess-taps", "Title")
    inbox = I.Inbox(state_dir() / "inbox")
    media = TextMedia()

    ret1 = I.handle(update("🎚️ חשיבה", thread), c, bot, topics, inbox, media, time.time())
    assert ret1 == "effort"

    ret2 = I.handle(update("🧠 מודל", thread), c, bot, topics, inbox, media, time.time())
    assert ret2 == "model"

    monkeypatch.setattr(accounts, "names", lambda: ["main", "work"])  # the cell names an account there is
    ret3 = I.handle(update("👤 work", thread), c, bot, topics, inbox, media, time.time())
    assert ret3 == "account"

    ret4 = I.handle(update("⏱️ 40%", thread), c, bot, topics, inbox, media, time.time())
    assert ret4 == "quota"


# --- no claude: decided before any window is opened (review of #221) ----------------------------------------

def _no_claude(monkeypatch):
    from ctb import usershell

    monkeypatch.setattr(usershell, "probe", lambda *a, **k: False)  # the shell ran and did not find claude
    calls = []
    monkeypatch.setattr(I, "_tmux", lambda *a: calls.append(a) or subprocess.CompletedProcess(a, 0, stdout="❯"))
    return calls


def _sent_texts(bot):
    return [str(p.get("text") or "") for method, p in bot.calls if method in ("sendMessage", "editMessageText")]


def test_opening_a_new_project_without_claude_reports_failure_and_opens_no_window(tmp_path, bot, monkeypatch):
    calls = _no_claude(monkeypatch)
    topics = TopicMap(state_dir() / "topics.json", bot, OWNER)
    token = "abcdef0123456789"
    root = tmp_path / "state"
    root.mkdir(parents=True, exist_ok=True)
    (root / "projects-pending.json").write_text(
        json.dumps({"400": {"task": "build something", "dirs": ["/dir/a"], "token": token}}), encoding="utf-8")
    res = I.project_chosen(cfg(), bot, {"message_id": 15, "message_thread_id": 400}, topics, token, "new")
    assert res == "unsupported"
    assert not [c for c in calls if c[0] == "new-session"]
    assert any(i18n.t("project_open_failed", lang="he") in text for text in _sent_texts(bot))


def test_resuming_without_claude_fails_and_opens_no_window(tmp_path, monkeypatch):
    calls = _no_claude(monkeypatch)
    monkeypatch.setattr(I, "_pane_command", lambda pane: None)  # no window yet
    monkeypatch.setattr(I, "session_folder", lambda s: str(tmp_path))
    assert I.resume_session("5373d775-0000-4000-8000-000000000001") is False
    assert not [c for c in calls if c[0] == "new-session"]


def test_an_account_login_without_claude_says_so_at_once(tmp_path, monkeypatch):
    calls = _no_claude(monkeypatch)
    monkeypatch.setattr(I.accounts, "add", lambda name: None)
    monkeypatch.setattr(I.accounts, "config_dir", lambda name: tmp_path)
    said = []
    I.account_add(cfg(), None, "account2", lambda text, **k: said.append(text))
    assert said == [i18n.t("account_login_failed_start", lang="he")] and calls == []
