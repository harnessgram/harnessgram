"""Signed decision answers (decision_sign.py): every answer route signs what the owner answered, who answered (the
user id the route verified, never the request body) and through which route, with a key that only the Keychain
holds. A Keychain or package failure leaves the answer unsigned but delivered. Never the real Keychain: each test
uses an in-memory store."""
import base64
import json

import pytest

from ctb import cli, decision_sign, decisions, panel, widgets
from ctb.accounts import _keychain_add as REAL_ADD, _keychain_item as REAL_ITEM  # before conftest stubs them
from ctb.config import state_dir
from test_decisions import ORBIT_S, ask, queued, rig, roomy_budget, server, cfg, stored, tap  # noqa: F401
from test_panel import OWNER, call, signed
from test_widget_devices import answer as widget_answer, fresh_rate, pair  # noqa: F401
from test_widgets import Bot, query, sent_stamp

ed25519 = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")


class MemoryStore:
    """A Keychain item in memory: "missing" until added, never replaced once there (like add without -U)."""

    def __init__(self, seed=None, status=None):
        self.seed, self.status, self.adds = seed, status, 0

    def get(self):
        if self.status:
            return self.status, None
        return ("ok", self.seed) if self.seed else ("missing", None)

    def add(self, seed):
        self.adds += 1
        if self.seed is None:
            self.seed = seed
        return True


@pytest.fixture
def store(monkeypatch):
    s = MemoryStore()
    monkeypatch.setattr(decision_sign, "STORE", s)
    return s


def logged():
    path = state_dir() / "poll.log"
    return path.read_text() if path.exists() else ""


