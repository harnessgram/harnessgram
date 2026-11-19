import json

from ctb import cards, hooks as H, mirror, owner_questions as Q
from ctb.config import Config


def test_detects_direct_owner_questions_in_english_and_hebrew():
    assert Q.question("I can take either route. Which do you prefer?") == "Which do you prefer?"
    assert Q.question("האם תרצה שאמשיך?") == "האם תרצה שאמשיך?"


def test_question_survives_status_continuation_and_options():
    cases = (
        ("Should I use blue?\nSummary: Waiting for your choice.", "Should I use blue?"),
        ("Want me to deploy it?\nMeanwhile I'll keep working on the tests.", "Want me to deploy it?"),
        ("Should we use A or B?\n1. Use A\n2. Use B", "Should we use A or B?"),
        ("Which do you prefer?\n- Blue\n- Green", "Which do you prefer?"),
        ("Should I use blue?\nI will continue with the recommended option meanwhile.", "Should I use blue?"),
        ("האם תרצה שאפרוס?\nסיכום: ממתין לבחירה.", "האם תרצה שאפרוס?"),
        ("רוצה שאמשיך?", "רוצה שאמשיך?"),
    )
    for text, expected in cases:
        assert Q.question(text) == expected


def test_questions_not_addressed_to_owner_are_ignored():
    for text in ("Did you know this cache is stale?", "Who will approve the PR?", "האם הבדיקות עברו?"):
        assert Q.question(text) is None


def test_contractions_are_not_stripped_as_quotes():
    assert Q.question("I'll keep testing. Should I deploy?") == "Should I deploy?"
    assert Q.question("You're choosing now: which do you prefer?") == "You're choosing now: which do you prefer?"


def test_false_positive_guards():
    for text in (
        "Who knows?",
        '"Should I deploy?"',
        "The peer wrote, 'Should I deploy?' and then left.",
        "```python\nprint('Do you want this?')\n```",
        "> Telegram peer: Do you want this?",
        "# Should I deploy?",
        "Should I use the cache?\nThe answer is yes, because it is faster.",
        "Should I use the cache? I will use it because it is faster.",
    ):
        assert Q.question(text) is None


def test_explicit_card_command_is_scoped_to_current_turn():
    def row(role, content):
        return json.dumps({"type": role, "message": {"content": content}})
    old = row("assistant", [{"type": "tool_use", "input": {"command": "ctb decide ask 'old?'"}}])
    prompt = row("user", [{"type": "text", "text": "new turn"}])
    assert not Q.turn_used_explicit_card([old, prompt, row("assistant", [{"type": "text", "text": "Should I go?"}])])
    current = row("assistant", [{"type": "tool_use", "input": {"command": "ctb card ask 'go?'"}}])
    assert Q.turn_used_explicit_card([prompt, current])
    context = row("assistant", [{"type": "text", "text": "Reminder: ctb card ask 'go?'"},
                                {"type": "tool_result", "content": "ctb decide ask 'go?'"}])
    assert not Q.turn_used_explicit_card([prompt, context])


def test_one_claim_per_question_per_turn(tmp_path, monkeypatch):
    monkeypatch.setattr(Q, "state_dir", lambda: tmp_path)
    assert Q.claim_once("s", "turn", "card")
    assert not Q.claim_once("s", "turn", "card")
    assert Q.claim_once("s", "turn", "stop")


def test_turn_key_depends_on_last_real_prompt_only():
    prompt = json.dumps({"type": "user", "uuid": "prompt-2", "message": {"content": "choose"}})
    hook_context = json.dumps({"type": "user", "isMeta": True,
                               "message": {"content": "ctb card ask reminder"}})
    assistant = json.dumps({"type": "assistant", "message": {"content": "Should I use A?"}})
    assert Q.turn_key("s", [prompt, hook_context], "Should I use A?") == \
        Q.turn_key("s", [prompt, assistant], "Should I use B?")

    notification = json.dumps({"type": "user", "uuid": "generated",
                               "message": {"content": "<task-notification>done</task-notification>"}})
    assert Q.turn_key("s", [prompt, notification], "Should I use A?") == Q.turn_key(
        "s", [prompt], "Should I use A?")
    assert Q.has_real_prompt([notification]) is False


def test_automatic_card_answer_is_explicitly_not_approval():
    rec = {"kind": "ask", "text": "Use blue", "title": "Which do you prefer?",
           "automatic_question": True}
    text = cards.delivered_text(rec)
    assert "reply to this question only" in text
    assert "not approval of any sensitive action" in text


