import subprocess

import pytest

from ctb import keychain as K


@pytest.fixture(autouse=True)
def clear_failures():
    K._FAILURES.clear()
    yield
    K._FAILURES.clear()


class Result:
    def __init__(self, code=0, out=""):
        self.returncode, self.stdout = code, out


def test_security_i_quotes_metacharacters_and_rejects_newlines():
    calls = []
    runner = lambda argv, **kw: calls.append((argv, kw)) or Result()
    store = K.Keychain(runner)
    assert store.set(('service"x', r"account\x"), "value", label='label"x')
    command = calls[0][1]["input"]
    assert command.count("\n") == 1
    assert '-s "service\\\"x"' in command
    assert '-a "account\\\\x"' in command
    assert not store.set(("service\nnext", "account"), "value")
    assert not store.set(("service", "account"), "value", label="label\nnext")
    assert len(calls) == 1


@pytest.mark.parametrize("code,status", [(44, "missing"), (0, "error")])
def test_missing_is_distinct_from_an_empty_success(code, status):
    store = K.Keychain(lambda argv, **kw: Result(code, ""))
    assert store.get_status(("service", "account")) == (status, None)


def test_missing_item_is_not_cached_so_a_new_secret_is_seen_at_once():
    now, calls = [100.0], []
    runner = lambda argv, **kw: calls.append(argv) or Result(44, "")
    store = K.Keychain(runner, clock=lambda: now[0])
    assert store.get_status(("service", "account"))[0] == "missing"
    assert store.get(("service", "account")) is None and len(calls) == 2


def test_set_clears_negative_cache():
    item = ("service", "account")
    K._FAILURES[item] = 100
    store = K.Keychain(lambda argv, **kw: Result(), clock=lambda: 101)
    assert store.set(item, "value") and item not in K._FAILURES


def test_get_service_uses_service_only_lookup_and_cache():
    calls = []
    store = K.Keychain(lambda argv, **kw: calls.append(argv) or Result(0, " secret \n"))
    assert store.get_service("my.service") == "secret"
    assert calls == [["security", "find-generic-password", "-s", "my.service", "-w"]]
