"""The `ctb` command line."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
from typing import List, Optional

from . import __version__
from . import config as C


MIRROR_TITLE = "Claude"  # `mirror` with no --title
# helpers the hooks start that only SEND (no local state), by the kind scheduled.suppressed() knows
SEND_ONLY_HELPERS = {"refresh-status": "status", "typing": "typing"}

def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="harnessgram", description="Harnessgram: Telegram remote control for Claude Code.",
                                epilog="First run: harnessgram setup   (then: harnessgram doctor)")
    p.add_argument("--version", action="version", version=f"Harnessgram {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="command")

    sub.add_parser("hook-stop", help="Claude Code Stop hook (reads the hook JSON on stdin)")
    sub.add_parser("hook-stop-failure", help="Claude Code StopFailure hook: fallback on usage limit (stdin: hook JSON)")
    sub.add_parser("hook-prompt", help="Claude Code UserPromptSubmit hook (reads the hook JSON on stdin)")
    sub.add_parser("hook-post", help="Claude Code PostToolUse hook: deliver Telegram messages mid-step "
                                     "(stdin: hook JSON)")
    sub.add_parser("hook-ask", help="Claude Code PreToolUse hook for AskUserQuestion: record the question "
                                    "(stdin: hook JSON; never blocks)")
    sub.add_parser("hook-notify", help="Claude Code Notification hook: ring the phone when the session needs you "
                                       "(stdin: hook JSON)")
    sub.add_parser("hook-wake", help="async Stop hook: wake an idle session from Telegram (stdin: hook JSON)")
    sub.add_parser("hook-start", help="record the session's terminal pane (stdin: hook JSON)")
    sub.add_parser("hook-lifecycle", help="handle session lifecycle events")
    lc = sub.add_parser("lifecycle", help=argparse.SUPPRESS)
    lc.add_argument("--session", required=True)
    lc.add_argument("--kind", required=True)
    lc.add_argument("--agent-type", default="")
    lc.add_argument("--agent-id", default="")
    lc.add_argument("--description", default="")
    lc.add_argument("--agent-transcript", default="")
    lc.add_argument("--transcript", default="")
    finish = sub.add_parser("finish-stop", help=argparse.SUPPRESS)
    finish.add_argument("--text-file", required=True)
    finish.add_argument("--delete-text-file", action="store_true")
    pm = sub.add_parser("prompt-mirror", help=argparse.SUPPRESS)
    pm.add_argument("--session", required=True)
    pm.add_argument("--text-file", required=True)
    pm.add_argument("--delete-text-file", action="store_true")
    sl = sub.add_parser("session-log", help="send a session's full work log (built from its transcript) to its topic")
    sl.add_argument("session")
    sl.add_argument("--all", action="store_true", help="the whole session, not only what is new since the last log")
    sub.add_parser("serve", help="run the Telegram listener (long-poll) in the foreground")
    share = sub.add_parser("share-serve", help="serve shared files on loopback")
    share.add_argument("--port", type=int, required=True)
    panel = sub.add_parser("panel", help="the control panel (Telegram Mini App)")
    panel_sub = panel.add_subparsers(dest="panel_cmd", required=True, metavar="action")
    ps = panel_sub.add_parser("serve", help="serve the panel on 127.0.0.1")
    ps.add_argument("--port", type=int, default=8765)
    st = sub.add_parser("studio", help="the Studio: a Cockpit app for directing videos from the phone")
    st_sub = st.add_subparsers(dest="studio_cmd", required=True, metavar="action")
    st_sub.add_parser("install", help="register the Studio as a Cockpit app (writes its folder; run again after an update)")
    rd = sub.add_parser("radio", help="team radio: a narrated live stream in the home group (macOS)")
    rds = rd.add_subparsers(dest="radio_cmd", required=True, metavar="action")
    rds.add_parser("start", help="open the group's live stream and start the radio")
    rds.add_parser("stop", help="stop the radio and end the live stream")
    rst = rds.add_parser("status", help="print the radio's state as JSON")
    rst.add_argument("--live", action="store_true", help="also ask Telegram about the live stream")
    rds.add_parser("say", help="speak one line on the radio now").add_argument("text")
    rds.add_parser("run", help=argparse.SUPPRESS)  # the launchd job
    rpm = rds.add_parser("prepare-motion", help="render the flight board's ident and 'landed' sting once (needs "
                                                "node, puppeteer-core and Chrome)")
    rpm.add_argument("--modules", default="", help="a folder that holds node_modules/puppeteer-core")
    rpm.add_argument("--chrome", default="", help="the Chrome or Chromium program")
    rpm.add_argument("--node", default="node", help="the node program")
    ryt = rds.add_parser("youtube", help="the staff edition on YouTube (unlisted live; off by default)")
    ryts = ryt.add_subparsers(dest="youtube_cmd", required=True, metavar="action")
    yst = ryts.add_parser("status", help="print the staff radio's state as JSON (works offline)")
    yst.add_argument("--live", action="store_true", help="also ask YouTube about the channel and the broadcast")
    ryts.add_parser("connect", help="sign in to YouTube and pick the channel (stores the sign-in in the Keychain)")
    yss = ryts.add_parser("start", help="go live: create an unlisted broadcast and start the staff radio (the owner's action)")
    yss.add_argument("--request", help=argparse.SUPPRESS)  # the Cockpit's confirmed start request id
    ryts.add_parser("stop", help="stop the staff radio and end its broadcast")
    ryts.add_parser("disconnect", help="forget the channel and remove the stored keys")
    ryts.add_parser("run", help=argparse.SUPPRESS)  # the launchd job
    dashboard = sub.add_parser("dashboard", help="update the session dashboard")
    dashboard.add_argument("--daily", action="store_true", help="send the morning summary")
    dashboard.add_argument("--print", dest="print_only", action="store_true", help="print without sending")

    m = sub.add_parser("mirror", help="send a reply (stdin or --text-file) or a --file to the session's topic")
    m.add_argument("--title", default=MIRROR_TITLE, help="session title (names the topic)")
    m.add_argument("--session", default=None, help="session id (default: the title; with --file, the session "
                                                   "this command runs inside)")
    m.add_argument("--text-file", help="read the reply from this file instead of stdin")
    m.add_argument("--delete-text-file", action="store_true", help="remove --text-file after reading it")
    m.add_argument("--file", help="send this file (photo, playable audio/video, or document) instead of text")
    m.add_argument("--caption", default="", help="file caption")
    m.add_argument("--audio-title", help="audio player title (default: cleaned file name)")
    m.add_argument("--performer", help="audio player performer")
    m.add_argument("--reply-chat", type=int, help="chat of the message this turn answers")
    m.add_argument("--reply-to", type=int, help="message this turn answers in --reply-chat")
    m.add_argument("--translate", dest="translate", action="store_true", default=None,
                   help="translate to target_language (default: follow the config)")
    m.add_argument("--no-translate", dest="translate", action="store_false")

    m.add_argument("--transcript", default="", help="session transcript, for the context-size footer")

    tw = sub.add_parser("translate-warm", help=argparse.SUPPRESS)  # fill the translation cache (detached)
    tw.add_argument("--text-file", required=True)
    tw.add_argument("--delete-text-file", action="store_true")
    pr = sub.add_parser("progress", help="update (or with --end delete) the live progress message (used by hooks)")
    pr.add_argument("--session", required=True)
    pr.add_argument("--end", action="store_true")
    pr.add_argument("--jobs", action="store_true", help=argparse.SUPPRESS)  # translate what was shown in English
    nt = sub.add_parser("notify", help="ring the phone in a session's topic (used by hook-notify)")
    nt.add_argument("--session", required=True)
    nt.add_argument("--type", default="")
    ss = sub.add_parser("session-state", help=argparse.SUPPRESS)
    ss.add_argument("--session", required=True)
    ss.add_argument("--state", required=True, choices=("busy", "waiting", "done", "bottom"))
    dn = sub.add_parser("direct-note", help=argparse.SUPPRESS)  # something done directly in Claude, shown in the topic
    dn.add_argument("--session", required=True)
    dn.add_argument("--field", required=True)
    dn.add_argument("--value", default="")
    rs = sub.add_parser("refresh-status", help=argparse.SUPPRESS)
    rs.add_argument("--session", required=True)
    rs.add_argument("--transcript", required=True)
    rs.add_argument("--source", default="compact", choices=("compact", "clear", "startup", "resume"))
    hb = sub.add_parser("heartbeat", help="keep the working placeholder moving while sessions really work "
                                          "(runs until stopped; run it under a service manager)")
    hb.add_argument("--once", action="store_true", help="one round, then exit")
    hb.add_argument("--every", type=float, default=12.0, help="seconds between rounds")
    ac = sub.add_parser("auto-compact", help=argparse.SUPPRESS)  # spawned by hooks, detached
    ac.add_argument("session")
    ac.add_argument("ctx", type=int)
    ac.add_argument("cold", nargs="?", default="")
    ac.add_argument("--note", default="")
    ac.add_argument("--asked", action="store_true")
    cp = sub.add_parser("compact", help="ask for a compaction of this session when the current turn ends "
                                        "(run by the agent inside its session, at a natural break)")
    cp.add_argument("--note", default="", help="one line: what the summary must keep")
    cp.add_argument("--cancel", action="store_true", help="take the request back")
    cp.add_argument("--session", default="", help="session id (defaults to CLAUDE_CODE_SESSION_ID)")
    sub.add_parser("topic-icons", help="pick a fitting icon for every topic whose name changed")
    dl = sub.add_parser("delivered", help="react to messages a hook delivered (used by hooks)")
    dl.add_argument("--refs", default="", help="chat:message_id,...")
    dl.add_argument("--lease", default="", help="session:nonce of an in-flight delivery to confirm from the transcript")
    dp = sub.add_parser("dispatcher", help="the session that receives messages written outside any session topic")
    dps = dp.add_subparsers(dest="disp_cmd", required=True)
    ds = dps.add_parser("set")
    ds.add_argument("session")
    dps.add_parser("show")
    dps.add_parser("clear")

    ty = sub.add_parser("typing", help="show 'typing...' in a session's topic (used by hook-post)")
    ty.add_argument("--session", required=True)

    tm = sub.add_parser("team", help="team group: share sessions with a few people (local command only)")
    ts = tm.add_subparsers(dest="team_cmd", required=True)
    ts.add_parser("show", help="print the team settings")
    sc = ts.add_parser("set-chat", help="the supergroup (negative id) where the bot is an admin")
    sc.add_argument("chat_id", type=int)
    am = ts.add_parser("add-member", help="allow a Telegram user id to send SUGGESTIONS to shared sessions")
    am.add_argument("user_id", type=int)
    am.add_argument("name")
    rm = ts.add_parser("remove-member")
    rm.add_argument("user_id", type=int)
    sh = ts.add_parser("share", help="mirror a session to the team group (replies are redacted)")
    sh.add_argument("session")
    un = ts.add_parser("unshare")
    un.add_argument("session")

    pj = sub.add_parser("project", help="project groups: one forum supergroup per project, next to the home group "
                                        "(local command only)")
    pjs = pj.add_subparsers(dest="project_cmd", required=True)
    rules = argparse.ArgumentParser(add_help=False)
    rules.add_argument("--folder", action="append", default=[], metavar="PATH",
                       help="a folder the /new picker offers in this group (repeatable)")
    rules.add_argument("--paths", action="append", default=[], metavar="PATH",
                       help="sessions started in this folder (or below; * and ? work) belong here (repeatable)")
    rules.add_argument("--repos", action="append", default=[], metavar="NAME",
                       help="sessions in a git repository or worktree with this folder or origin name (repeatable)")
    rules.add_argument("--title-prefix", action="append", default=[], metavar="TEXT",
                       help="sessions whose title starts with TEXT: belong here (repeatable)")
    pa = pjs.add_parser("add", parents=[rules], help="add a project group you already created")
    pa.add_argument("name")
    pa.add_argument("--chat", type=int, required=True, help="the supergroup id (-100...)")
    pjs.add_parser("list", help="print the project groups and their match rules")
    pr_ = pjs.add_parser("remove", help="drop a project group; its sessions move home on their next activity")
    pr_.add_argument("name")
    pp = pjs.add_parser("pair", parents=[rules], help="find the group by sending /start@<bot> in it, and save it")
    pp.add_argument("name")

    i = sub.add_parser("install", help="install the listener and the scheduled jobs and merge the hooks into ~/.claude/settings.json")
    i.add_argument("--dry-run", action="store_true", help="show what would change; write nothing")
    i.add_argument("--no-service", action="store_true", help="hooks only; do not write the listener service")
    i.add_argument("--no-schedules", action="store_true", help="listener only; skip the dashboard, heartbeat and "
                                                               "daily summary jobs")
    i.add_argument("--no-wake", action="store_true", help="skip the async wake hook (messages then arrive at the "
                                                           "end of a step only)")
    i.add_argument("--no-start", action="store_true", help="write the service file but do not start it")
    i.add_argument("--settings", help="settings.json to edit (default: ~/.claude/settings.json)")

    w = sub.add_parser("wait-idle", help="wait until no session's Stop worker is running (run before "
                                         "`uv tool install --force`); exit 1 on timeout")
    w.add_argument("--timeout", type=float, default=30.0)

    u = sub.add_parser("uninstall", help="remove our hooks, the listener and the scheduled jobs")
    u.add_argument("--dry-run", action="store_true")
    u.add_argument("--settings")

    d = sub.add_parser("doctor", help="check config, bot, Threaded Mode, listener and hooks")
    d.add_argument("--offline", action="store_true", help="skip the Telegram check")

    st = sub.add_parser("setup", help="guided first run: BotFather steps, token check, pairing, config, install, "
                                      "test message, doctor")
    st.add_argument("--owner-id", type=int, help="your numeric Telegram id (skips the /start pairing step)")
    st.add_argument("--language", help="language replies are shown in (ISO code, default: ask, then en)")
    st.add_argument("--yes", action="store_true", help="do not ask to confirm the paired account")
    st.add_argument("--non-interactive", action="store_true",
                    help="never prompt (CI, scripts): token from CTB_BOT_TOKEN, needs --owner-id")
    st.add_argument("--no-install", action="store_true", help="do not install the service and the hooks")
    st.add_argument("--no-service", action="store_true", help="hooks only; no listener service")
    st.add_argument("--no-start", action="store_true", help="write the service file but do not start it")
    st.add_argument("--no-test-message", action="store_true", help="do not send the 'connected' message")
    st.add_argument("--no-doctor", action="store_true", help="do not run doctor at the end")
    st.add_argument("--settings", help="settings.json to edit (default: ~/.claude/settings.json)")
    st.add_argument("--web", action="store_true",
                    help="the same steps as a guided page in your browser (local only; --language he|en sets "
                         "the page language, default: the browser's)")

    ba_ = sub.add_parser("bot-api", help="move the bot between Telegram's cloud server and your own local Bot API "
                                         "server (the token is read from the saved config)")
    ba_.add_argument("action", choices=("logout", "close"),
                     help="logout: leave the cloud server (once, before your own server's first start); "
                          "close: leave your own server (before going back to the cloud)")
    ba_.add_argument("--yes", action="store_true", help="do not ask for confirmation")

    pg = sub.add_parser("pair", help="find your chat id by sending /start to the bot, and save it in the config")
    pg.add_argument("--group", action="store_true",
                    help="after pairing: use a group with topics as the home chat (send /start@<bot> there)")
    pg.add_argument("--private", action="store_true", help="go back to the private chat with the bot")

    sub.add_parser("statusline", help="Claude Code statusLine command: saves quota and model/thinking level "
                                      "per session and account (stdin: status JSON)")
    ac = sub.add_parser("account", help=argparse.SUPPRESS)
    acs = ac.add_subparsers(dest="account_cmd", required=True)
    am_ = acs.add_parser("auto-move", help="move a session to another Claude account after a quota limit (used by the mirror)")
    am_.add_argument("--session", required=True)
    am_.add_argument("--thread", type=int, required=True)
    am_.add_argument("--target", required=True)
    am_.add_argument("--idle", action="store_true", help="idle rebalance move: do not type continue")

    t = sub.add_parser("take", help="print and clear a session's Telegram queue (debugging)")
    t.add_argument("session")

    ap = sub.add_parser("autopilot", help="continue idle sessions on quota return or in away mode")
    ap.add_argument("--dry-run", action="store_true", help="show what would run; send and type nothing")

    tr = sub.add_parser("topic-rename", help="rename a session's Telegram topic")
    tr.add_argument("name", help="new name in short Hebrew (project: task)")
    tr.add_argument("session", nargs="?", default="", help="session id (defaults to CLAUDE_CODE_SESSION_ID)")
    tk = sub.add_parser("tasks", help="scheduled tasks: list them, or mute/unmute one (a muted task sends nothing "
                                      "to Telegram)")
    tk.add_argument("action", choices=("list", "mute", "unmute"))
    tk.add_argument("name", nargs="?", default="", help="the task's name (mute/unmute)")
    tn = sub.add_parser("topic-names", help="name session topics now (project: task); --dry-run only shows")
    tn.add_argument("--dry-run", action="store_true", help="show the names it would set; rename nothing")
    tn.add_argument("--limit", type=int, default=0, help="most topics to name in this run (default: a round)")
    tn.add_argument("--unlock", default="", metavar="SESSION", help="let the bridge name this topic again")
    bd = sub.add_parser("board", help="this topic's pin board: links, a checklist and reminders for the owner "
                                      "(run by the agent inside its session)")
    where = argparse.ArgumentParser(add_help=False)
    where.add_argument("--session", default="", help="session id (defaults to CLAUDE_CODE_SESSION_ID)")
    bds = bd.add_subparsers(dest="board_cmd", required=True, metavar="action")
    ba = bds.add_parser("add", parents=[where], help="pin a link (default: the latest reply)")
    ba.add_argument("text", metavar="title")
    ba.add_argument("url", nargs="?", default="", help="http(s) or t.me link; omitted = the latest reply")
    bds.add_parser("check", parents=[where], help="add a checklist item").add_argument("text")
    br = bds.add_parser("remind", parents=[where], help="add something waiting on the owner")
    br.add_argument("text")
    br.add_argument("--due", default="", help='short text such as "until 18:00"')
    for name, text in (("done", "tick a check item or clear a reminder"), ("remove", "take an item off the board")):
        bds.add_parser(name, parents=[where], help=text).add_argument("n", type=int, help="the item number")
    bds.add_parser("list", parents=[where], help="print the board's items with their numbers")
    dc = sub.add_parser("decide", help="leave a yes/no decision for the owner in the control panel "
                                       "(run by the agent inside its session)")
    dcs = dc.add_subparsers(dest="decide_cmd", required=True, metavar="action")
    da = dcs.add_parser("ask", help="ask a question; the owner answers with a tap in the panel")
    da.add_argument("title", help="the question, up to 200 characters")
    da.add_argument("--detail", default="", help="more context, up to 1500 characters")
    da.add_argument("--yes", default="", help="label of the yes button (30 characters)")
    da.add_argument("--no", default="", help="label of the no button (30 characters)")
    dcs.add_parser("list", help="print this session's open decisions")
    dcs.add_parser("cancel", help="withdraw one of this session's open decisions").add_argument("id")
    bk = sub.add_parser("bank", help="the reset bank: saved quota resets per provider, shown in the control "
                                     "panel and told to agents")
    bks = bk.add_subparsers(dest="bank_cmd", metavar="action")
    bset = bks.add_parser("set", help="set a provider's saved resets, for example: bank set claude 1")
    bset.add_argument("provider")
    bset.add_argument("n", type=int)
    bexp = bks.add_parser("expire", help="set when a provider's saved resets expire, for example: "
                                         "bank expire claude 2030-01-20 (none clears it)")
    bexp.add_argument("provider")
    bexp.add_argument("expires", metavar="YYYY-MM-DD|none")
    bks.add_parser("forget", help="drop a provider's row").add_argument("provider")
    cd = sub.add_parser("card", help="publish a live card to the owner's control panel: progress, a choice, "
                                     "a short question, a link or numbers (run by the agent inside its session)")
    cds = cd.add_subparsers(dest="card_cmd", required=True, metavar="kind")

    def card(name: str, text: str, edit: bool = True):
        p = cds.add_parser(name, help=text)
        p.add_argument("title", nargs="?" if name == "progress" else None, default="",
                       help="the card's title, up to 120 characters"
                            + (" (may be left out when updating with --id)" if name == "progress" else ""))
        p.add_argument("--detail", default="", help="one or two more lines, up to 600 characters")
        if edit:
            p.add_argument("--id", default="", help="replace this open card of yours in place")
        return p

    cp = card("progress", "steps and a percentage that move while you work; update it with --id")
    cp.add_argument("--step", action="append", default=[], help="a step name (repeat, up to 6)")
    cp.add_argument("--at", type=int, default=None, help="how many steps are finished")
    cp.add_argument("--percent", type=int, default=None, help="0 to 100")
    cc = card("choice", "ask the owner to pick one of 2 to 6 options, once", edit=False)
    cc.add_argument("--option", action="append", default=[], help="an option's label, up to 30 characters (repeat)")
    card("ask", "ask the owner for a short typed answer, once", edit=False)
    cl = card("link", "one https link with an open button")
    cl.add_argument("url", help="the https:// link")
    cl.add_argument("--label", default="", help="the button's label, up to 30 characters")
    cn = card("numbers", "up to three key figures")
    cn.add_argument("--figure", action="append", default=[], help="label=value (repeat, up to 3)")
    cds.add_parser("list", help="print this session's open cards")
    cds.add_parser("close", help="take one of this session's open cards off the panel").add_argument("id")
    bl = sub.add_parser("block", help="put a small block into the Cockpit's Morning Brief: a status sentence, "
                                      "a checklist or a needs-you line (JSON; run by the agent inside its session)")
    bls = bl.add_subparsers(dest="block_cmd", required=True, metavar="action")
    blp = bls.add_parser("post", help='send one block as JSON, for example {"kind": "status", "line": "studio", '
                                      '"text": "Three of eight screens are built."}; send it again with its "id" '
                                      "to update it")
    blp.add_argument("--json", dest="source", default="-", metavar="FILE|-", help="the JSON file, or - for stdin")
    bls.add_parser("list", help="print this session's blocks")
    bls.add_parser("close", help="take one of this session's blocks off the Cockpit").add_argument("id")
    # Help shows what people type first; hidden helpers vanish and the hooks Claude Code runs go last
    # (a clean install printed "==SUPPRESS==" rows and nine hook rows above `setup`).
    shown = [a for a in sub._choices_actions if a.help != argparse.SUPPRESS]
    first = ["setup", "doctor", "pair", "install", "uninstall"]
    sub._choices_actions[:] = (sorted([a for a in shown if a.dest in first], key=lambda a: first.index(a.dest))
                               + [a for a in shown if a.dest not in first and not a.dest.startswith("hook-")]
                               + [a for a in shown if a.dest.startswith("hook-")])
    return p


def _read_text(args) -> str:
    if args.text_file:
        with open(args.text_file, encoding="utf-8") as f:
            text = f.read()
        if args.delete_text_file:
            try:
                os.unlink(args.text_file)
            except OSError:
                pass
        return text
    return sys.stdin.read()


def _cmd_mirror(args) -> int:
    from . import mirror
    try:
        cfg = C.load()
    except C.ConfigError as e:
        print(f"Harnessgram: {e}", file=sys.stderr)
        return 1
    if not cfg.ready:
        print("Harnessgram: bot_token / owner_chat_id missing in the config", file=sys.stderr)
        return 1
    session = args.session or args.title
    if args.file:
        if not args.session and args.title == MIRROR_TITLE:
            # an agent sending a file names no session: it goes to the topic of the session it runs inside,
            # never to a general topic called "Claude" (three previews were lost that way)
            from . import decisions
            try:
                session = decisions.caller_session()
            except decisions.DecisionError as e:
                print(f"Harnessgram: {e.reason}; pass --session <id> to say which topic", file=sys.stderr)
                return 1
        return mirror.run_file(cfg, args.title, session, args.file, args.caption, args.audio_title, args.performer)
    translate = cfg.translate if args.translate is None else args.translate
    return mirror.run_reply(cfg, args.title, session, _read_text(args), translate, args.transcript,
                            answer_to={"chat": args.reply_chat, "id": args.reply_to})


def _cmd_helper(args) -> int:
    """Small detached helpers the hooks start: progress, notify, delivered."""
    from . import inbox, mirror, progress
    try:
        cfg = C.load()
    except C.ConfigError:
        return 1
    if not cfg.ready:
        return 1
    if args.cmd == "translate-warm":
        from pathlib import Path as _P
        from . import translate as _T
        src = _P(args.text_file)
        try:
            texts = json.loads(src.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            texts = []
        finally:
            if args.delete_text_file:
                src.unlink(missing_ok=True)
        _T.warm(texts if isinstance(texts, list) else [], cfg)
        return 0
    if args.cmd == "progress":
        if args.jobs:
            progress.run_jobs(cfg, args.session)
            return 0
        return (progress.run_end_progress if args.end else progress.run_send_progress)(cfg, args.session)
    if args.cmd == "notify":
        return mirror.run_notify(cfg, args.session, args.type)
    if args.cmd == "prompt-mirror":
        return mirror.run_prompt(cfg, args.session, _read_text(args))
    if getattr(args, "lease", ""):
        from . import sweeper
        session, _, nonce = args.lease.rpartition(":")
        return sweeper.run_confirm(cfg, session, nonce)
    return inbox.run_delivered(cfg, [r for r in args.refs.split(",") if r])


def _cmd_dispatcher(args) -> int:
    from . import dispatcher, team
    try:
        if args.disp_cmd == "set":
            session = team.resolve_session(args.session)
            dispatcher.set_dispatcher(session)
            print(f"dispatcher: {session}")
        elif args.disp_cmd == "show":
            print(dispatcher.get_dispatcher() or "(none)")
        else:
            print("cleared" if dispatcher.clear_dispatcher() else "(none was set)")
    except C.ConfigError as e:
        print(f"Harnessgram: {e}", file=sys.stderr)
        return 1
    return 0


def _cmd_typing(args) -> int:
    from . import mirror
    try:
        cfg = C.load()
    except C.ConfigError:
        return 1
    return mirror.run_typing(cfg, args.session) if cfg.ready else 1


def _cmd_team(args) -> int:
    """Only ever runs locally: the team's members and shared sessions cannot be changed from Telegram."""
    from . import team
    try:
        if args.team_cmd == "show":
            print(team.describe(C.load()))
            return 0
        if args.team_cmd == "set-chat":
            print(team.set_chat(args.chat_id))
        elif args.team_cmd == "add-member":
            print(team.add_member(args.user_id, args.name))
        elif args.team_cmd == "remove-member":
            print(team.remove_member(args.user_id))
        elif args.team_cmd == "share":
            print(team.share(args.session))
        elif args.team_cmd == "unshare":
            print(team.unshare(args.session))
    except C.ConfigError as e:
        print(f"Harnessgram: {e}", file=sys.stderr)
        return 1
    return 0


