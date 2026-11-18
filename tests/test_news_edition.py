import ctb.news.edition as news_edition
import ctb.radio as radio
from ctb.news import (ChannelMessage, EditionBuilder, FakeSource, FakeSummarizer, PrivateEditionQueue, PrivateRadio,
                      BridgeSummarizer, HourlyEditionScheduler, TelegramChannelSource, cluster_messages, jaccard,
                      normalize, sha256)

NOW = 10_000.0


def test_news_gate_plays_a_private_mixer_clip_without_attribute_error():
    public = radio.SpeechMixer(b"\0\0" * radio.FRAME)
    mixer = radio.PrivateMixer(b"\0\0" * radio.FRAME, public)
    mixer.background = None
    mixer.gain = 0
    mixer.add_private(b"\1\0" * radio.FRAME, 7)
    allowed = lambda: True
    allowed.playing = allowed
    gates = {7: allowed}
    mixer.active_gate = lambda tag: tag not in gates or getattr(gates[tag], "playing", gates[tag])()
    assert mixer.next() == b"\1\0" * radio.FRAME


def message(text, channel="one", title="מקור ראשון", age=10, urgency=50):
    return ChannelMessage(channel, title, text, NOW - age, urgency)


def test_normalize_hash_dedup_and_jaccard_clustering():
    first = message("עדכון חשוב: הכביש נפתח כעת 🚗")
    second = message("עדכון חשוב הכביש נפתח כעת!", "two", "מקור שני")
    assert normalize(first.text) == normalize(second.text)
    assert sha256(first.text) == sha256(second.text)
    assert jaccard(first.text, second.text) == 1
    groups = cluster_messages([first, second, message("תחזית מזג האוויר השתנתה", "three", "מקור שלישי")])
    assert len(groups) == 2
    assert sorted(groups[0].sources) == sorted(["מקור ראשון", "מקור שני"])


def test_suitability_drops_item_and_generated_summary():
    safe = message("הכביש נפתח לתנועה")
    blocked = message("תיאור blocked", "two", "מקור שני")
    summarizer = FakeSummarizer(["עדכון רגוע"])
    script = EditionBuilder(FakeSource([safe, blocked]), summarizer,
                            lambda text: "blocked" not in text).build(True, ["one", "two"], NOW)
    assert summarizer.calls == 1
    assert "עדכון רגוע" in script and "מקור ראשון" in script and "מקור שני" not in script


def test_empty_channel_list_is_off_without_source_or_model_call():
    summarizer = FakeSummarizer(["לא אמור להיקרא"])
    assert EditionBuilder(FakeSource([message("חדשות")]), summarizer, lambda _: True).build(True, [], NOW) == ""
    assert summarizer.calls == 0


def test_stale_urgent_alarm_is_not_spoken():
    old = message("אירוע דחוף", age=421, urgency=90)
    summarizer = FakeSummarizer(["לא אמור להיקרא"])
    assert EditionBuilder(FakeSource([old]), summarizer, lambda _: True).build(True, ["one"], NOW) == ""
    assert summarizer.calls == 1


def test_stale_item_is_filtered_after_the_model_marks_it_urgent():
    old = message("אירוע שדורש סיווג", age=421, urgency=10)
    summarizer = FakeSummarizer([("התראה דחופה", 90)])
    assert EditionBuilder(FakeSource([old]), summarizer, lambda _: True).build(True, ["one"], NOW) == ""
    assert summarizer.calls == 1


def test_private_only_one_at_a_time_and_budget_rechecked_at_speak_time():
    class Radio(PrivateRadio):
        def speak_news_edition(self, text, gate):
            spoken.append((text, gate()))
            return True

    queue, spoken = PrivateEditionQueue(), []
    assert queue.destination == "private-radio"
    assert queue.offer("מהדורה אחת")
    assert not queue.offer("מהדורה שנייה")
    assert not queue.speak_next(lambda *_: spoken.append("public"), lambda: True, lambda _: True)
    assert not queue.speak_next(object(), lambda: True, lambda _: True)
    assert spoken == [] and queue.pending == "מהדורה אחת"
    budget = iter([True, True])
    assert queue.speak_next(Radio(), lambda: next(budget), lambda _: True)
    assert spoken == [("מהדורה אחת", True)] and not queue.pending


