import pytest

from conftest import GOOD_TOKEN
from test_setup_doctor import SetupBot
from ctb import config as C
from ctb import setup as S
from ctb.telegram import TelegramError

OTHER_TOKEN = "987654:" + "B" * 35


def test_setup_network_failure_prints_network_message(bot):
    """Review finding 3: network failure prints a network error message, not 'Telegram rejected the token'."""
    bot.fail["getMe"] = "network error"
    output = []
    res = S.run(
        bot=bot,
        env={"CTB_BOT_TOKEN": GOOD_TOKEN},
        owner_id=42,
        interactive=False,
        install_hooks=False,
        service=False,
        start=False,
        test_message=False,
        run_doctor=False,
        say=output.append,
    )
    assert res == 1
    joined = "\n".join(output)
    assert "Could not reach Telegram (network error). Check the internet connection and try again." in joined
    assert "Telegram rejected the token" not in joined


def test_setup_timeout_prints_network_message(bot):
    """Timeout error is also treated as a reachability failure."""
    bot.fail["getMe"] = "timeout"
    output = []
    res = S.run(
        bot=bot,
        env={"CTB_BOT_TOKEN": GOOD_TOKEN},
        owner_id=42,
        interactive=False,
        install_hooks=False,
        service=False,
        start=False,
        test_message=False,
        run_doctor=False,
        say=output.append,
    )
    assert res == 1
    joined = "\n".join(output)
    assert "Could not reach Telegram (timeout). Check the internet connection and try again." in joined
    assert "Telegram rejected the token" not in joined


def test_setup_token_rejected_message(bot):
    """When Telegram actually rejects the token with an API error description, say so."""
    # FakeBot with a non-network description
    output = []
    class RejectBot:
        def call(self, method, params=None, timeout=15):
            if method == "getMe":
                raise TelegramError("Unauthorized", code=401)
            return {}
    res = S.run(
        bot=RejectBot(),
        env={"CTB_BOT_TOKEN": GOOD_TOKEN},
        owner_id=42,
        interactive=False,
        install_hooks=False,
        service=False,
        start=False,
        test_message=False,
        run_doctor=False,
        say=output.append,
    )
    assert res == 1
    joined = "\n".join(output)
    assert "Telegram rejected the token (Unauthorized)." in joined
    assert "Could not reach Telegram" not in joined


def test_setup_replace_saved_token_with_yes(bot):
    """Review finding 4: when a config has a token and CTB_BOT_TOKEN has a different valid token,
    --yes replaces it and prints 'Replacing the saved bot token.' without leaking the token."""
    bot = SetupBot()
    path = C.save_raw({"bot_token": GOOD_TOKEN, "owner_chat_id": 42})
    assert C.load(path).bot_token == GOOD_TOKEN

    output = []
    res = S.run(
        bot=bot,
        env={"CTB_BOT_TOKEN": OTHER_TOKEN},
        owner_id=42,
        assume_yes=True,
        interactive=False,
        install_hooks=False,
        service=False,
        start=False,
        test_message=False,
        run_doctor=False,
        say=output.append,
    )
    assert res == 0
    joined = "\n".join(output)
    assert "Replacing the saved bot token." in joined
    # Security: never print or echo the token in any message
    assert GOOD_TOKEN not in joined
    assert OTHER_TOKEN not in joined
    # Saved config now has the new token
    assert C.load(path).bot_token == OTHER_TOKEN


def test_setup_replace_saved_token_interactive_yes(bot):
    """Interactive setup prompts when CTB_BOT_TOKEN differs, and replaces on yes."""
    bot = SetupBot()
    path = C.save_raw({"bot_token": GOOD_TOKEN, "owner_chat_id": 42})
    output = []
    prompts = []

    def fake_input(prompt):
        prompts.append(prompt)
        return "yes"

    res = S.run(
        bot=bot,
        env={"CTB_BOT_TOKEN": OTHER_TOKEN},
        owner_id=42,
        assume_yes=False,
        interactive=True,
        input_fn=fake_input,
        install_hooks=False,
        service=False,
        start=False,
        test_message=False,
        run_doctor=False,
        say=output.append,
    )
    assert res == 0
    joined = "\n".join(output)
    assert "Replacing the saved bot token." in joined
    assert GOOD_TOKEN not in joined and OTHER_TOKEN not in joined
    assert any("Replace the saved" in p for p in prompts)
    assert C.load(path).bot_token == OTHER_TOKEN


def test_setup_replace_saved_token_interactive_no(bot):
    """Interactive setup prompts when CTB_BOT_TOKEN differs, and keeps existing token on no."""
    bot = SetupBot()
    path = C.save_raw({"bot_token": GOOD_TOKEN, "owner_chat_id": 42})
    output = []
    prompts = []

    def fake_input(prompt):
        prompts.append(prompt)
        return "no"

    res = S.run(
        bot=bot,
        env={"CTB_BOT_TOKEN": OTHER_TOKEN},
        owner_id=42,
        assume_yes=False,
        interactive=True,
        input_fn=fake_input,
        install_hooks=False,
        service=False,
        start=False,
        test_message=False,
        run_doctor=False,
        say=output.append,
    )
    assert res == 0
    joined = "\n".join(output)
    assert "Replacing the saved bot token." not in joined
    assert GOOD_TOKEN not in joined and OTHER_TOKEN not in joined
    # Existing token is kept
    assert C.load(path).bot_token == GOOD_TOKEN


def test_setup_same_token_does_not_prompt_or_replace(bot):
    """When CTB_BOT_TOKEN equals the saved token, do not prompt or announce replacement."""
    bot = SetupBot()
    path = C.save_raw({"bot_token": GOOD_TOKEN, "owner_chat_id": 42})
    output = []
    prompts = []

    res = S.run(
        language="en",
        bot=bot,
        env={"CTB_BOT_TOKEN": GOOD_TOKEN},
        owner_id=42,
        assume_yes=False,
        interactive=True,
        input_fn=prompts.append,
        install_hooks=False,
        service=False,
        start=False,
        test_message=False,
        run_doctor=False,
        say=output.append,
    )
    assert res == 0
    joined = "\n".join(output)
    assert "Replacing the saved bot token." not in joined
    assert not any("Replace the saved" in p for p in prompts)
    assert C.load(path).bot_token == GOOD_TOKEN
