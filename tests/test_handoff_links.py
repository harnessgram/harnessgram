"""One-time secure-handoff form links reach the owner whole, never an engine, the cache or a shared copy."""
from types import SimpleNamespace

from ctb import creds, translate

TOKEN = "0123456789abcdef" * 3
LINK = f"https://box.tailabc123.ts.net/__secure-handoff/{TOKEN}"
CFG = SimpleNamespace(target_language="en")


def test_owner_chat_keeps_the_link_but_not_secrets():
    out = creds.redact(f"Open {LINK} now. key sk-ant-api03-{'a' * 40}", keep_handoff=True)
    assert LINK in out and "sk-ant" not in out


def test_shared_copy_hides_it():
    assert TOKEN not in creds.redact(f"Open {LINK}")


def test_smuggled_secret_in_a_handoff_shaped_link_is_redacted():
    for bad in (f"https://x.tailabc.ts.net/__secure-handoff/ghp_{'A1' * 18}",
                f"https://x.tailabc.ts.net/__secure-handoff/sk-ant-api03-{'b' * 40}"):
        out = creds.redact(bad, keep_handoff=True)
        assert "ghp_A1A1" not in out and "sk-ant-api03" not in out


def test_translation_restores_only_when_asked(monkeypatch):
    seen = []
    monkeypatch.setattr(translate, "_one_held", lambda text, cfg, fast: seen.append(text) or text)
    out = translate._one(f"Open {LINK}", CFG, True, keep_links=True)
    assert TOKEN not in seen[-1] and LINK in out
    out = translate._one(f"Open {LINK}", CFG, True)  # default callers: the old behaviour
    assert seen[-1] == f"Open {LINK}" and out == f"Open {LINK}"


def test_reply_path_keeps_links_only_for_unshared(monkeypatch):
    from ctb import mirror
    calls = []
    monkeypatch.setattr(mirror, "translate_text", lambda t, cfg, fast=False, keep_links=False: calls.append(keep_links) or t)
    class M:
        dropped = 0
        def send_reply(self, title, session, text, fn, footer, answer_to=None):
            fn(text)
    monkeypatch.setattr(mirror, "build", lambda cfg: M())
    monkeypatch.setattr(mirror.usage, "compact_footer", lambda cfg, tr: "")
    for shared in (False, True):
        cfg = SimpleNamespace(team=SimpleNamespace(shares=lambda s, shared=shared: shared))
        mirror.run_reply(cfg, "t", "s", "hi", True)
    assert calls == [True, True]  # the team copy drops the link in _emit, not the translation


OAUTH = ("https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e"
         "&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback&scope=user%3Aprofile"
         "&code_challenge=" + "C" * 43 + "&code_challenge_method=S256"
         "&state=" + "S" * 43 + "")


def test_claude_sign_in_links_stay_whole_for_the_owner_and_are_redacted_otherwise():
    for host in ("claude.com/cai", "claude.ai", "platform.claude.com", "platform.claude.com/cai"):
        link = OAUTH.replace("claude.com/cai", host)
        assert creds.redact(f"Open {link}.", keep_handoff=True) == f"Open {link}."
        assert "[redacted]" in creds.redact(f"Open {link}.")      # the default (team) path cuts its query


def test_only_exact_claude_authorize_links_are_held_never_other_urls():
    for other in ("https://claude.com.evil.io/cai/oauth/authorize?state=" + "a" * 40,
                  "https://evil.example/oauth/authorize?state=" + "a" * 40,
                  "https://claude.ai/login?state=" + "a" * 40,
                  "http://claude.ai/oauth/authorize?state=" + "a" * 40,
                  "https://claude.ai/oauth/authorize"):
        assert creds.hold_links(f"see {other}")[1] == [], other


def test_held_links_round_trip_and_hide_cleanly():
    text, held = creds.hold_links(f"A {OAUTH}, and {LINK}.")
    assert len(held) == 2 and OAUTH not in text and TOKEN not in text
    assert creds.restore_links(text, held) == f"A {OAUTH}, and {LINK}."
    assert "&amp;" in creds.restore_links(text, held, escape=True)
    hidden = creds.hide_links(text, held)
    assert hidden == "A [redacted], and [redacted]." and "hgl" not in hidden.lower()