def _cmd_project(args) -> int:
    """Only ever runs locally: project groups cannot be changed from Telegram."""
    from . import projects
    from .telegram import Bot, TelegramError
    rules = dict(folders=args.folder, paths=args.paths, repos=args.repos, title_prefixes=args.title_prefix) \
        if args.project_cmd in ("add", "pair") else {}
    try:
        if args.project_cmd == "list":
            print(projects.describe(C.load()))
        elif args.project_cmd == "add":
            print(projects.add(args.name, args.chat, **rules))
        elif args.project_cmd == "remove":
            print(projects.remove(args.name))
        elif args.project_cmd == "pair":
            cfg = C.load()
            try:
                bot = Bot.from_config(cfg, strict=True)
            except TelegramError as e:
                raise C.ConfigError(e.description) from None
            print(projects.pair(cfg, bot, args.name, **rules) + " Restart the listener: harnessgram install.")
    except C.ConfigError as e:
        print(f"Harnessgram: {e}", file=sys.stderr)
        return 1
    return 0


def _reset_topics_for_mode_switch() -> None:
    """Topic ids belong to one chat: after switching between the private chat and a group the old
    map would point at threads that do not exist there (or at another session's). Kept as a backup."""
    from .config import state_dir
    old = state_dir() / "topics.json"
    if old.exists():
        old.replace(old.with_name(f"topics.before-switch-{int(time.time())}.json"))
    from .sessions_menu import CLOSED_FILE  # the close records name threads of the old chat
    try:
        (state_dir() / CLOSED_FILE).unlink()
    except OSError:
        pass


