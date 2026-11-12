"""Every prompt to an outside model goes through one sanitizer (creds.for_outside): credentials redacted and the
owner-only links (Claude sign-in, one-time secure handoff) removed, in every field - names and project metadata
included. The sample links and tokens are built from pieces: none is real."""
import urllib.request

import pytest

from conftest import GOOD_TOKEN
from ctb import config as C
from ctb import creds, topic_names, translate

OAUTH = ("https://claude.ai/oauth/authorize?" + "code=true&client_id=" + "9d1c250a-e61b-44d9-88ed-" + "5944d1962f5e"
         + "&response_type=code&redirect_uri=" + "https%3A%2F%2Fconsole.example%2Foauth%2Fcode%2Fcallback"
         + "&scope=user%3Aprofile&login_hint=" + "owner" + "%40example.com"
         + "&code_challenge=" + "Ab1" * 15 + "&code_challenge_method=S256&state=" + "Zx9" * 15)
HANDOFF = "https://box.tailabc123.ts.net/__secure-handoff/" + "ab" * 24
FAKE_KEY = "sk-" + "ant-" + "x" * 30
LEAKS = ("login_hint", "client_id", "redirect_uri", "example.com", "ts.net", "secure-handoff", "x" * 30, "Zx9Zx9")


def leaked(text: str) -> list:
    return [mark for mark in LEAKS if mark in text]


def make_cfg(**extra):
    raw = {"bot_token": GOOD_TOKEN, "owner_chat_id": 7, "target_language": "he", "translate": True,
           "engines": ["gemini"], "gemini_key_command": ["echo", "fake-credential"]}
    raw.update(extra)
    return C.from_dict(raw)


@pytest.mark.parametrize("text", [OAUTH, HANDOFF, "Visit:" + OAUTH, "url=" + HANDOFF, FAKE_KEY,
                                  f"see {OAUTH} and {HANDOFF} with {FAKE_KEY}"])
def test_the_sanitizer_removes_owner_links_and_credentials(text):
    assert leaked(creds.for_outside(text)) == []
    assert leaked(creds.for_outside(text, redact_credentials=False)) == [] or FAKE_KEY in text


def test_topic_naming_sends_no_owner_link_in_any_field():
    cfg = make_cfg(topic_project_about={"Bridge": "the tool, form at " + HANDOFF + " key " + FAKE_KEY},
                   topic_project_names={"bridge": "Bridge"})
    rules, data = topic_names.prompt_parts(cfg, current="Bridge: sign in " + OAUTH, project="",
                                           folder="repo " + HANDOFF, owner=["please open " + OAUTH],
                                           replies=["the form is at " + HANDOFF + " key " + FAKE_KEY],
                                           first="sign in at " + OAUTH)
    assert leaked(rules) == [] and leaked(data) == []
    project_rules, _ = topic_names.prompt_parts(cfg, "", "Bridge " + OAUTH, "", ["x y z w"], [], "")
    assert leaked(project_rules) == []


@pytest.fixture
def requests(monkeypatch):
    sent = []

    def urlopen(req, *a, **k):
        sent.append(req.data.decode("utf-8") if getattr(req, "data", None) else req.full_url)
        raise OSError("network is off in tests")
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(translate, "_credential", lambda command, header, timeout: "fake-credential")
    return sent


@pytest.mark.parametrize("call", [
    lambda cfg: translate.ask("name this topic: " + OAUTH + " " + FAKE_KEY, cfg, system="project note " + HANDOFF),
    lambda cfg: translate.gemini("Open " + OAUTH + " then " + HANDOFF + " " + FAKE_KEY, cfg),
    lambda cfg: translate.speak("שלום " + HANDOFF + " " + FAKE_KEY, cfg),
    lambda cfg: translate.google("Open " + OAUTH + " then " + HANDOFF, "he"),
], ids=["ask", "gemini", "speak", "google"])
def test_each_outside_request_is_sanitized_at_the_wire(requests, call):
    """The last line of defence, per request: whatever a caller passes (prompt, system instruction), the body
    sent carries no owner-only link and no credential."""
    try:
        call(make_cfg())
    except translate.TranslateError:
        pass  # Google answers nothing in tests; what was sent is what counts
    assert requests, "no request was made"
    assert all(leaked(body) == [] for body in requests), requests


