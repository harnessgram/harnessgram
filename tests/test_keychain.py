import subprocess

from ctb.keychain import Keychain


def test_write_uses_hex_on_stdin_and_never_secret_argv():
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    secret = 'private value " with spaces'
    assert Keychain(run).set(('service " name', "account\\name"), secret)
    argv, kwargs = calls[0]
    assert argv == ["security", "-i"] and secret not in " ".join(argv)
    assert secret not in kwargs["input"]
    encoded = kwargs["input"].split("-X ", 1)[1].strip()
    assert bytes.fromhex(encoded).decode() == secret


def test_timeout_is_cooled_down_without_a_second_spawn():
    calls = []
    now = [100.0]

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    store = Keychain(run, clock=lambda: now[0], timeout=0.01, cooldown=30)
    assert store.get(("sample.service", "sample-account")) is None
    assert store.get(("sample.service", "sample-account")) is None
    assert len(calls) == 1
    now[0] += 31
    assert store.get(("sample.service", "sample-account")) is None and len(calls) == 2


def test_missing_item_is_not_cooled_down():
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 44, "", "")

    store = Keychain(run, cooldown=30)
    assert store.get_status(("sample.missing", "account")) == ("missing", None)
    assert store.get_status(("sample.missing", "account")) == ("missing", None)
    assert len(calls) == 2


def test_concurrent_reads_of_one_item_spawn_a_single_child():
    import threading
    from ctb import keychain as K
    calls, gate = [], threading.Event()

    def runner(args, **kw):
        calls.append(args)
        gate.wait(2)
        class R:
            returncode, stdout = 51, ""
        return R()

    kc = K.Keychain(runner=runner)
    item = ("ctb-test-concurrent", "a")
    K._FAILURES.pop(item, None)
    threads = [threading.Thread(target=kc.get, args=(item,)) for _ in range(5)]
    for th in threads:
        th.start()
    gate.set()
    for th in threads:
        th.join(5)
    assert len(calls) == 1
    K._FAILURES.pop(item, None)


def test_closing_the_apns_client_releases_the_key():
    from types import SimpleNamespace
    from ctb import apns
    client = apns.APNSClient.__new__(apns.APNSClient)
    client._key, client.http = "secret-pem", SimpleNamespace(close=lambda: None)
    client.close()
    assert client._key is None