def _cmd_pair_group() -> int:
    """Group mode: the paired owner sends /start@<bot> in a forum supergroup where the bot is an
    admin with 'Manage topics'; its id becomes the home chat. Only the owner is ever obeyed there."""
    from . import pairing
    from .telegram import Bot, TelegramError
    path = C.config_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        cfg = C.load(path)
    except (OSError, ValueError, C.ConfigError):
        print("Harnessgram: pair the private chat first (harnessgram setup or harnessgram pair).", file=sys.stderr)
        return 1
    if not cfg.owner_chat_id:
        print("Harnessgram: pair the private chat first (harnessgram pair).", file=sys.stderr)
        return 1
    try:
        bot = Bot.from_config(cfg, strict=True)
        me = bot.call("getMe")
        print(f"In the group, with your own account, send (waiting up to 3 minutes):\n\n    /start@{me.get('username')}\n")
        found = pairing.wait_for_group_start(bot, me.get("username") or "", time.time(), cfg.owner_chat_id)
        if not found:
            print("Harnessgram: no /start from you in a group; nothing saved.", file=sys.stderr)
            return 1
        group = found["chat"]["id"]
        chat = bot.call("getChat", {"chat_id": group})
        mem = bot.call("getChatMember", {"chat_id": group, "user_id": me["id"]})
    except TelegramError as e:
        print(f"Harnessgram: {e.description}", file=sys.stderr)
        return 1
    if not chat.get("is_forum"):
        print("Harnessgram: turn on Topics in the group's settings, then run this again.", file=sys.stderr)
        return 1
    if not (mem.get("status") == "creator" or (mem.get("status") == "administrator" and mem.get("can_manage_topics"))):
        print("Harnessgram: make the bot an admin of the group with 'Manage topics', then run this again.",
              file=sys.stderr)
        return 1
    if raw.get("chat_id") != group:
        _reset_topics_for_mode_switch()
    before, raw = dict(raw), dict(raw, chat_id=group)
    print(f"Group mode on: {chat.get('title', group)}. Saved to {C.save_changes(before, raw, path)} (mode 600). "
          "Restart the listener: harnessgram install.")
    return 0