def test_the_radio_voice_request_is_sanitized_text_and_style(monkeypatch):
    from ctb import radio_voice
    bodies = []

    def urlopen(req, *a, **k):
        bodies.append(req.data.decode("utf-8"))
        raise OSError("network is off in tests")
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(radio_voice, "access_token", lambda cfg, why=None: "fake-credential")
    cfg = make_cfg(gemini_vertex_project="example-project")
    radio_voice.vertex("hello " + HANDOFF, cfg, "gemini-3-tts", "Kore", "calm, as at " + OAUTH)
    assert bodies and all(leaked(b) == [] for b in bodies)


def test_the_radio_fallback_line_never_carries_the_raw_reply():
    """With no editor and no Gemini answer, the spoken fallback is built from the cleaned reply."""
    from ctb import radio_talk
    token = "1234567" + "890:" + "A" * 35
    line = radio_talk.brief_line(make_cfg(), "Work", "Done. The bot token is " + token + " and " + HANDOFF + ".",
                                 ask=lambda prompt: "", translate=lambda t: t)
    assert token not in line and "ts.net" not in line


def test_with_redaction_turned_off_owner_links_still_never_leave(requests):
    cfg = make_cfg(redact_outgoing=False)
    translate.ask("name this topic: " + OAUTH + " " + HANDOFF, cfg)
    assert requests and not any(mark in body for body in requests
                                for mark in ("login_hint", "ts.net", "secure-handoff"))


BOT_TOKEN = "1234567" + "890:" + "A" * 35


@pytest.mark.parametrize("text", [
    OAUTH.replace("https://claude.ai", "HTTPS://CLAUDE.AI"),  # upper-case scheme and host
    HANDOFF.replace("https", "HTTPS").replace(".tailabc123.", ".TAILABC123."),
    OAUTH.replace("/", "\\/"),  # JSON-escaped slashes
    HANDOFF.replace("/", "\\/"),
    "".join("%%%02X" % ord(c) if c in ":/?&=" else c for c in OAUTH),  # %XX in the link
    "".join("%%%02X" % ord(c) if c in ":/" else c for c in HANDOFF),
    "https%253A%252F%252Fbox.tailabc123.ts.net%252F__secure-handoff%252F" + "ab" * 24,  # double-encoded
    "".join("%%%02X" % ord(c) for c in BOT_TOKEN),  # a bot token with every character encoded
    "https://box.tailabc123.ts.net/__secure-handoff/[redacted]",  # what an earlier redaction left of a link
    "HTTPS://Box.Tailabc123.TS.NET/__YT-AUTH/" + "cd" * 24,
    "https:" + "//person" + chr(64) + "box.tailabc123.ts.net/__secure-handoff/" + "ab" * 24,  # a login part first
    "https://box.tailabc123.ts.net/x/../__secure-handoff/" + "ab" * 24,  # a dot segment in the path
    "https:" + "//user" + chr(64) + "claude.ai/a/../oauth/authorize?login_hint=x",
    "".join("\\u%04x" % ord(c) for c in BOT_TOKEN),  # \\uXXXX
    "".join("&#%d;" % ord(c) for c in BOT_TOKEN),  # numeric HTML entities
    "".join("&#x%x;" % ord(c) for c in BOT_TOKEN),
    __import__("urllib.parse").parse.quote(__import__("urllib.parse").parse.quote(
        __import__("urllib.parse").parse.quote(BOT_TOKEN, safe=""), safe=""), safe=""),  # three layers
], ids=["oauth-case", "handoff-case", "oauth-escaped", "handoff-escaped", "oauth-percent", "handoff-percent",
        "handoff-double", "token-percent", "handoff-remains", "yt-auth-case", "handoff-userinfo", "handoff-dots",
        "oauth-userinfo-dots", "token-unicode", "token-entities", "token-hex-entities", "token-three-layers"])
