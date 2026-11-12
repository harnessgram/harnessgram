"""Odd spellings around a Claude sign-in link (OAUTH_BROAD): never shown to the team or on the card."""
import html
import json

import pytest

from conftest import GOOD_TOKEN, FakeBot
from ctb import creds, hooks, i18n, progress as P
from ctb.config import Config, state_dir
from ctb.topics import PLACEHOLDER, TopicMap

import test_team as TT

LINK = ("https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e"
        "&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback&login_hint=owner%40example.com"
        "&scope=user%3Aprofile&code_challenge=" + "C" * 43 + "&code_challenge_method=S256"
        "&state=" + "S" * 43 + "")
FORMS = {
    "colon": "Visit:{l} to sign in",
    "param": "login url={l} now",
    "italic": "Open _{l}_ please",
    "jsonnl": "Output:\\\\n{l}\\\\nPaste code here",
    "bold": "Open **{l}** please",
}
SECRETS = ("client_id", "redirect_uri", "login_hint", "oauth/authorize", "code_challenge", "owner%40")
OWNER = 7


@pytest.fixture(autouse=True)
def english(monkeypatch):
    monkeypatch.setattr(i18n, "_current_lang", "en")


@pytest.mark.parametrize("form", FORMS)
def test_team_never_sees_any_part_of_the_link(tmp_path, form):
    m, bot = TT.mirror_rig(tmp_path)
    m.send_reply("Shared one", "shared", FORMS[form].format(l=LINK), None)
    team = "".join(TT.by_chat(bot, TT.GROUP))
    assert team and "[redacted]" in team
    assert not any(x in team for x in SECRETS), team


@pytest.mark.parametrize("form", FORMS)
def test_team_never_sees_it_after_translation_either(tmp_path, form):
    m, bot = TT.mirror_rig(tmp_path)
    m.cfg.target_language = "he"
    seen = []
    m.send_reply("Shared one", "shared", FORMS[form].format(l=LINK),
                 lambda t: seen.append(t) or "\u05e9\u05dc\u05d5\u05dd " + t)
    team = "".join(TT.by_chat(bot, TT.GROUP))
    assert not any(x in team for x in SECRETS), team
    assert not any(x in "".join(seen) for x in ("client_id", "login_hint", "redirect_uri"))  # nor an engine


@pytest.mark.parametrize("form", FORMS)
def test_progress_card_hides_every_form_and_the_owner_gets_the_link(monkeypatch, form):
    monkeypatch.setattr(hooks, "spawn", lambda *a, **k: None)
    c = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="en", ui_language="en", progress=True)
    data = {"tool_name": "Bash", "tool_input": {"command": FORMS[form].format(l=LINK), "description": "login"},
            "tool_response": {"stdout": FORMS[form].format(l=LINK), "stderr": ""}, "transcript_path": ""}
    bot = FakeBot()
    TopicMap(state_dir() / "topics.json", bot, OWNER).ensure("s", "T")
    P.append_progress("s", data, c)
    rows = [json.loads(x) for x in (state_dir() / "progress" / "s.jsonl").read_text().splitlines()]
    for r in rows:
        if r["kind"] != "link":
            blob = json.dumps(r)
            assert not any(x in blob for x in SECRETS), r
    assert [r["url"] for r in rows if r["kind"] == "link"] == [LINK]          # trimmed to the URL, once
    bot.calls.clear()
    P.run_send_progress(c, "s", bot=bot, throttle=0, now_fn=lambda: 1000.0, fast=True)
    sent = [p["text"] for m, p in bot.calls if m in ("sendMessage", "editMessageText") and p["text"] != PLACEHOLDER]
    assert sum(html.escape(LINK) in t for t in sent) == 1
    assert all(not any(x in t for x in SECRETS) for t in sent if html.escape(LINK) not in t)


def test_strict_and_broad_agree_on_what_the_owner_gets_whole():
    assert creds.extract_signin(f"Open {LINK}.")[1] == [LINK]
    nested = f"https://example.com/r?u={LINK}"
    out, found = creds.extract_signin(nested)
    assert "client_id" not in out and found                                  # hidden, and still sent to the owner
    assert creds.hold_links(nested)[1] == []                                 # but not held whole in a reply


def test_held_value_stops_at_hash_at_sign_and_bold():
    base = "https://claude.ai/oauth/authorize?state=x"
    for tail in ("#frag", "@host", "**", "**."):
        _, held = creds.hold_links(f"see {base}{tail}")
        assert [l for _, l in held] == [base], tail