def _cmd_pair_private() -> int:
    path = C.config_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print("Harnessgram: no config yet.", file=sys.stderr)
        return 1
    before = dict(raw)
    if raw.pop("chat_id", None):
        _reset_topics_for_mode_switch()
    print(f"Back to the private chat. Saved to {C.save_changes(before, raw, path)}. Restart the listener: harnessgram install.")
    return 0


def _cmd_pair() -> int:
    """Save the token (typed hidden, never an argument) and the chat id of the account that sends
    the one-time code."""
    from . import pairing
    from .telegram import Bot, TelegramError, clean_token
    path = C.config_path()
    new_config = not path.exists()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    try:
        token = clean_token(raw.get("bot_token") or os.environ.get("CTB_BOT_TOKEN")
                            or getpass.getpass("Bot token from BotFather: "))
        bot = Bot(token)
        me = bot.call("getMe")
        code, started = pairing.new_code(), time.time()
        print(f"Bot @{me.get('username')}. Open it in Telegram and send exactly this message "
              f"(waiting up to 3 minutes):\n\n    /start {code}\n")
        found = pairing.wait_for_start(bot, code, started)
    except TelegramError as e:
        print(f"Harnessgram: {e.description}", file=sys.stderr)
        return 1
    if not found:
        print("Harnessgram: no matching /start received; nothing saved.", file=sys.stderr)
        return 1
    who = found.get("from") or {}
    name = " ".join(x for x in (who.get("first_name"), who.get("last_name")) if x) or "?"
    answer = input(f"Pair with {name} (@{who.get('username', '-')}, id {who.get('id')})? Only this account "
                   "will be able to control your sessions. [y/N] ")
    if answer.strip().lower() not in ("y", "yes"):
        print("Not paired.")
        return 1
    before = dict(raw)
    if new_config:
        from .setup import new_install_defaults
        new_install_defaults(raw)
    raw.update(bot_token=token, owner_chat_id=found["chat"]["id"])
    raw.setdefault("target_language", "he")
    raw.setdefault("translate", False)
    print(f"Saved to {C.save_changes(before, raw, path, replace_owner=True)} (mode 600). Next: harnessgram install, then harnessgram doctor.")
    try:
        from .i18n import t
        ui_lang = raw.get("ui_language", "he" if raw.get("target_language") == "he" else "en")
        bot.call("sendMessage", {"chat_id": found["chat"]["id"],
                                 "text": t("paired_message", lang=ui_lang)})
    except TelegramError:
        pass
    return 0