def test_fuzzy_then_exact_duplicates_share_the_same_cluster():
    newest_a = message("א ב ג ד", age=1)
    similar_b = message("א ב ג ד ה", "two", "מקור שני", age=2)
    exact_b = message("א ב ג ד ה!", "three", "מקור שלישי", age=3)
    groups = cluster_messages([exact_b, similar_b, newest_a])
    assert len(groups) == 1
    assert groups[0].sources == ["מקור ראשון", "מקור שני", "מקור שלישי"]


def test_complete_script_including_furniture_obeys_limit(monkeypatch):
    opening = news_edition.t("news_edition_open", lang="he")
    closing = news_edition.t("news_edition_close", lang="he")
    monkeypatch.setattr(news_edition, "MAX_SCRIPT_CHARS", len(opening) + len(closing) + 2)
    builder = EditionBuilder(FakeSource([message("חדשות ממש")]), FakeSummarizer(["סיכום"]), lambda _: True)
    assert builder.build(True, ["one"], NOW) == ""


def test_content_free_posts_are_discarded_before_clustering():
    posts = [message(text, channel=str(index)) for index, text in
             enumerate(["", "...!!!", "😀🚗", "https://example.com/path"])]
    source = FakeSource(posts)
    summarizer = FakeSummarizer([])
    assert EditionBuilder(source, summarizer, lambda _: True).build(True, ["0", "1", "2", "3"], NOW) == ""
    assert summarizer.calls == 0


def test_rejected_ranked_summary_does_not_consume_item_slot():
    posts = [message("ראשון", urgency=90), message("שני", "two", urgency=80),
             message("שלישי", "three", urgency=70)]
    summarizer = FakeSummarizer(["אסור", "מתאים", "גם מתאים"])
    script = EditionBuilder(FakeSource(posts), summarizer, lambda text: text != "אסור", max_items=2).build(
        True, ["one", "two", "three"], NOW)
    assert summarizer.calls == 3
    assert "מתאים" in script and "גם מתאים" in script


def test_disabled_switch_never_calls_source():
    class Source:
        def fetch(self, channels, since):
            raise AssertionError("disabled edition read its source")

    assert EditionBuilder(Source(), FakeSummarizer([]), lambda _: True).build(False, ["one"], NOW) == ""


def test_rejected_summary_and_final_script_are_not_returned():
    post = message("חדשות")
    assert EditionBuilder(FakeSource([post]), FakeSummarizer(["סיכום אסור"]),
                          lambda text: "אסור" not in text).build(True, ["one"], NOW) == ""
    assert EditionBuilder(FakeSource([post]), FakeSummarizer(["סיכום"]),
                          lambda text: not text.startswith("הנה")).build(True, ["one"], NOW) == ""


def test_hebrew_marks_and_final_letters_normalize_together():
    assert normalize("שָׁלוֹם מֶלֶךְ") == normalize("שלום מלכ")
    assert sha256("כמנפצ") == sha256("ךםןףץ")


def test_tied_ranking_uses_normalized_representative_digest():
    posts = [message("חדשות ב", "two"), message("חדשות א", "one")]
    expected = sorted(posts, key=lambda item: sha256(item.text))

    class RecordingSummarizer:
        def __init__(self):
            self.seen = []

        def summarize(self, cluster, instruction):
            self.seen.append(cluster.representative.text)
            return cluster.representative.text

    summarizer = RecordingSummarizer()
    EditionBuilder(FakeSource(reversed(posts)), summarizer, lambda _: True).build(True, ["one", "two"], NOW)
    assert summarizer.seen == [item.text for item in expected]


