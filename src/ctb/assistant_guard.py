"""The PreToolUse guard of an assistant session (assistant.py): defense in depth inside the OS sandbox.

Run by the hook as `<python> -I <this file> --ctb=<json argv> [extra tool ...]` with the hook's JSON on stdin: exit 0
allows the tool call (or, with a JSON "deny" on stdout, refuses it with a message), any other exit (a refusal here, a
crash, a missing interpreter) makes the hook line exit 2, which refuses it. Standard library only, no package imports,
so it runs the same with -I from any folder.

The boundary is the operating system's: Claude Code runs every Bash command of an assistant session inside its
sandbox (Seatbelt on macOS) with no network, no unix sockets, writes only in the session's private temporary folder,
and no reading of the bridge's config and state, Claude's own folders or the Keychain (assistant.launch_flags). In
dontAsk mode with no user settings loaded, only the exact tools of assistant.settings_dict() are allowed at all. This
guard repeats that allow-list (a second, independent check) and does the one thing the sandbox cannot:

- built-in tools: only BUILTIN (local reads, planning, questions to the owner). No tool that writes a file;
- Bash: any command, because it runs inside the sandbox; except one `ctb card ...` line (or `harnessgram card`),
  which needs the bridge and so cannot work in there. The guard runs that card command itself, outside the sandbox,
  as an argv list with no shell at all, and refuses the tool call with ctb's answer as the message (the session
  reads it). It accepts only a strict line: words separated by single spaces, each a plain word of SAFE characters
  or one single-quoted string with no quote or newline inside, and options only from CARD_OPTIONS spelled in full
  (no abbreviation such as `--ima`, no `--image`, no `--visibility`: an assistant card is private, see cards.run);
- an MCP tool only by its EXACT full name in MCP_TOOLS (the owner's mail, calendar and files reads, the WhatsApp
  hub's reads, and the draft steps), or one the owner added in the config (`assistant.tools`, passed here as
  arguments). A generic fetch or browse tool that could carry data to any address is never on the list, whatever
  it is called. MCP tools run outside the sandbox, so this list and the permission rules are what hold them.
Everything else, including any tool added later, is refused.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from typing import FrozenSet, List, Optional

TOOLS = ("Read", "Glob", "Grep", "Bash", "ToolSearch", "TodoWrite", "AskUserQuestion")  # the session's built-ins
BUILTIN = frozenset(TOOLS) - {"Bash"}
_GMAIL, _CAL, _DRIVE, _WA = ("mcp__claude_ai_Gmail__", "mcp__claude_ai_Google_Calendar__",
                             "mcp__claude_ai_Google_Drive__", "mcp__whatsapp__")
MCP_TOOLS = frozenset(
    [_GMAIL + t for t in ("search_threads", "get_thread", "get_message", "list_labels", "list_drafts", "get_draft",
                          "create_draft", "update_draft")]
    + [_CAL + t for t in ("list_calendars", "list_events", "get_event", "search_events", "suggest_time")]
    + [_DRIVE + t for t in ("search_files", "get_file_metadata", "read_file_content", "list_recent_files")]
    + [_WA + t for t in ("wa_status", "wa_list_chats", "wa_read_chat", "wa_search_messages", "wa_unread_digest",
                         "wa_prepare", "wa_cancel")])
# Words that never belong in a tool the owner adds (assistant.valid_section refuses them in the config).
WRITE_WORDS = frozenset({
    "send", "execute", "exec", "run", "post", "publish", "unpublish", "delete", "remove", "trash", "reply",
    "forward", "upload", "share", "create", "update", "set", "add", "apply", "deploy", "invite", "respond",
    "approve", "reject", "merge", "rename", "patch", "put", "insert", "grant", "revoke", "buy", "transfer",
    "archive", "label", "mark", "pin", "subscribe", "rate", "move", "submit", "schedule", "queue", "moderate",
    "edit", "write", "commit", "call", "spawn", "stop", "cancel", "pause", "restore", "reset", "rebase", "boost",
    "reorder", "enable", "disable", "connect", "notify", "broadcast", "refund", "fetch", "browse", "navigate",
    "download", "request", "http", "url", "web", "open", "visit", "crawl", "scrape",
})
TOOL_RE = re.compile(r"mcp__[A-Za-z0-9_-]+__[A-Za-z0-9_-]+")
PROGRAMS = frozenset({"ctb", "harnessgram"})
WORD = re.compile(r"[A-Za-z0-9_.:/@%+,-]+|'[^'\n\r\x00]*'")
CARD_LIKE = re.compile(r"\s*(?:\S*/)?(?:ctb|harnessgram)\s+card\b")
# The card options an assistant may use, spelled in full (cli.py). Not --image (a local file on the card) and not
# --visibility (an assistant card is always private).
CARD_OPTIONS = frozenset({"--detail", "--id", "--step", "--at", "--percent", "--option", "--label", "--figure"})
SESSION_RE = re.compile(r"[0-9a-fA-F][0-9a-fA-F-]{7,35}")
CARD_TIMEOUT = 30


class Refused(Exception):
    pass


def words(name: str):
    return [w for w in re.split(r"[^a-z0-9]+", name.lower()) if w]


def extra_ok(name) -> bool:
    """An owner-added tool may only be an exact MCP name with no write or network word in its own part."""
    return (isinstance(name, str) and bool(TOOL_RE.fullmatch(name))
            and not any(w in WRITE_WORDS for w in words(name.split("__", 2)[2])))


def _words_of(command: str):
    """The words of a line made only of WORD tokens separated by single spaces, else None. A token is a whole plain
    word or a whole single-quoted string: two adjacent tokens with no space between them are refused."""
    out, at = [], 0
    while True:
        m = WORD.match(command, at)
        if m is None:
            return None
        out.append(m.group(0))
        at = m.end()
        if at == len(command):
            return out
        if command[at] != " ":
            return None
        at += 1


def card_argv(tool_input) -> Optional[List[str]]:
    """The ctb arguments (`card ...`) when the Bash call is a card command; None when it is any other command (it
    runs inside the sandbox). Raises Refused for a line that looks like a card command but is not a strict one."""
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not CARD_LIKE.match(command):
        return None
    found = _words_of(command)
    if found is None:
        raise Refused("a card line with a word that is neither plain (letters, digits and _ . : / @ % + , -) nor one "
                      "single-quoted string, or words not separated by single spaces; write "
                      "`ctb card ask 'Your question?'`")
    if found[0] not in PROGRAMS or len(found) < 3 or found[1] != "card":
        raise Refused("a card line that is not `ctb card <kind> ...`")
    argv = shlex.split(command)  # plain and single-quoted words only: POSIX splitting is exact here
    if len(argv) != len(found):
        raise Refused("a card line it cannot read")
    bad = [a for a in argv[2:] if a.startswith("-") and a not in CARD_OPTIONS]
    if bad:
        raise Refused(f"a card option it does not take ({bad[0][:40]}); options: {' '.join(sorted(CARD_OPTIONS))}, "
                      "spelled in full; no --image and no --visibility (an assistant card is private)")
    return argv[1:]


def decide(data, extra: FrozenSet[str] = frozenset()) -> Optional[str]:
    """None to allow, else why the call is refused. A strict card line is allowed here; main() runs it itself."""
    if not isinstance(data, dict):
        return "an input it cannot read"
    name = data.get("tool_name")
    if not isinstance(name, str) or not name:
        return "a call without a tool name"
    if name.startswith("mcp__"):
        if name in MCP_TOOLS or (name in extra and extra_ok(name)):
            return None
        return "an MCP tool that is not on the assistant's exact list"
    if name == "Bash":
        tool_input = data.get("tool_input")
        if not isinstance(tool_input, dict) or not isinstance(tool_input.get("command"), str) \
                or not tool_input["command"].strip():
            return "an empty shell command"
        try:
            card_argv(tool_input)
        except Refused as exc:
            return str(exc)
        return None  # inside the sandbox
    if name in BUILTIN:
        return None
    return "a tool that is not on the assistant's list (no file writes, no web)"


def run_card(ctb: List[str], argv: List[str], session: str) -> str:
    """Run `ctb card ...` outside the sandbox as an argv list (no shell). ctb itself proves the session by its
    process ancestry (decisions.caller_session) and keeps an assistant's cards private (cards.run)."""
    env = dict(os.environ)
    env["CLAUDE_CODE_SESSION_ID"] = session
    try:
        p = subprocess.run(ctb + argv, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=CARD_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return "HarnessGram: cards are not available"
    out = "\n".join(s.strip() for s in (p.stdout, p.stderr) if s and s.strip())
    return (out or f"ctb exited {p.returncode}")[:2000]


def deny_json(reason: str) -> str:
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                              "permissionDecisionReason": reason}}, ensure_ascii=False)