def test_leftovers_of_an_altered_placeholder_become_redacted_but_exact_ones_stay():
    text, held = creds.hold_links(f"Open {LINK} now")
    ph = held[0][0]
    assert creds.scrub_placeholders(f"x {ph}.", held) == f"x {ph}."
    for bad in (ph.replace("HGL", "Hgl"), ph.replace("N0X", "N 0 X"), ph[:-3], "HGL0abc12N0X", ph.upper()):
        out = creds.scrub_placeholders(f"x {bad} y", held)
        assert "hgl" not in out.lower() and "[redacted]" in out, bad
    assert creds.scrub_placeholders("HGLOBAL plans", []) == "HGLOBAL plans"   # nothing held: untouched


def test_a_split_never_cuts_through_a_placeholder():
    from ctb import fmt
    text, held = creds.hold_links(LINK)
    ph = held[0][0]
    for pad in range(0, 60):
        line = "z" * pad + " " + ph + " tail"
        for limit in range(len(ph) + 1, pad + len(ph) + 3):
            pieces = fmt._split_text(line, limit) if len(line) > limit else [line]
            assert "".join(pieces) == line and sum(ph in p for p in pieces) == 1, (pad, limit)


def test_sign_in_link_is_swapped_for_a_note_and_returned_whole():
    out, links = creds.extract_signin(f"Open {OAUTH}. Again {OAUTH}")
    assert out == "Open (sign-in link sent separately). Again (sign-in link sent separately)"
    assert links == [OAUTH]
    assert creds.extract_signin(f"Open {LINK}") == (f"Open {LINK}", [])   # a handoff link is not a sign-in link


def test_a_sign_in_link_inside_another_links_query_is_not_held():
    nested = "https://example.com/redirect?u=" + OAUTH
    assert creds.hold_links(nested)[1] == []
    assert creds.hold_links("https://example.com/?a=1&u=" + OAUTH)[1] == []
    assert creds.hold_links(f"Open {OAUTH}")[1] and creds.hold_links(f"({OAUTH}).")[1]


def test_a_random_token_after_the_link_is_not_swept_into_it():
    token = "aB3dE5fG7h" * 4
    link = "https://claude.ai/oauth/authorize?state=x"
    for text in (f"{link},{token}", f"see {link}, then {token}"):
        _, held = creds.hold_links(text)
        assert [l for _, l in held] == [link]
    out = creds.redact(f"{link},{token}", keep_handoff=True)
    assert out == f"{link},[redacted]"
    assert creds.hold_links(f"{OAUTH}&{token}")[1] != [] and creds.hold_links(f"{link}&bad")[1] == []


def test_hide_links_clears_leftover_placeholder_pieces():
    text, held = creds.hold_links(f"Open {LINK} now")
    ph = held[0][0]
    out = creds.hide_links(f"a {ph} b {ph.replace('HGL', 'Hgl')} c https://hgl.invalid/zzz d", held)
    assert out.count("[redacted]") == 3 and "hgl" not in out.lower()


def test_only_a_link_of_real_shape_is_returned_but_every_link_is_hidden():
    short = OAUTH.replace("S" * 43, "short").replace("C" * 43, "short")
    no_state = OAUTH.replace("&state=" + "S" * 43, "")
    no_challenge = OAUTH.replace("&code_challenge=" + "C" * 43, "")
    bad_method = OAUTH.replace("S256", "plain")
    for sample in (short, no_state, no_challenge, bad_method):
        out, found = creds.extract_signin(f"Open {sample}.")
        assert found == [] and out == "Open (sign-in link sent separately)."
    assert creds.extract_signin(f"Open {OAUTH}.")[1] == [OAUTH]
    assert creds.extract_signin(f"Open {OAUTH.replace('&', '&amp;')}.")[1] == [OAUTH.replace("&", "&amp;")]


def test_youtube_phone_sign_in_link_reaches_the_owner_whole():
    link = f"https://box.tailabc123.ts.net/__yt-auth/{TOKEN}/"
    assert link in creds.redact(f"Open {link} on the phone", keep_handoff=True)
    assert TOKEN not in creds.redact(f"Open {link}")