def test_stop_backstop_blocks_once_and_skips_dontask(tmp_path, monkeypatch, capsys):
    transcript = tmp_path / "turn.jsonl"
    rows = [
        {"type": "user", "message": {"content": "work"}},
        {"type": "assistant", "uuid": "a", "message": {"content": [
            {"type": "text", "text": "Should I use the blue version?"}]}}
    ]
    transcript.write_text("\n".join(json.dumps(x) for x in rows) + "\n")
    monkeypatch.setattr(H, "load_config", lambda: Config(
        bot_token="fake", owner_chat_id=1, auto_decision_cards=True))
    monkeypatch.setattr("ctb.assistant.is_assistant", lambda session: False)
    monkeypatch.setattr("ctb.topics.TopicMap.place_of", lambda self, session: object())
    monkeypatch.setattr(H, "spawn", lambda *a, **k: None)
    data = {"session_id": "session-1", "transcript_path": str(transcript)}
    assert H.hook_stop(json.dumps(data)) == 0
    assert json.loads(capsys.readouterr().out) == {
        "decision": "block", "reason": "post it with ctb decide ask, then keep working"}
    assert H.hook_stop(json.dumps(data)) == 0
    assert capsys.readouterr().out == ""

    data["session_id"] = "session-2"
    data["permission_mode"] = "dontAsk"
    assert H.hook_stop(json.dumps(data)) == 0
    assert capsys.readouterr().out == ""


def test_stop_backstop_skips_a_tail_without_its_real_prompt(tmp_path, monkeypatch, capsys):
    transcript = tmp_path / "long.jsonl"
    prompt = json.dumps({"type": "user", "uuid": "turn-1", "message": {"content": "work"}})
    padding = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_result", "content": "x" * (513 * 1024)}]}})
    answer = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Should I use blue?"}]}})
    transcript.write_text("\n".join((prompt, padding, answer)) + "\n")
    monkeypatch.setattr(H, "load_config", lambda: Config(
        bot_token="fake", owner_chat_id=1, auto_decision_cards=True))
    monkeypatch.setattr("ctb.topics.TopicMap.place_of", lambda self, session: object())
    monkeypatch.setattr(H, "spawn", lambda *a, **k: None)
    assert H.hook_stop(json.dumps({"session_id": "long", "transcript_path": str(transcript)})) == 0
    assert capsys.readouterr().out == ""


