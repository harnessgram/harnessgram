"""`harnessgram bot-api`: the token for logOut / close is read from the saved config and never an argument."""
import json

import pytest

from ctb import botapi, cli, config as C
from ctb import telegram as T

TOKEN = "123456:" + "A" * 35


class FakeBot:
    made = []

    def __init__(self, token=None, base=None, local=False, files_dir=""):
        self.token, self.base, self.local = token, base, local
        self.calls = []
        FakeBot.made.append(self)

    def call(self, method, params=None, timeout=15):
        self.calls.append((method, params))
        if getattr(FakeBot, "fail", None):
            raise T.TelegramError(FakeBot.fail)
        return True


@pytest.fixture(autouse=True)
def fake(monkeypatch):
    FakeBot.made, FakeBot.fail = [], None
    monkeypatch.setattr(T, "Bot", FakeBot)


def save(**extra):
    path = C.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"bot_token": TOKEN, "owner_chat_id": 7, **extra}), encoding="utf-8")


def test_logout_uses_the_saved_token_against_the_cloud_server(capsys):
    save(api_base="http://127.0.0.1:8081", api_local=True)
    assert cli.main(["bot-api", "logout", "--yes"]) == 0
    bot = FakeBot.made[0]
    assert (bot.token, bot.base, bot.calls) == (TOKEN, None, [("logOut", None)])  # base None = the cloud server
    out = capsys.readouterr()
    assert TOKEN not in out.out + out.err


def test_close_goes_to_the_local_server(capsys):
    save(api_base="http://127.0.0.1:8081", api_local=True)
    assert cli.main(["bot-api", "close", "--yes"]) == 0
    bot = FakeBot.made[0]
    assert (bot.base, bot.local, bot.calls) == ("http://127.0.0.1:8081", True, [("close", None)])


def test_close_needs_a_local_server(capsys):
    save()
    assert cli.main(["bot-api", "close", "--yes"]) == 1 and not FakeBot.made
    assert "api_local" in capsys.readouterr().err


def test_it_asks_first_and_a_no_or_closed_stdin_sends_nothing(capsys):
    save()
    assert botapi.run("logout", ask=lambda _: "n") == 1
    assert botapi.run("logout", ask=lambda _: (_ for _ in ()).throw(EOFError)) == 1
    assert not FakeBot.made or all(not b.calls for b in FakeBot.made)
    assert botapi.run("logout", ask=lambda _: "y") == 0 and FakeBot.made[-1].calls == [("logOut", None)]


def test_a_telegram_error_is_reported_without_the_token(capsys):
    save()
    FakeBot.fail = "Unauthorized"
    assert cli.main(["bot-api", "logout", "--yes"]) == 1
    out = capsys.readouterr()
    assert "Unauthorized" in out.err and TOKEN not in out.out + out.err


def test_no_config_is_a_clean_error(capsys):
    assert cli.main(["bot-api", "logout", "--yes"]) == 1 and not FakeBot.made


def test_the_token_is_not_an_option():
    with pytest.raises(SystemExit):
        cli.main(["bot-api", "logout", "--token", TOKEN])


def test_a_server_description_that_echoes_the_token_is_not_printed(capsys):
    save()
    FakeBot.fail = f"Bad Request: https://example.test/bot{TOKEN}/logOut failed, token {TOKEN} rejected"
    assert cli.main(["bot-api", "logout", "--yes"]) == 1
    out = capsys.readouterr()
    assert TOKEN not in out.out + out.err and "A" * 20 not in out.out + out.err
    assert botapi.HIDDEN in out.err  # the message held the token: only a fixed line is printed


def test_safe_replaces_a_bare_token_and_a_url_with_it():
    other = "987654:" + "b" * 30
    text = botapi.safe(f"see https://h.test/bot{other}/close and {other} and more", TOKEN)
    assert other not in text and "b" * 20 not in text and text.startswith("see ") and text.endswith(" and more")
    assert "<url hidden>" in text and "<hidden>" in text


def test_the_messages_describe_the_real_waiting_rule(capsys):
    save(api_base="http://127.0.0.1:8081", api_local=True)
    assert botapi.run("logout", ask=lambda q: (print(q), "y")[1]) == 0
    text = capsys.readouterr().out
    assert "start your own server right away" in text and "cloud server again for about 10 minutes" in text
    assert botapi.run("close", ask=lambda q: (print(q), "y")[1]) == 0
    assert "another local server" in capsys.readouterr().out


def _spellings(token):
    return {
        "plain": token,
        "colon %3A": token.replace(":", "%3A"),
        "colon %3a": token.replace(":", "%3a"),
        "colon %253A": token.replace(":", "%253A"),
        "colon %25%33%41": token.replace(":", "%25%33%41"),
        "fully encoded": "".join("%%%02X" % ord(c) for c in token),
        "fully encoded, lower": "".join("%%%02x" % ord(c) for c in token),
        "double encoded": "".join(f"%25{ord(c):02X}" for c in token),
        "case changed": token.lower(),
        "url": f"https://h.test/bot{token}/logOut",
        "encoded url": f"https://h.test/bot{token.replace(':', '%3A')}/logOut?x=1",
    }


def test_a_message_that_holds_the_token_at_any_encoding_level_is_replaced_by_a_fixed_line():
    for name, form in _spellings(TOKEN).items():
        out = botapi.safe(f"Bad Gateway: {form} (upstream)", TOKEN)
        assert out == botapi.HIDDEN, name


def test_a_message_encoded_deeper_than_the_cap_is_hidden_as_too_deep():
    def nest(text, times):
        for _ in range(times):
            text = text.replace("%", "%25")
        return text
    assert botapi.safe("x " + nest("%41", botapi.MAX_DECODES + 2), TOKEN) == botapi.TOO_DEEP
    assert botapi.safe("x " + nest(TOKEN.replace(":", "%3A"), botapi.MAX_DECODES + 2), TOKEN) == botapi.TOO_DEEP
    # exactly at the cap it is still judged, and the token is found
    assert botapi.safe("x " + nest(TOKEN.replace(":", "%3A"), botapi.MAX_DECODES - 1), TOKEN) == botapi.HIDDEN


def test_a_long_message_without_the_token_is_cut_not_hidden():
    out = botapi.safe("Bad Request: " + "word " * 2000, TOKEN)
    assert out.startswith("Bad Request: word") and out.endswith(botapi.CUT) and len(out) <= botapi.MAX_TEXT + len(botapi.CUT)


def test_the_token_beyond_the_cut_point_hides_the_message():
    assert botapi.safe("x" * (botapi.MAX_TEXT + 10) + TOKEN, TOKEN) == botapi.HIDDEN


def test_a_clean_short_message_is_unchanged():
    assert botapi.safe("Unauthorized: logOut is not allowed here", TOKEN) == "Unauthorized: logOut is not allowed here"


def test_the_work_grows_linearly_with_a_long_token_and_a_long_input():
    import time

    def cost(n):
        token = "1:" + "A" * n
        text = ("%25" * 5 + "x" * 3000 + token)[:botapi.MAX_TEXT * 4]
        start = time.perf_counter()
        for _ in range(3):
            botapi.safe(text, token)
        return time.perf_counter() - start
    small, large = cost(20_000), cost(160_000)  # eight times the token and the input
    assert large < max(small * 40, 0.5), (small, large)  # linear would be about 8x; quadratic about 64x
    for hostile in ("1" * 200_000, "bot1" * 50_000, "%3a" * 70_000, "1:" + "a" * 200_000):
        botapi.safe(hostile, TOKEN)