def _saved_token() -> bool:
    try:
        import json
        raw = json.loads(C.config_path().read_text(encoding="utf-8"))
        return bool(raw.get("bot_token") or raw.get("bot_token_command"))
    except (OSError, ValueError, AttributeError):
        return False


def _cmd_tasks(args) -> int:
    from . import scheduled
    if args.action == "list":
        known = scheduled.tasks()
        if not known:
            print("No scheduled task seen yet.")
        for name, entry in known.items():
            where = ", ".join(f"topic {thread}" for _, thread in entry["topics"]) or "no topic yet"
            print(f"{name}\t{'muted' if entry['muted'] else 'on'}\t{where}")
        return 0
    if not scheduled.valid_name(args.name):
        print("Harnessgram: give the task's name (letters, digits, . _ -; at most 64).", file=sys.stderr)
        return 2
    scheduled.set_muted(args.name, args.action == "mute")
    print(f"{args.name}: {'muted' if args.action == 'mute' else 'on'}")
    return 0


# Commands that work while mirroring is off: they set the bridge up or only read and change local files.
LOCAL_ONLY = {"doctor", "setup", "install", "uninstall", "pair", "project", "team", "bank", "dispatcher",
              "wait-idle", "statusline", "compact", "tasks", "studio"}
# Long-running services: they start either way and check the switch at every round or request themselves.
SERVICES = {"serve", "heartbeat", "panel", "share-serve", "radio"}
# Everything else talks to Telegram, the relay, an outside model or a session's queue: nothing while off.
GUARDED = {"account", "auto-compact", "autopilot", "block", "board", "bot-api", "card", "dashboard", "decide", "delivered",
           "direct-note", "finish-stop", "hook-ask", "hook-lifecycle", "hook-notify", "hook-post", "hook-prompt",
           "hook-start", "hook-stop", "hook-stop-failure", "hook-wake", "lifecycle", "mirror", "notify",
           "progress", "prompt-mirror", "refresh-status", "session-log", "session-state", "take", "topic-icons",
           "topic-names", "topic-rename", "translate-warm", "typing"}