def test_stop_claim_is_not_taken_when_a_later_hook_result_replaces_the_reminder(
        tmp_path, monkeypatch, capsys):
    transcript = tmp_path / "turn.jsonl"
    transcript.write_text("\n".join((
        json.dumps({"type": "user", "uuid": "turn-1", "message": {"content": "work"}}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Should I use blue?"}]}}),
    )) + "\n")
    monkeypatch.setattr(Q, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(H, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(H, "load_config", lambda: Config(
        bot_token="fake", owner_chat_id=1, auto_decision_cards=True,
        enforce_language=True, target_language="he"))
    monkeypatch.setattr("ctb.assistant.is_assistant", lambda session: False)
    monkeypatch.setattr("ctb.topics.TopicMap.place_of", lambda self, session: object())
    monkeypatch.setattr(H, "spawn", lambda *a, **k: None)
    monkeypatch.setattr(H, "is_not_target", lambda *a: True)
    H.hook_stop(json.dumps({"session_id": "s", "transcript_path": str(transcript)}))
    assert json.loads(capsys.readouterr().out)["reason"] != "post it with ctb decide ask, then keep working"
    key = Q.turn_key("s", transcript.read_text().splitlines(), "")
    assert not Q.claimed("s", key, "stop")


def test_mirror_auto_card_success_failure_and_switch_off(tmp_path, monkeypatch):
    transcript = tmp_path / "turn.jsonl"
    transcript.write_text(json.dumps({"type": "user", "uuid": "turn-1",
                                      "message": {"content": "work"}}) + "\n")
    published, settled = [], []

    class Topics:
        def place_of(self, session):
            return type("Place", (), {"chat": 1, "thread": 2})()

    class Sender:
        first_reply_id = 9
        dropped = False
        last_error = None
        bot = object()
        topics = Topics()

        def send_reply(self, *args, **kwargs):
            return None

        def _chat(self, session):
            return 1

    monkeypatch.setattr(mirror, "build", lambda cfg: Sender())
    monkeypatch.setattr(mirror, "refresh_card", lambda *a, **k: None)
    monkeypatch.setattr(mirror, "record_outcome", lambda *a, **k: None)
    monkeypatch.setattr(mirror, "limit_offer", lambda *a, **k: settled.append("done"))
    monkeypatch.setattr("ctb.scheduled.suppressed", lambda *a, **k: False)
    monkeypatch.setattr("ctb.pins.link", lambda *a: "https://t.me/c/1/2/9")
    monkeypatch.setattr(Q, "claimed", lambda *a: False)
    monkeypatch.setattr(Q, "claim_once", lambda *a, **k: True)
    monkeypatch.setattr("ctb.sensitive_buttons.text_sensitive", lambda text: False)
    monkeypatch.setattr(cards, "publish", lambda *a, **k: published.append((a, k)))

    cfg = Config(auto_decision_cards=True)
    assert mirror.run_reply(cfg, "Title", "s", "Should I use blue?", False,
                            source_transcript=str(transcript)) == 0
    assert published and published[0][1] == {"routine": True, "automatic_question": True}
    assert settled == ["done"]

    long_question = "Should I use " + "a" * 130 + "?"
    monkeypatch.setattr("ctb.sensitive_buttons.text_sensitive", lambda text: True)
    assert mirror.run_reply(cfg, "Title", "s", long_question, False,
                            source_transcript=str(transcript)) == 0
    args, kwargs = published[-1]
    assert len(args[4]) == cards.TITLE_CHARS and long_question in args[5]
    assert kwargs == {"routine": False, "automatic_question": True}

    monkeypatch.setattr(cards, "publish", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("card broke")))
    assert mirror.run_reply(cfg, "Title", "s", "Should I use green?", False,
                            source_transcript=str(transcript)) == 0
    assert settled == ["done", "done", "done"]

    published.clear()
    monkeypatch.setattr(cards, "publish", lambda *a, **k: published.append((a, k)))
    cfg.auto_decision_cards = False
    assert mirror.run_reply(cfg, "Title", "s", "Should I use red?", False,
                            source_transcript=str(transcript)) == 0
    assert published == [] and settled == ["done", "done", "done", "done"]


def test_mirror_uses_the_stop_workers_frozen_turn_after_transcript_advances(tmp_path, monkeypatch):
    transcript = tmp_path / "turn.jsonl"
    frozen = [json.dumps({"type": "user", "uuid": "turn-1", "message": {"content": "work"}})]
    transcript.write_text(frozen[0] + "\n" + json.dumps(
        {"type": "user", "uuid": "turn-2", "message": {"content": "next owner prompt"}}) + "\n")
    checked, published = [], []

    class Topics:
        def place_of(self, session):
            return type("Place", (), {"chat": 1, "thread": 2})()

    class Sender:
        first_reply_id = 9
        dropped = False
        last_error = None
        bot = object()
        topics = Topics()

        def send_reply(self, *args, **kwargs):
            return None

        def _chat(self, session):
            return 1

    monkeypatch.setattr(mirror, "build", lambda cfg: Sender())
    monkeypatch.setattr(mirror, "refresh_card", lambda *a, **k: None)
    monkeypatch.setattr(mirror, "record_outcome", lambda *a, **k: None)
    monkeypatch.setattr(mirror, "limit_offer", lambda *a, **k: None)
    monkeypatch.setattr("ctb.scheduled.suppressed", lambda *a, **k: False)
    monkeypatch.setattr(Q, "claimed", lambda session, key, kind: checked.append(key) or True)
    monkeypatch.setattr(cards, "publish", lambda *a, **k: published.append(a))

    assert mirror.run_reply(Config(auto_decision_cards=True), "Title", "s", "Should I use blue?", False,
                            source_transcript=str(transcript), source_lines=frozen) == 0
    assert checked == [Q.turn_key("s", frozen, "")]
    assert published == []


def test_cards_store_automatic_question_fields(tmp_path, monkeypatch):
    # The storage API itself accepts and persists the two auto-card flags.
    monkeypatch.setattr(cards, "state_dir", lambda: tmp_path)
    monkeypatch.setattr("ctb.apns.notify", lambda rec: None)
    rec = cards.publish("s", 1, 2, "ask", "Question?", routine=True, automatic_question=True)
    assert rec["routine"] is True and rec["automatic_question"] is True