NOTE = ("HarnessGram assistant session: refused ({why}). This session reads, drafts and publishes cards "
        "(`ctb card ask 'Text'`); a send, post or change outside is prepared as a draft or wa_prepare and put "
        "on a card, and the owner does it himself.")


def _options(argv: List[str]):
    """(--ctb argv, extra tools) from the guard's own arguments; ValueError when they are not exactly right."""
    ctb, extra = None, []
    for a in argv:
        if a.startswith("--ctb="):
            if ctb is not None:
                raise ValueError("two --ctb")
            ctb = json.loads(a[len("--ctb="):])
            if not (isinstance(ctb, list) and ctb and all(isinstance(w, str) and w for w in ctb)
                    and os.path.isabs(ctb[0])):
                raise ValueError("bad --ctb")
        elif extra_ok(a):
            extra.append(a)
        else:
            raise ValueError("bad extra tool")
    return ctb, frozenset(extra)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        ctb, extra = _options(argv)
        data = json.loads(sys.stdin.read())
        why = decide(data, extra)
        card = card_argv(data.get("tool_input")) if why is None and data.get("tool_name") == "Bash" else None
    except Exception:  # noqa: BLE001 - anything unexpected refuses
        why, card = "an input it cannot read", None
    if why is None and card is not None:
        session = data.get("session_id")
        if ctb is None or not isinstance(session, str) or not SESSION_RE.fullmatch(session):
            why = "a card command, but cards are not available here"
        else:
            sys.stdout.write(deny_json("HarnessGram ran this card command for you outside the sandbox (the shell "
                                       "did not run it). ctb answered: " + run_card(ctb, card, session)) + "\n")
            return 0
    if why is None:
        return 0
    sys.stderr.write(NOTE.format(why=why) + "\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