def test_raw_model_summary_is_checked_before_cleaning():
    post = message("חדשות")
    builder = EditionBuilder(FakeSource([post]), FakeSummarizer(["סיכום https://blocked.example"]),
                             lambda text: "blocked.example" not in text)
    assert builder.build(True, ["one"], NOW) == ""


def test_tied_messages_pick_the_same_representative_in_any_order():
    first = message("אותה ידיעה בדיוק כאן", channel="a", title="א")
    second = message("אותה ידיעה בדיוק כאן היום", channel="b", title="ב")
    forward = cluster_messages([first, second])
    backward = cluster_messages([second, first])
    assert [c.representative for c in forward] == [c.representative for c in backward]


def test_channel_reader_uses_account_helper_and_enforces_each_bound(monkeypatch):
    calls = []

    def helper(settings, action, **kwargs):
        calls.append((settings, action, kwargs))
        rows = [{"channel_id": "one", "channel_title": "מקור", "text": str(i),
                 "published_at": NOW - 1} for i in range(55)]
        rows += [{"channel_id": "one", "text": "old", "published_at": NOW - 3601},
                 {"channel_id": "not-selected", "text": "other", "published_at": NOW}]
        return {"ok": True, "messages": rows}

    monkeypatch.setattr("ctb.radio.account_helper", helper)
    posts = list(TelegramChannelSource({"telethon_python": "configured"}).fetch(["one"], NOW - 3600))
    assert len(posts) == 50
    assert calls[0][1] == "read_channels"
    assert calls[0][2]["limit"] == 50 and calls[0][2]["since"] == NOW - 3600


def test_summarizer_falls_back_to_one_trimmed_sentence_without_model():
    cluster = cluster_messages([message("משפט ראשון. משפט שני שלא ייקרא")])[0]
    assert BridgeSummarizer(lambda *_: "").summarize(cluster, "instruction") == "משפט ראשון."


def test_summarizer_accepts_fenced_json_and_uses_urgency_for_ranking():
    posts = [message("ראשון", "one"), message("שני", "two")]
    replies = iter(['```json\n{"summary":"ראשון רגוע","urgency":10}\n```',
                    '{"summary":"שני חשוב","urgency":90}'])
    script = EditionBuilder(FakeSource(posts), BridgeSummarizer(lambda *_: next(replies)), lambda _: True).build(
        True, ["one", "two"], NOW)
    assert script.index("שני חשוב") < script.index("ראשון רגוע")


def test_background_worker_logs_only_exception_class(monkeypatch):
    class Broken:
        def build(self, *args):
            raise RuntimeError("private source text")

    seen = []
    monkeypatch.setattr("ctb.radio.log", seen.append)
    scheduler = HourlyEditionScheduler(Broken(), PrivateEditionQueue(), PrivateRadio(), lambda _: True, lambda: True)
    assert not scheduler.step(True, ["one"], NOW)
    assert seen == ["news edition: stopped (RuntimeError)"]


def test_hourly_scheduler_is_off_without_reads_and_uses_private_outlet_only():
    class Source:
        calls = 0

        def fetch(self, channels, since):
            self.calls += 1
            return [message("עדכון")]

    class Radio(PrivateRadio):
        def speak_news_edition(self, script, gate):
            spoken.append(script)
            return gate()

    source, spoken = Source(), []
    scheduler = HourlyEditionScheduler(
        EditionBuilder(source, BridgeSummarizer(), lambda _: True), PrivateEditionQueue(), Radio(),
        lambda _: True, lambda: True)
    assert not scheduler.step(False, ["one"], NOW)
    assert not scheduler.step(True, [], NOW)
    assert source.calls == 0
    assert scheduler.step(True, ["one"], NOW)
    assert not scheduler.step(True, ["one"], NOW + 3599)
    assert scheduler.step(True, ["one"], NOW + 3600)
    assert source.calls == 2 and len(spoken) == 2