def test_every_way_of_writing_a_link_or_token_is_caught(text):
    link = "/" in creds.normalized(text)
    # a link: every filter that must never show it (outside models, the team copy, the progress card)
    # a token: every redaction, the Telegram and panel ones included
    cleaners = (creds.for_outside, creds.hide_owner_links) if link else (creds.for_outside, creds.redact,
                                                                         creds.redact_channel)
    for clean in cleaners:
        out = clean("see " + text + " now")
        assert BOT_TOKEN not in creds.normalized(out), (clean.__name__, out)
        assert not any(mark in out.lower() for mark in ("login_hint", "client_id", "ts.net", "secure-handoff",
                                                        "yt-auth", "tailabc")), (clean.__name__, out)


def test_a_text_still_encoded_after_the_decoding_limit_is_refused():
    deep = "x"
    for _ in range(creds.DECODE_ROUNDS + 2):
        deep = __import__("urllib.parse").parse.quote(deep + "/", safe="")
    assert creds.for_outside("see " + deep) == "[redacted]" and creds.redact("see " + deep) == "[redacted]"


def test_ordinary_encoded_text_is_left_as_written():
    text = "open https://example.com/a%20b?q=1 and 100% done"
    assert creds.redact(text) == text and creds.hide_owner_links(text) == text


def test_owner_links_go_before_redaction_everywhere():
    """A redaction that ran first (it cuts the handoff token) no longer hides the rest of the link."""
    already = creds.redact("form at " + HANDOFF)
    assert "ts.net" in already  # redaction alone keeps the host
    assert "ts.net" not in creds.hide_owner_links(already) and "ts.net" not in creds.for_outside(already)


def test_a_translation_is_cached_under_its_sanitized_text(monkeypatch):
    """The disk cache key is the sanitized text, so the cache never holds an owner-only link either, and the
    control panel's cache lookup finds what the translation stored."""
    cfg = make_cfg(engines=["gemini"])
    monkeypatch.setattr(translate, "gemini", lambda text, cfg: "פתח " + text)
    out = translate.translate_text("Open " + OAUTH, cfg)
    assert "login_hint" not in out and translate.cached("Open " + OAUTH, cfg) is not None


@pytest.mark.parametrize("text,secret", [
    ("\\u0070assword\\u003A hunter2xyz", "hunter2xyz"),  # a labelled short secret written in \\uXXXX
    ("%6ftp: 482913", "482913"),  # %XX
    ("&#112;in: 4821", "4821"),  # a numeric entity
], ids=["unicode-label", "percent-label", "entity-label"])
def test_an_encoded_short_labelled_secret_is_caught(text, secret):
    from ctb import board
    for clean in (creds.redact_channel, lambda t: board.clean_text(t, 200)):
        assert secret not in clean("see " + text), clean


@pytest.mark.parametrize("space", ["%20", "\\u0020", "&#32;"])
def test_an_encoded_space_does_not_hide_a_bearer_token(space):
    value = "abcdefghijklmnopqrstuvwx"
    text = "Authorization: Bearer" + space + value
    for clean in (creds.for_outside, creds.redact_channel, creds.redact):
        assert value not in creds.normalized(clean(text)), clean


def _growth(clean, unit: str, n: int = 4000) -> float:
    """How much longer clean() takes when its input doubles (best of three each): about 2 when linear, about 4
    when quadratic. A ratio, not a wall-clock limit, so a slow machine does not change the verdict."""
    import time as clock

    def best(text):
        times = []
        for _ in range(3):
            start = clock.perf_counter()
            clean(text)
            times.append(clock.perf_counter() - start)
        return max(min(times), 1e-4)
    return best(unit * (2 * n)) / best(unit * n)