def verify(sig):
    """The payload object, after checking the signature over the exact payload bytes with the stored key."""
    body = base64.b64decode(sig["payload"], validate=True)
    raw_key = base64.b64decode(sig["key"], validate=True)
    raw_sig = base64.b64decode(sig["signature"], validate=True)
    assert len(raw_key) == 32 and len(raw_sig) == 64
    ed25519.Ed25519PublicKey.from_public_bytes(raw_key).verify(raw_sig, body)  # raises InvalidSignature
    obj = json.loads(body.decode("utf-8"))
    assert body == json.dumps(obj, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode("utf-8")
    return obj


def bridge_key(store):
    seed = base64.b64decode(store.seed)
    pub = ed25519.Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes_raw()
    return base64.b64encode(pub).decode()


def check_payload(obj, rec_stored, by, via):
    assert obj == {"v": 1, "kind": "harnessgram-decision-answer", "id": rec_stored["id"], "title": rec_stored["title"],
                   "detail": rec_stored["detail"], "choice": rec_stored["choice"], "note": rec_stored["note"],
                   "created": rec_stored["created"], "closed": rec_stored["closed"],
                   "routine": rec_stored["routine"], "by": by, "via": via}
    assert type(obj["by"]) is int


# --- the three routes ----------------------------------------------------------------------------------------------

def test_a_panel_answer_is_signed_by_the_owner_over_the_exact_payload(rig, server, store):
    rec = ask(rig, title="Ship the fix? «כן»", detail="שורה אחת\nline two")
    assert tap(server, rec["id"], "yes", note="go ahead, בבקשה")[0] == 200
    saved = stored()[rec["id"]]
    obj = verify(saved["answer_sig"])
    check_payload(obj, saved, OWNER, "panel")
    assert (obj["choice"], obj["note"], obj["title"]) == ("yes", "go ahead, בבקשה", rec["title"])
    assert saved["answer_sig"]["key"] == bridge_key(store) and store.adds == 1  # created once, on first use
    assert len(queued(rig, ORBIT_S)) == 1


def test_the_answering_user_comes_from_the_route_never_the_body(rig, server, store):
    rec = ask(rig)
    body = json.dumps({"id": rec["id"], "choice": "yes", "by": 999, "via": "widget"})
    assert tap(server, rec["id"], raw=body)[0] == 400  # unknown fields are refused, nothing is answered
    assert stored()[rec["id"]]["state"] == "open"
    # only the owner's Telegram login answers at all, so the signed id is always the server's owner
    assert tap(server, rec["id"], init=signed(user_id=999))[0] == 403
    assert tap(server, rec["id"])[0] == 200
    obj = verify(stored()[rec["id"]]["answer_sig"])
    assert (obj["by"], obj["via"]) == (OWNER, "panel")


def test_a_device_widget_answer_is_signed_with_via_widget(rig, server, store):
    token = pair(server)["token"]
    rec = ask(rig, title="Ship the fix?", routine=True)
    status, _, data = widget_answer(server, token, rec["id"])
    assert status == 200, data
    saved = stored()[rec["id"]]
    check_payload(verify(saved["answer_sig"]), saved, OWNER, "widget")
    body = json.dumps({"id": rec["id"], "choice": "yes", "by": 999}).encode()
    assert call(server, path="/api/widget-decide", body=body,
                headers={"Authorization": "Bearer " + token})[0] == 400


def test_a_chat_widget_answer_is_signed_with_via_chat_widget(rig, store, monkeypatch):
    rec = ask(rig)
    bot = Bot()
    widgets.send_decision(rig.cfg, bot, rec)
    monkeypatch.setattr(decisions, "announce", lambda *args: None)
    assert widgets.handle_query(query(rig.cfg, sent_stamp(bot)), rig.cfg, bot, rig.box) == "widget-answered"
    saved = stored()[rec["id"]]
    check_payload(verify(saved["answer_sig"]), saved, rig.cfg.owner_chat_id, "chat-widget")


def test_every_answer_route_names_its_route():
    """Each caller of decisions.answer passes `by` and a known `via`; a new route must choose one too."""
    import inspect
    import re
    for module, vias in ((panel, {"panel", "widget"}), (widgets, {"chat-widget"})):
        source = inspect.getsource(module)
        calls = re.findall(r"decisions\.answer\((.*?)\)\n", source, re.S)
        assert calls and all("by=" in c and "via=" in c for c in calls), module.__name__
        assert set(re.findall(r'via="([a-z-]+)"', " ".join(calls))) == vias
    assert decision_sign.VIAS == {"panel", "widget", "chat-widget"}


# --- the record ----------------------------------------------------------------------------------------------------

def test_the_signature_survives_a_save_and_a_malformed_one_is_dropped(rig, server, store):
    first, second = ask(rig, title="One?"), ask(rig, title="Two?")
    assert tap(server, first["id"])[0] == 200
    sig = stored()[first["id"]]["answer_sig"]
    assert tap(server, second["id"], "no")[0] == 200  # another answer loads and saves the whole file
    assert stored()[first["id"]]["answer_sig"] == sig
    data = stored()
    bad = [dict(sig, signature=sig["signature"][:-4]), dict(sig, key="not base64!"), dict(sig, extra="x"),
           {k: v for k, v in sig.items() if k != "payload"}, dict(sig, payload="A" * 20000), "text", None]
    for value in bad:
        data[first["id"]]["answer_sig"] = value
        assert "answer_sig" not in decisions._record(first["id"], data[first["id"]]), value
    data[first["id"]]["answer_sig"] = sig
    assert decisions._record(first["id"], data[first["id"]])["answer_sig"] == sig
    data[first["id"]].update(state="open")  # only an answered record carries one
    assert "answer_sig" not in decisions._record(first["id"], data[first["id"]])


def test_a_hand_written_signature_from_another_key_does_not_verify(rig, server, store):
    rec = ask(rig)
    assert tap(server, rec["id"])[0] == 200
    sig = stored()[rec["id"]]["answer_sig"]
    forged_key = ed25519.Ed25519PrivateKey.generate()
    body = base64.b64decode(sig["payload"])
    forged = dict(sig, signature=base64.b64encode(forged_key.sign(body)).decode())
    with pytest.raises(Exception):
        verify(forged)
    assert sig["key"] == bridge_key(store)


# --- failures never block the owner --------------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["error"])
def test_a_keychain_failure_leaves_the_answer_unsigned_and_delivered(rig, server, monkeypatch, status):
    s = MemoryStore(status=status)
    monkeypatch.setattr(decision_sign, "STORE", s)
    rec = ask(rig)
    assert tap(server, rec["id"])[0] == 200
    saved = stored()[rec["id"]]
    assert saved["state"] == "answered" and "answer_sig" not in saved and s.adds == 0  # never created on an error
    assert len(queued(rig, ORBIT_S)) == 1
    assert "decision sign: unsigned (keychain)" in logged()


def test_a_missing_package_leaves_the_answer_unsigned(rig, server, store, monkeypatch):
    def missing():
        raise decision_sign.Unavailable("no-cryptography")
    monkeypatch.setattr(decision_sign, "_ed25519", missing)
    rec = ask(rig)
    assert tap(server, rec["id"])[0] == 200
    assert "answer_sig" not in stored()[rec["id"]] and store.adds == 0
    assert "decision sign: unsigned (no-cryptography)" in logged()


def test_a_malformed_seed_is_never_replaced(rig, server, monkeypatch):
    s = MemoryStore(seed="c2hvcnQ=")
    monkeypatch.setattr(decision_sign, "STORE", s)
    rec = ask(rig)
    assert tap(server, rec["id"])[0] == 200
    assert "answer_sig" not in stored()[rec["id"]] and s.seed == "c2hvcnQ=" and s.adds == 0
    assert "decision sign: unsigned (bad-seed)" in logged()


def test_a_failing_signature_still_delivers(rig, server, store, monkeypatch):
    def broken(*a, **k):
        raise ValueError("boom")
    monkeypatch.setattr(decision_sign, "sign", broken)
    rec = ask(rig)
    assert tap(server, rec["id"])[0] == 200
    assert "answer_sig" not in stored()[rec["id"]] and len(queued(rig, ORBIT_S)) == 1
    assert "decision sign: unsigned (ValueError)" in logged() and "boom" not in logged()


def test_an_answer_without_a_route_is_unsigned(rig, store):
    rec = ask(rig)
    decisions.answer(rig.cfg, rig.box, rec["id"], "yes")
    assert "answer_sig" not in stored()[rec["id"]] and store.adds == 0
    assert "decision sign: unsigned (route)" in logged()


def test_reopening_after_a_failed_queue_drops_the_signature(rig, server, store, monkeypatch):
    rec = ask(rig)
    seen = []

    def broken(session, item, **kw):
        seen.append(stored()[rec["id"]].get("answer_sig"))  # saved signed before the message is queued
        raise OSError("disk")

    real = rig.box.put
    monkeypatch.setattr(rig.box, "put", broken)
    assert tap(server, rec["id"], "yes")[0] == 500
    assert seen and decision_sign.well_formed(seen[0])
    reopened = stored()[rec["id"]]
    assert reopened["state"] == "open" and "answer_sig" not in reopened
    monkeypatch.setattr(rig.box, "put", real)
    assert tap(server, rec["id"], "no")[0] == 200
    obj = verify(stored()[rec["id"]]["answer_sig"])
    assert obj["choice"] == "no"


def test_the_seed_never_reaches_the_record_or_the_log(rig, server, store):
    rec = ask(rig)
    assert tap(server, rec["id"])[0] == 200
    text = (state_dir() / decisions.STATE_FILE).read_text() + logged()
    assert store.seed and store.seed not in text


# --- `ctb decide public-key` ---------------------------------------------------------------------------------------

def test_public_key_prints_only_the_raw_32_byte_key(store, capsys):
    assert cli.main(["decide", "public-key"]) == 0
    out = capsys.readouterr()
    line = out.out.strip()
    assert out.out == line + "\n" and len(base64.b64decode(line, validate=True)) == 32 and store.adds == 1
    assert line == bridge_key(store) and store.seed not in out.out + out.err
    assert cli.main(["decide", "public-key"]) == 0 and capsys.readouterr().out.strip() == line and store.adds == 1


def test_public_key_without_the_package_says_how_to_install_it(store, capsys, monkeypatch):
    def missing():
        raise decision_sign.Unavailable("no-cryptography")
    monkeypatch.setattr(decision_sign, "_ed25519", missing)
    assert cli.main(["decide", "public-key"]) == 1
    out = capsys.readouterr()
    assert out.out == "" and "harnessgram[sign]" in out.err and store.adds == 0


def test_public_key_with_an_unreadable_keychain_creates_nothing(monkeypatch, capsys):
    s = MemoryStore(status="error")
    monkeypatch.setattr(decision_sign, "STORE", s)
    assert cli.main(["decide", "public-key"]) == 1
    assert "Keychain" in capsys.readouterr().err and s.adds == 0


# --- the Keychain helpers ------------------------------------------------------------------------------------------

def test_the_keychain_helpers_pass_the_seed_on_stdin_only(monkeypatch):
    import subprocess
    from ctb import accounts
    runs = []

    def fake_run(argv, **kw):
        runs.append((argv, kw.get("input")))
        if argv[1] == "find-generic-password":
            return subprocess.CompletedProcess(argv, 44, "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(accounts.subprocess, "run", fake_run)
    seed = base64.b64encode(b"\x01" * 32).decode()
    assert REAL_ITEM(decision_sign.SERVICE, decision_sign.ACCOUNT) == ("missing", None)
    assert REAL_ADD(decision_sign.SERVICE, decision_sign.ACCOUNT, seed) is True
    (find, _), (add, stdin) = runs
    assert find == ["security", "find-generic-password", "-s", decision_sign.SERVICE, "-a", decision_sign.ACCOUNT, "-w"]
    assert add == ["security", "-i"] and seed not in " ".join(add)
    assert stdin.startswith("add-generic-password -s ") and " -U" not in stdin
    assert seed not in stdin and ("-X " + seed.encode().hex()) in stdin