def _off(args) -> Optional[int]:
    """The entry check (policy.py): with mirroring off a guarded command does nothing. A hook returns 0 with no
    output (no message taken from the queue, no state changed); any other command says so locally."""
    from . import policy
    cmd = args.cmd
    if cmd == "pair" or (cmd == "project" and getattr(args, "project_cmd", "") == "pair"):
        # pairing reads (and so consumes) the bot's waiting messages: never while the config says off
        if policy.explicitly_off():
            print(policy.PAIR_OFF_NOTICE, file=sys.stderr)
            return 1
        return None
    guarded = cmd in GUARDED or (cmd == "radio" and getattr(args, "radio_cmd", "") in ("start", "say")) or (
        cmd == "radio" and getattr(args, "radio_cmd", "") == "youtube" and getattr(args, "youtube_cmd", "") == "start")
    if not guarded or policy.on():
        return None
    if cmd.startswith("hook-"):
        return 0
    print(policy.OFF_NOTICE, file=sys.stderr)
    return 1


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv)
    off = _off(args)
    if off is not None:
        return off
    cmd = args.cmd
    if cmd in SEND_ONLY_HELPERS and getattr(args, "session", ""):
        from .scheduled import suppressed
        if suppressed(args.session, SEND_ONLY_HELPERS[cmd]):
            return 0  # held back for a muted scheduled task (scheduled.py)
    if cmd == "tasks":
        return _cmd_tasks(args)
    if cmd == "studio":
        from .studio.install import InstallError, install as studio_install
        try:
            folder = studio_install()
        except InstallError as e:
            print(f"Harnessgram: {e}", file=sys.stderr)
            return 1
        print(f"Studio installed: {folder} (reopen the panel to see its tab)")
        return 0
    if cmd == "hook-stop":
        from .hooks import hook_stop
        return hook_stop(sys.stdin.read())
    if cmd == "hook-stop-failure":
        from .hooks import hook_stop_failure
        return hook_stop_failure(sys.stdin.read())
    if cmd == "hook-prompt":
        from .hooks import hook_prompt
        return hook_prompt(sys.stdin.read())
    if cmd == "hook-post":
        from .hooks import hook_post
        return hook_post(sys.stdin.read())
    if cmd == "hook-wake":
        from .wake import hook_wake
        return hook_wake(sys.stdin.read())
    if cmd == "hook-start":
        from .wake import hook_start
        return hook_start(sys.stdin.read())
    if cmd == "hook-lifecycle":
        from .hooks import hook_lifecycle
        return hook_lifecycle(sys.stdin.read())
    if cmd == "lifecycle":
        from .mirror import run_lifecycle
        try:
            if args.kind in ("subagent-start", "subagent-stop"):
                from .agent_owner import START_WAIT, STOP_WAIT, proven
                if not proven(args.session, args.agent_id, args.transcript, args.agent_transcript,
                              wait=START_WAIT if args.kind == "subagent-start" else STOP_WAIT):
                    return 0  # not proven to be this session's agent: dropped, never shown in a guessed topic
            cfg = C.load()
            run_lifecycle(cfg, args.session, args.kind, args.agent_type, args.description,
                          agent_transcript=args.agent_transcript, transcript=args.transcript,
                          agent_id=args.agent_id)
        except Exception:
            pass
        return 0
    if cmd == "finish-stop":
        from .hooks import finish_stop
        return finish_stop(json.loads(_read_text(args)))
    if cmd == "dashboard":
        from .dashboard import run
        try:
            cfg = C.load()
        except C.ConfigError:
            return 1
        code = run(cfg, daily=args.daily, print_only=args.print_only)
        if not args.print_only and not args.daily:
            from . import topic_icons
            try:  # outside any hook, so a slow Gemini pick only delays this scheduled command
                topic_icons.run(cfg)
            except Exception:
                pass
        return code
    if cmd == "session-log":
        from .sessionlog import send as send_log
        try:
            cfg = C.load()
        except C.ConfigError as e:
            print(f"Harnessgram: {e}", file=sys.stderr)
            return 1
        sent = send_log(cfg, args.session, everything=args.all)
        print("sent" if sent else "nothing to send")
        return 0 if sent else 1
    if cmd == "heartbeat":
        from .heartbeat import run as heartbeat_run
        try:
            cfg = C.load()
        except C.ConfigError as e:
            print(f"Harnessgram: {e}", file=sys.stderr)
            return 1
        return heartbeat_run(cfg, every=max(args.every, 3.0), once=args.once)
    if cmd == "topic-icons":
        from . import topic_icons
        try:
            cfg = C.load()
        except C.ConfigError as e:
            print(f"Harnessgram: {e}", file=sys.stderr)
            return 1
        print(topic_icons.run(cfg))
        return 0
    if cmd == "direct-note":
        from . import direct
        try:
            return direct.run_note(C.load(), args.session, args.field, args.value)
        except Exception:
            return 0
    if cmd == "refresh-status":
        from .wake import refresh_status, valid_transcript
        return 0 if valid_transcript(args.transcript) and refresh_status(args.session, args.transcript, source=args.source) else 1
    if cmd == "session-state":
        from .topics import set_state
        try:
            cfg = C.load()
        except C.ConfigError:
            return 1
        return set_state(cfg, args.session, args.state)
    if cmd == "serve":
        from .inbox import serve
        try:
            cfg = C.load()
        except C.ConfigError as e:
            print(f"Harnessgram: {e}", file=sys.stderr)
            return 1
        if not cfg.ready:
            print("Harnessgram: bot_token / owner_chat_id missing in the config (run `harnessgram pair`)", file=sys.stderr)
            return 1
        return serve(cfg)
    if cmd == "panel":
        from .panel import serve as panel_serve
        try:
            cfg = C.load()
        except C.ConfigError as e:
            print(f"Harnessgram: {e}", file=sys.stderr)
            return 1
        if not cfg.ready:
            print("Harnessgram: bot_token / owner_chat_id missing in the config (run `harnessgram pair`)", file=sys.stderr)
            return 1
        return panel_serve(cfg, args.port)
    if cmd == "radio":
        from .radio import cli as radio_cli
        try:
            cfg = C.load()
        except C.ConfigError as e:
            print(f"Harnessgram: {e}", file=sys.stderr)
            return 1
        if args.radio_cmd == "prepare-motion":
            from .radio import prepare_motion
            return prepare_motion(cfg, args.modules, args.chrome, args.node)
        if args.radio_cmd == "youtube":
            from .radio_youtube import cli as youtube_cli
            return youtube_cli(cfg, args.youtube_cmd, live=getattr(args, "live", False),
                               request=getattr(args, "request", None))
        return radio_cli(cfg, args.radio_cmd, text=getattr(args, "text", ""), live=getattr(args, "live", False))
    if cmd == "share-serve":
        from .share import serve
        serve(args.port)
        return 0
    if cmd == "mirror":
        return _cmd_mirror(args)
    if cmd == "wait-idle":
        from .hooks import wait_idle
        ok = wait_idle(args.timeout)
        print("idle" if ok else f"a Stop worker is still running after {args.timeout:g}s")
        return 0 if ok else 1
    if cmd == "install":
        from .install import install
        from pathlib import Path
        return install(dry_run=args.dry_run, service=not args.no_service, wake=not args.no_wake,
                       start=not args.no_start, settings_file=Path(args.settings) if args.settings else None,
                       schedules=not args.no_schedules)
    if cmd == "uninstall":
        from .install import uninstall
        from pathlib import Path
        return uninstall(dry_run=args.dry_run, settings_file=Path(args.settings) if args.settings else None)
    if cmd == "doctor":
        from .doctor import run
        return run(offline=args.offline)
    if cmd == "bot-api":
        from .botapi import run as botapi_run
        return botapi_run(args.action, assume_yes=args.yes)
    if cmd == "hook-ask":
        from .hooks import hook_ask
        return hook_ask(sys.stdin.read())
    if cmd == "hook-notify":
        from .hooks import hook_notify
        return hook_notify(sys.stdin.read())
    if cmd in ("progress", "notify", "delivered", "prompt-mirror", "translate-warm"):
        return _cmd_helper(args)
    if cmd == "auto-compact":
        import threading
        import time as _time
        from .inbox import auto_compact
        def settle():
            # The /compact keys and the progress message run in daemon threads: this process exists only
            # for them, so it stays until they finish (at most 20 minutes) (review of 42ff513).
            deadline = _time.time() + 1200
            while _time.time() < deadline:
                rest = [t for t in threading.enumerate() if t is not threading.current_thread() and t.is_alive()]
                if not rest:
                    break
                rest[0].join(timeout=max(0.1, min(5.0, deadline - _time.time())))

        if args.asked:  # the agent's own request (`ctb compact`): it stays until a compaction really ended
            from . import compact
            compact.run_asked(args.session, args.note or "",
                              lambda note: auto_compact(args.session, args.ctx, cold=args.cold == "cold",
                                                        note=note, asked=True), settle)
        else:
            auto_compact(args.session, args.ctx, cold=args.cold == "cold")
            settle()
        return 0
    if cmd == "dispatcher":
        return _cmd_dispatcher(args)
    if cmd == "typing":
        return _cmd_typing(args)
    if cmd == "team":
        return _cmd_team(args)
    if cmd == "project":
        return _cmd_project(args)
    if cmd == "pair":
        if getattr(args, "private", False):
            return _cmd_pair_private()
        return _cmd_pair_group() if getattr(args, "group", False) else _cmd_pair()
    if cmd == "setup":
        from pathlib import Path
        from . import setup as setup_mod
        if args.web:
            if args.non_interactive:
                print("--web is the guided page; it cannot be combined with --non-interactive.", file=sys.stderr)
                return 2
            from . import setup_page
            return setup_page.run_web(language=args.language, owner_id=args.owner_id,
                                      install_hooks=not args.no_install, service=not args.no_service,
                                      start=not args.no_start, test_message=not args.no_test_message,
                                      run_doctor=not args.no_doctor,
                                      settings_file=Path(args.settings) if args.settings else None)
        scripted = args.yes and args.owner_id and (os.environ.get("CTB_BOT_TOKEN") or _saved_token())
        if not args.non_interactive and not sys.stdin.isatty() and not scripted:
            print("harnessgram setup asks a few questions; run it in a terminal, or use --non-interactive "
                  "with CTB_BOT_TOKEN and --owner-id.", file=sys.stderr)
            return 2
        return setup_mod.run(owner_id=args.owner_id, language=args.language, assume_yes=args.yes,
                             interactive=not args.non_interactive, install_hooks=not args.no_install,
                             service=not args.no_service, start=not args.no_start,
                             test_message=not args.no_test_message, run_doctor=not args.no_doctor,
                             settings_file=Path(args.settings) if args.settings else None)
    if cmd == "statusline":
        from .statusline import run as statusline_run
        print(statusline_run(sys.stdin.read()))
        return 0
    if cmd == "account":
        from . import accounts
        from .inbox import auto_move
        from .telegram import Bot
        try:
            cfg = C.load()
            if not cfg.ready or not (accounts.valid_name(args.target) or args.target == accounts.MAIN):
                return 1
            return 0 if auto_move(cfg, Bot.from_config(cfg), args.session, args.thread, args.target,
                                  idle=getattr(args, "idle", False)) else 1
        except (C.ConfigError, OSError):
            return 1
    if cmd == "take":
        from .inbox import default_inbox
        print(json.dumps(default_inbox().take(args.session), ensure_ascii=False))
        return 0
    if cmd == "autopilot":
        from . import autopilot
        try:
            cfg = C.load()
        except C.ConfigError as e:
            print(f"Harnessgram: {e}", file=sys.stderr)
            return 1
        return autopilot.run_autopilot(cfg, dry_run=args.dry_run)
    if cmd == "topic-rename":
        from .topics import topic_rename
        try:
            cfg = C.load()
        except C.ConfigError as e:
            print(f"Harnessgram: {e}", file=sys.stderr)
            return 1
        return topic_rename(cfg, args.name, args.session)
    if cmd == "topic-names":
        from . import topic_names
        from .i18n import t
        try:
            cfg = C.load()
        except C.ConfigError as e:
            print(f"Harnessgram: {e}", file=sys.stderr)
            return 1
        if args.unlock:
            from .topics import TopicMap
            current = (TopicMap(C.state_dir() / "topics.json", None, C.home_chat(cfg), cfg=cfg).all_places()
                       .get(args.unlock) or {}).get("name", "")
            return 0 if topic_names.unlock(args.unlock, current) else 1
        limit = args.limit or (10 ** 6 if args.dry_run else topic_names.MAX_PER_ROUND)
        found = topic_names.run(cfg, dry_run=args.dry_run, limit=limit)
        lang = getattr(cfg, "ui_language", "") or cfg.target_language
        for item in found:
            print(t("topic_names_would" if args.dry_run else "topic_names_done", lang=lang, kind=item["kind"]),
                  item["session"][:8])
            print("  " + item["old"])
            print("  -> " + item["new"])
        if not found:
            print(t("topic_names_none", lang=lang))
        return 0
    if cmd == "compact":
        from . import compact
        session = args.session or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
        if not session:
            print("Harnessgram: no session (run this inside a Claude Code session, or pass --session)", file=sys.stderr)
            return 1
        if args.cancel:
            started = compact.claimed(session)
            gone = compact.cancel_ask(session)
            print("The compaction already started; nothing more is waiting." if started and gone
                  else "The compaction request was taken back." if gone else "No request was waiting.")
            return 0
        compact.ask(session, args.note)
        print("Asked. The bridge compacts this session when this turn ends and the session is idle"
              + (", keeping: " + compact.clean_note(args.note) if compact.clean_note(args.note) else "")
              + ". Finish the turn normally; do not start new work that needs the old context.")
        return 0
    if cmd == "board":
        from .board import run as board_run
        try:
            cfg = C.load()
        except C.ConfigError as e:
            print(f"Harnessgram: {e}", file=sys.stderr)
            return 1
        return board_run(cfg, args.board_cmd, session=args.session, text=getattr(args, "text", ""),
                         url=getattr(args, "url", ""), due=getattr(args, "due", ""), n=getattr(args, "n", 0))
    if cmd == "bank":
        from .bank import run as bank_run
        return bank_run(args.bank_cmd or "list", name=getattr(args, "provider", ""), n=getattr(args, "n", 0),
                        expires=getattr(args, "expires", ""))
    if cmd == "card":
        from .cards import run as card_run
        try:
            cfg = C.load()
        except C.ConfigError as e:
            print(f"Harnessgram: {e}", file=sys.stderr)
            return 1
        kind = args.card_cmd
        fields = {"progress": {"steps": getattr(args, "step", None), "at": getattr(args, "at", None),
                               "percent": getattr(args, "percent", None)},
                  "choice": {"options": getattr(args, "option", None)},
                  "link": {"url": getattr(args, "url", ""), "label": getattr(args, "label", "")},
                  "numbers": {"figures": getattr(args, "figure", None)}}.get(kind, {})
        return card_run(cfg, kind, title=getattr(args, "title", ""), detail=getattr(args, "detail", ""),
                        ident=getattr(args, "id", ""), **fields)
    if cmd == "block":
        from .blocks import run as block_run
        try:
            cfg = C.load()
        except C.ConfigError as e:
            print(f"Harnessgram: {e}", file=sys.stderr)
            return 1
        return block_run(cfg, args.block_cmd, source=getattr(args, "source", "-"), ident=getattr(args, "id", ""))
    if cmd == "decide":
        from .decisions import run as decide_run
        try:
            cfg = C.load()
        except C.ConfigError as e:
            print(f"Harnessgram: {e}", file=sys.stderr)
            return 1
        return decide_run(cfg, args.decide_cmd, title=getattr(args, "title", ""),
                          detail=getattr(args, "detail", ""), yes=getattr(args, "yes", ""),
                          no=getattr(args, "no", ""), ident=getattr(args, "id", ""))
    return 2


if __name__ == "__main__":
    sys.exit(main())