CLEANERS = [creds.for_outside, creds.redact, creds.redact_channel, creds.hide_owner_links]


@pytest.mark.parametrize("unit", ["\\", "a\\", "https://claude.ai/", "https://x.tailabc1.ts.net/", "https://b.tailabc1.ts.net/x/",
                                  "http://claude.ai/oauth/", "%25"],
                         ids=["backslashes", "backslash-pairs", "claude-links", "tailnet-links", "tailnet-paths",
                              "oauth-prefixes", "percent"])
@pytest.mark.parametrize("clean", CLEANERS, ids=[c.__name__ for c in CLEANERS])
def test_cleaning_time_grows_linearly(clean, unit):
    assert _growth(clean, unit) < 3.0


@pytest.mark.parametrize("text", ["password: hunter2", "%6ftp: 482913", "DB_PASSWORD=!Abcd1234",
                                  "pw: $ecret99", "password: /Abcd1234"])
def test_an_outside_model_gets_the_same_label_checks_as_the_panel(text):
    secret = creds.normalized(text).split(":")[-1].split("=")[-1].strip()
    for clean in (creds.for_outside, creds.redact_channel):
        assert secret not in creds.normalized(clean(text)), (clean.__name__, clean(text))


@pytest.mark.parametrize("text", ["password: ***", "pw: <your password>", "api_key: $KEY",
                                  "password_file: ./secrets/db.txt", "cert_password_path: /etc/ssl/key.pem"])
def test_real_placeholders_and_paths_stay_readable(text):
    assert creds.redact_channel(text) == text


def test_an_encoded_link_keeps_its_encoding_when_nothing_is_hidden():
    url = "https://example.com/a%20b?q=a%26b"
    for clean in (creds.for_outside, creds.redact, creds.redact_channel, creds.hide_owner_links):
        assert clean("see " + url) == "see " + url, clean.__name__


@pytest.mark.parametrize("prefix", ["İ:", "İİİİ ", "ẞß:", "é́:", "ﬃ:", "Σ:"],
                         ids=["dotted-I", "dotted-I-run", "sharp-s", "combining-marks", "ligature", "sigma"])
def test_characters_that_change_length_when_lowered_do_not_shift_the_link_search(prefix):
    """str.lower() makes "İ" two characters: offsets from a lowered copy would miss the link in the original."""
    for link in ("https://host.tailabc1.ts.net/__secure-handoff/" + "a" * 48, "https://claude.ai/oauth/authorize?state=x"):
        text = prefix + link
        for out in (creds.hide_owner_links(text), creds.for_outside(text, redact_credentials=False),
                    creds.for_outside(text)):
            assert "ts.net" not in out and "oauth/authorize" not in out, out
            assert out.startswith(prefix.rstrip(":").rstrip()[:1])


def test_the_exact_reported_string_is_hidden():
    text = "İ:https://host.tailabc1.ts.net/__secure-handoff/" + "a" * 48
    assert creds.hide_owner_links(text) == "İ:[redacted]"
    assert creds.for_outside(text, redact_credentials=False) == "İ:[redacted]"


def test_the_radio_phrasing_prompt_and_channel_report_are_cleaned():
    from ctb import radio, radio_post
    prompts = []
    radio.phrase_one({"project": "Bridge " + HANDOFF, "text": "rotated " + FAKE_KEY + " at " + OAUTH},
                     lambda prompt: prompts.append(prompt) or "")
    assert prompts and leaked(prompts[0]) == []
    body = radio_post.render({"kind": "bulletin", "lines": [("Work", "rotated " + FAKE_KEY + " see " + HANDOFF)]},
                             "en", "10:00")
    assert body and leaked(body) == []
