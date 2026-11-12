"""Team radio: the script, change detection, the daily voice cap, the panel switch, and no secret on any argv
or in any log."""
import json
import re
import shutil
import subprocess
import threading
import time

import pytest

from conftest import GOOD_TOKEN
from ctb import panel, radio, radio_rtmp, radio_script as R, radio_voice as V
from ctb.config import Config
from test_panel import OWNER, call, signed

SECRET_URL = "rtmps://dc9-9.rtmp.example.org/s/"
SECRET_KEY = "9999999999:SeCrEtStReAmKeY"


def snap(**kw):
    state = {"decisions": [{"id": "a1", "title": "למזג את האצווה של אורה?", "topic": "אורה: מיזוג"}],
             "reminders": [], "sessions": [{"id": "s1", "name": "מסלול: מסך הבית", "state": "working"},
                                           {"id": "s2", "name": "מזווה: תשלומים", "state": "idle"},
                                           {"id": "s3", "name": "ישן", "state": "ended"}],
             "quota": [{"label": "Claude", "five_hour": 40, "weekly": 91}]}
    state.update(kw.pop("state", {}))
    ledger = kw.pop("ledger", ([{"key": "orbit#4", "project": "orbit", "text": "Promote main 905a900f8 to production",
                                 "updated": 999_000.0},
                                {"key": "lumen#2", "project": "lumen", "text": "לבחור כיוון עיצוב למסך הקריאה",
                                 "updated": 999_500.0}],
                               [{"key": "orbit#3", "project": "orbit", "text": "הסרטון עלה ללוח הטיסות",
                                "updated": 999_900.0}],
                               5))
    return R.snapshot(state, kw.pop("asking", ["s2"]), ledger, kw.pop("quota_rows", [{"label": "Codex", "pct": 79}]),
                      kw.pop("now", 1_000_000.0))


NAMES = {"orbit": "מסלול", "lumen": "אורה"}


# --- words ---------------------------------------------------------------------------------------

def test_numbers_in_hebrew_words():
    assert R.number_he(3) == "שלושה" and R.number_he(3, "f") == "שלוש"
    assert R.number_he(79) == "שבעים ותשעה"
    assert R.number_he(12, "f") == "שתים עשרה"
    assert R.number_he(120) == "מאה ועשרים"
    assert R.number_he(1500) == "1500"
    assert R.count_he(1, "דבר", "דברים") == "דבר אחד"
    assert R.count_he(2, "החלטה", "החלטות", "f") == "שתי החלטות"
    assert R.count_he(4, "דבר", "דברים") == "ארבעה דברים"
    assert R.clock_he(14, 31) == "שתיים וחצי"
    assert R.clock_he(9, 0) == "תשע"
    assert R.clock_he(23, 58) == "שתים עשרה"


def test_script_reads_what_waits_first_and_counts_english_items():
    s = snap()
    text = R.build_script(s, {}, NAMES, "he", when=time.localtime(0))
    assert text.startswith("רדיו הצוות, השעה")
    assert "יש ארבעה דברים חדשים שמחכים לך." in text
    # the three newest fresh items are read; the English one is the fourth, so it is counted ("ועוד אחד חדש")
    # (2026-11-11: every item here is within FRESH_WINDOW; the sample ledger times were 1.0 and 2.0, i.e. ancient)
    assert "הסשן מזווה: תשלומים מחכה לתשובה שלך." in text
    assert "החלטה בקוקפיט, אורה: מיזוג: למזג את האצווה של אורה?" in text
    assert "אורה: לבחור כיוון עיצוב למסך הקריאה." in text
    assert "905a900f8" not in text and "ועוד אחד חדש." in text
    assert "בשעות האחרונות הושלם דבר אחד. מסלול: הסרטון עלה ללוח הטיסות." in text
    assert "עכשיו עובד סשן אחד, ואחד ממתין" in text
    assert "שים לב: המכסה השבועית של קלוד הגיעה לתשעים ואחד אחוז." in text
    assert "חמש השעות" not in text  # Claude has no 5-hour limit
    assert "Codex" not in text  # 79% is under the warning line
    assert len(text) <= R.MAX_SCRIPT


# --- only fresh things are read (2026-11-11: "the radio mentions old and irrelevant things") -----------------

NOW = 1_000_000.0
OLD = NOW - 3 * 86400
FRESH = NOW - 600


def dec(key, title, at):
    return {"id": key, "title": title, "topic": "אורה: מיזוג", "age": NOW - at}


def only(decisions=(), ledger=(), done=(), asking=(), sessions=()):
    """A snapshot with just these waiting items (ledger rows: (id, text, updated))."""
    rows = [{"key": f"orbit#{i}", "project": "orbit", "text": t, "updated": u} for i, t, u in ledger]
    drows = [{"key": f"orbit#{i}", "project": "orbit", "text": t, "updated": u} for i, t, u in done]
    return R.snapshot({"decisions": list(decisions), "sessions": list(sessions), "quota": []}, asking,
                      (rows, drows, 0), [], NOW)


BACKLOG = [dec(f"o{n}", f"החלטה ישנה מספר {n} על הצבעים", OLD - n) for n in range(5)]


def test_only_fresh_items_are_read_and_the_backlog_is_one_sentence():
    s = only(decisions=BACKLOG + [dec("n1", "לאשר את הגרסה החדשה?", FRESH)])
    text = R.build_script(s, {}, NAMES, "he", when=time.localtime(0))
    assert "יש דבר אחד חדש שמחכה לך." in text and "לאשר את הגרסה החדשה" in text
    assert "החלטה ישנה" not in text and "מחכים לך:" not in text  # no breakdown, no old text
    assert "חוץ מזה מחכים בקוקפיט חמישה דברים ישנים יותר." in text
    assert R.attention_keys(s) == ["d:n1"]


def test_a_start_with_only_old_backlog_says_nothing_new_and_is_not_urgent():
    s = only(decisions=BACKLOG, ledger=[(1, "פריט ישן ברשימה", OLD)])
    text = R.build_script(s, {}, NAMES, "he", when=time.localtime(0))
    assert "אין שום דבר חדש שמחכה לך. חוץ מזה מחכים בקוקפיט שישה דברים ישנים יותר." in text
    assert "החלטה ישנה" not in text and "פריט ישן" not in text and "מחכים לך:" not in text
    assert R.decide(s, {}, NOW, 1200) != "urgent"  # no last.json at all: the old backlog is never urgent
    assert R.decide(s, R.remember(s, NOW), NOW + 3600, 1200) is None


def test_a_start_with_two_fresh_items_and_an_old_backlog():
    s = only(decisions=BACKLOG + [dec("n1", "לאשר את הגרסה החדשה?", FRESH)],
             ledger=[(7, "לבחור צבע לכפתור", NOW - 1800)])
    text = R.build_script(s, {}, NAMES, "he", when=time.localtime(0))
    assert "יש שני דברים חדשים שמחכים לך." in text
    assert "החלטה בקוקפיט, אורה: מיזוג: לאשר את הגרסה החדשה?" in text and "מסלול: לבחור צבע לכפתור." in text
    assert text.count("חוץ מזה") == 1 and "חמישה דברים ישנים יותר" in text
    assert R.decide(s, {}, NOW, 1200) == "urgent"


def test_a_fresh_item_that_cannot_be_said_is_counted_and_never_replaced_by_an_old_one():
    s = only(decisions=BACKLOG[:2], ledger=[(1, "Promote main to production", FRESH),
                                            (2, "Fix the CI cache", FRESH - 5)])
    text = R.build_script(s, {}, NAMES, "he", when=time.localtime(0))
    assert "יש שני דברים חדשים שמחכים לך. את החדשים אי אפשר להקריא בעברית." in text
    assert "החלטה ישנה" not in text and "Promote" not in text and "מחכים לך:" not in text
    one = only(decisions=BACKLOG[:2], ledger=[(1, "Promote main to production", FRESH)])
    assert "יש דבר אחד חדש שמחכה לך. את החדש אי אפשר להקריא בעברית." in R.build_script(
        one, {}, NAMES, "he", when=time.localtime(0))


def test_a_waiting_session_is_always_fresh_and_the_slots_count_fresh_items_only():
    sessions = [{"id": "s1", "name": "מסלול: מסך הבית", "state": "idle"}]
    s = only(decisions=BACKLOG + [dec(f"n{n}", f"שאלה חדשה {n}?", FRESH - n) for n in range(5)], asking=["s1"],
             sessions=sessions)
    text = R.build_script(s, {}, NAMES, "he", when=time.localtime(0))
    assert "הסשן מסלול: מסך הבית מחכה לתשובה שלך." in text
    assert "שאלה חדשה 0" in text and "שאלה חדשה 1" in text and "שאלה חדשה 2" not in text  # three slots
    assert "יש שישה דברים חדשים שמחכים לך." in text and "ועוד שלושה חדשים." in text and "החלטה ישנה" not in text


def test_the_edge_of_the_window():
    inside = dec("a", "בתוך החלון?", NOW - R.FRESH_WINDOW)
    outside = dec("b", "מחוץ לחלון?", NOW - R.FRESH_WINDOW - 1)
    assert R.attention_keys(only(decisions=[inside, outside])) == ["d:a"]


def test_a_quiet_day_says_nothing_waits():
    s = snap(state={"decisions": [], "sessions": [], "quota": []}, asking=[], ledger=([], [], 0), quota_rows=[])
    text = R.build_script(s, {}, NAMES, "he", when=time.localtime(0))
    assert "כרגע שום דבר לא מחכה לך" in text and text.endswith("זה הכול לעכשיו.")


def test_card_isolates_latin_runs():
    secs = R.card_sections(snap(), NAMES, "he")
    assert secs[0]["head"].startswith("⏳") and secs[0]["tone"] == "warn"
    assert any("⁨905a900f8⁩" in line for line in secs[0]["lines"])


# --- change detection ----------------------------------------------------------------------------

def test_new_item_waiting_is_urgent_and_same_state_is_quiet():
    s = snap()
    assert R.decide(s, {}, 1_000_000.0, 1200) == "urgent"
    last = R.remember(s, 1_000_000.0)
    assert R.decide(s, last, 1_000_060.0, 1200) is None
    assert R.decide(s, last, 1_000_000.0 + 3600, 1200) is None  # nothing changed: no repeated talk
    more = snap(state={"decisions": [{"id": "a1", "title": "x"}, {"id": "b2", "title": "חדש"}]})
    assert R.decide(more, last, 1_000_060.0, 1200) == "urgent"


def test_digest_ignores_an_old_item_that_comes_or_goes():
    fresh = [dec("n1", "לאשר?", FRESH)]
    with_old, without_old = only(decisions=fresh + BACKLOG), only(decisions=fresh + BACKLOG[:2])
    assert R.digest(with_old) == R.digest(without_old) == R.digest(only(decisions=fresh))
    last = R.remember(with_old, NOW)
    assert last["attention"] == ["d:n1"]
    assert R.decide(without_old, last, NOW + 7200, 1200) is None  # an old item left: no periodic talk
    assert R.decide(only(decisions=fresh + BACKLOG + [dec("o9", "עוד ישנה", OLD)]), last, NOW + 7200, 1200) is None
    assert R.digest(only(decisions=fresh + [dec("n2", "חדשה?", FRESH)])) != R.digest(with_old)  # a fresh one does


def test_done_at_a_start_is_limited_to_the_window_and_reads_two_titles():
    many = [(n, f"משימה שהושלמה {n}", FRESH - n) for n in range(4)] + [(50 + n, f"ישנה {n}", OLD) for n in range(90)]
    s = only(done=many)
    text = R.build_script(s, {}, NAMES, "he", when=time.localtime(0))
    assert "בשעות האחרונות הושלמו ארבעה דברים." in text and "היום" not in text
    assert "משימה שהושלמה 0" in text and "משימה שהושלמה 1" in text and "משימה שהושלמה 2" not in text
    assert "ישנה" not in text and "תשעים" not in text
    # after a previous update only what is new since then is said
    last = R.remember(only(done=many[:2]), NOW - 60)
    later = R.build_script(s, last, NAMES, "he", when=time.localtime(0))
    # 2026-11-11 review: the 90 old ones are never read or counted, whatever `last` says
    assert "מאז העדכון הקודם הושלמו שני דברים." in later and "ישנה" not in later and "תשעים" not in later


def test_english_gets_the_same_logic():
    s = only(decisions=[{"id": "n1", "title": "Approve the release?", "topic": "", "age": 600}] + [
        {"id": f"o{n}", "title": f"Old decision {n}", "topic": "", "age": 3 * 86400} for n in range(4)],
             ledger=[(1, "Fresh task text", FRESH)])
    text = R.build_script(s, {}, NAMES, "en", when=time.localtime(0))
    assert "2 new things wait for you." in text and "Approve the release?" in text
    assert "Old decision" not in text and "Besides that, 4 older items still wait in the cockpit." in text
    quiet = R.build_script(only(decisions=[{"id": "o", "title": "Old one", "topic": "", "age": 3 * 86400}]), {},
                           NAMES, "en", when=time.localtime(0))
    assert "Nothing new waits for you. Besides that, 1 older item still waits in the cockpit." in quiet
    assert "Old one" not in quiet
    unsayable = only(ledger=[(1, "", FRESH)])
    assert "Nothing waits" not in R.build_script(unsayable, {}, NAMES, "en", when=time.localtime(0))


def test_the_rewriter_is_given_only_fresh_facts():
    s = only(decisions=BACKLOG + [dec("n1", "לאשר?", FRESH)], done=[(1, "ישנה", OLD), (2, "חדשה", FRESH)])
    facts = R.fresh_facts(s)
    assert [d["key"] for d in facts["decisions"]] == ["d:n1"] and facts["older_waiting_count"] == 5
    assert [d["key"] for d in facts["done"]] == ["x:orbit#2"]


# --- round 2: translation, nothing lost, old done items, no time-only updates, bad times --------------------

def eng(n, at=FRESH):
    return [(i, f"Task number {i}", at - i) for i in range(1, n + 1)]


HE = {f"Task number {i}": f"משימה מספר {i}" for i in range(1, 12)}


def fake(calls=None, table=HE):
    def translate(text):
        if calls is not None:
            calls.append(text)
        return table[text]
    return translate


def test_english_items_are_translated_only_for_the_slots_and_cached():
    s, calls, cache = only(ledger=eng(10)), [], {}
    assert radio.localize(s, {}, "script", fake(calls), cache, NOW, workers=radio.Workers()) == R.MAX_ITEMS + R.SPARE
    assert len(calls) == R.MAX_ITEMS + R.SPARE == radio.POOL_SIZE  # not all ten
    text = R.build_script(s, {}, NAMES, "he", when=time.localtime(0))
    assert "מסלול: משימה מספר 1." in text and "מסלול: משימה מספר 3." in text and "Task number" not in text
    radio.localize(only(ledger=eng(10)), {}, "script", fake(calls), cache, NOW, workers=radio.Workers())
    assert len(calls) == R.MAX_ITEMS + R.SPARE  # the second update comes from the cache


def test_a_translator_failure_a_timeout_or_a_non_hebrew_answer_leave_the_item_counted():
    def boom(text):
        raise RuntimeError("engine down")

    def slow(text):
        time.sleep(1.0)
        return "משימה"
    for translate in (boom, slow, lambda text: "still English"):
        s = only(ledger=eng(2))
        began = time.time()
        assert radio.localize(s, {}, "script", translate, {}, NOW, budget=0.1, workers=radio.Workers()) == 0
        assert time.time() - began < 0.9
        text = R.build_script(s, {}, NAMES, "he", when=time.localtime(0))
        assert "את החדשים אי אפשר להקריא בעברית." in text and "Task number" not in text
    assert radio.localize(only(ledger=eng(2)), {}, "script", None, {}, NOW) == 0  # no translator (English voice)


def test_a_failed_translation_is_retried_only_after_a_while():
    calls, cache = [], {}

    def flaky(text):
        calls.append(text)
        raise RuntimeError("x")
    for at in (NOW, NOW + 60, NOW + radio.TRANSLATE_RETRY + 1):
        radio.localize(only(ledger=eng(1)), {}, "script", flaky, cache, at, workers=radio.Workers())
    assert len(calls) == 2  # the second update (60 s later) did not ask again


def test_done_titles_that_will_be_read_are_translated_too():
    s = only(done=[(1, "Task number 1", FRESH), (2, "Task number 2", FRESH - 1), (3, "Task number 3", FRESH - 2)])
    calls = []
    radio.localize(s, {}, "script", fake(calls), {}, NOW, workers=radio.Workers())
    assert sorted(calls) == ["Task number 1", "Task number 2"]  # two titles are read
    text = R.build_script(s, {}, NAMES, "he", when=time.localtime(0))
    assert "בשעות האחרונות הושלמו שלושה דברים. מסלול: משימה מספר 1. מסלול: משימה מספר 2." in text
    news = R.build_news(s, R.remember(only(), NOW - 600), NAMES, "he")
    assert "Task number" not in news


def test_every_fresh_item_is_read_or_counted_and_then_told():
    ds = [dec(f"n{n}", f"שאלה חדשה {n}?", FRESH - n) for n in range(7)]
    s = only(decisions=ds)
    text = R.build_script(s, {}, NAMES, "he", when=time.localtime(0))
    assert "יש שבעה דברים חדשים שמחכים לך." in text and "שאלה חדשה 2" in text and "שאלה חדשה 3" not in text
    assert "ועוד ארבעה חדשים." in text  # the four that had no slot: counted, not dropped
    last = R.remember(s, NOW)
    assert R.decide(s, last, NOW + 60, 1200) is None  # all seven were read or counted: told
    assert "יש" not in R.build_script(s, last, NAMES, "he", when=time.localtime(0)).split("עדיין")[0]
    more = only(decisions=ds + [dec("x", "עוד אחת?", FRESH + 1)])
    assert R.decide(more, last, NOW + 60, 1200) == "urgent"
    assert "ועוד אחד חדש." not in R.build_script(more, last, NAMES, "he", when=time.localtime(0))


def test_done_older_than_the_window_is_never_news_or_read_whatever_last_says():
    last = R.remember(only(done=[(1, "ראשונה", FRESH)]), NOW - 600)
    stale = only(done=[(1, "ראשונה", FRESH), (2, "ישנה מאוד", NOW - R.FRESH_WINDOW - 60)])
    assert R.news(stale, last) == [] and R.decide(stale, last, NOW, 1200, news_gap=60) is None
    assert "ישנה מאוד" not in R.build_news(stale, last, NAMES, "he")
    assert "הושלם" not in R.build_script(stale, last, NAMES, "he", when=time.localtime(0))
    assert [d["key"] for d in R.fresh_facts(stale)["done"]] == ["x:orbit#1"] and R.recent_done(stale, {}) == [stale["done"][0]]
    assert R.to_read(stale, last, "news") == []


def test_time_passing_alone_never_triggers_a_periodic_update():
    s = only(decisions=[dec("n1", "לאשר?", NOW - 3600)], ledger=[(1, "פריט", NOW - 4000)],
             done=[(9, "הושלם", NOW - 3000)])
    last = R.remember(s, NOW)
    later = dict(s, at=int(NOW + 8 * 3600))  # the same items, eight hours later: all aged out of the window
    assert R.attention_keys(later) == []
    assert R.digest(later, last) == last["digest"]
    assert R.decide(later, last, NOW + 8 * 3600, 1200) is None
    later2 = dict(s, at=int(NOW + 30 * 3600))
    assert R.decide(later2, last, NOW + 30 * 3600, 1200, news_gap=60) is None
    # the next remember keeps the told keys that still wait
    assert R.remember(later, NOW + 8 * 3600, last)["attention"] == ["d:n1", "l:orbit#1"]
    # a told item that is resolved is a real change
    gone = only(ledger=[(1, "פריט", NOW - 4000)])
    gone["at"] = int(NOW + 8 * 3600)
    assert R.decide(gone, last, NOW + 8 * 3600, 1200) == "periodic"


@pytest.mark.parametrize("at", ["bad", None, float("nan"), float("inf"), True, [], NOW + 10 * 86400])
def test_a_bad_or_far_future_time_is_old_and_never_raises(at):
    broken = {"id": "z", "title": "פריט עם זמן שבור?", "topic": "", "age": 0}
    s = only(decisions=[dec("ok", "תקין?", FRESH)])
    s["decisions"].append({"key": "d:z", "text": broken["title"], "topic": "", "at": at})
    s["ledger"].append({"key": "l:q", "project": "orbit", "text": "פריט", "at": at})
    s["done"].append({"key": "x:q", "project": "orbit", "text": "גמור", "updated": at})
    assert R.attention_keys(s) == ["d:ok"]
    text = R.build_script(s, {}, NAMES, "he", when=time.localtime(0))
    assert "שבור" not in text and "גמור" not in text and "חוץ מזה מחכים בקוקפיט שני דברים ישנים יותר." in text
    assert R.decide(s, R.remember(s, NOW), NOW + 60, 1200) is None and R.fresh_facts(s)["done"] == []
    assert radio.localize(s, {}, "script", fake(table={}), {}, NOW) == 0


def test_the_session_waiting_for_an_answer_is_fresh_even_with_a_bad_time():
    s = only(sessions=[{"id": "s1", "name": "מסלול: מסך הבית", "state": "idle"}], asking=["s1"])
    s["asking"][0]["at"] = "bad"
    assert R.attention_keys(s) == ["s:s1"]
    assert "הסשן מסלול: מסך הבית מחכה לתשובה שלך." in R.build_script(s, {}, NAMES, "he", when=time.localtime(0))


def test_a_little_clock_skew_is_fresh_and_the_sort_is_safe():
    s = only(decisions=[dec("a", "בהווה?", NOW - 200)])
    s["decisions"].append({"key": "d:b", "text": "קצת בעתיד?", "topic": "", "at": NOW + 120})  # a stamp, not an age
    assert R.attention_keys(s) == ["d:a", "d:b"]
    s["decisions"].append({"key": "d:c", "text": "x", "topic": "", "at": "bad"})
    assert [i["key"] for _, i in R.waiting_split(s)[0]] == ["d:b", "d:a"]


def test_the_three_sample_updates_read_as_one_coherent_set():
    w = time.localtime(0)
    ten = only(ledger=eng(10))
    radio.localize(ten, {}, "script", fake(), {}, NOW, workers=radio.Workers())
    t10 = R.build_script(ten, {}, NAMES, "he", when=w)
    assert ("יש עשרה דברים חדשים שמחכים לך. מסלול: משימה מספר 1. מסלול: משימה מספר 2. מסלול: משימה מספר 3. "
            "ועוד שבעה חדשים.") in t10
    four = only(ledger=eng(4))
    radio.localize(four, {}, "script", fake(table={**HE, "Task number 3": "still English"}), {}, NOW,
                   workers=radio.Workers())
    t4 = R.build_script(four, {}, NAMES, "he", when=w)
    assert ("יש ארבעה דברים חדשים שמחכים לך. מסלול: משימה מספר 1. מסלול: משימה מספר 2. מסלול: משימה מספר 4. "
            "ועוד אחד חדש.") in t4
    t0 = R.build_script(only(decisions=BACKLOG), {}, NAMES, "he", when=w)
    assert "אין שום דבר חדש שמחכה לך. חוץ מזה מחכים בקוקפיט חמישה דברים ישנים יותר." in t0
    print("\n".join(["", t10, t4, t0]))


def test_progress_speaks_only_after_the_interval():
    s = snap()
    last = R.remember(s, 1_000_000.0)
    busier = snap(state={"sessions": [{"id": "s1", "name": "א", "state": "working"},
                                      {"id": "s4", "name": "ב", "state": "working"}]}, asking=[])
    last["attention"] = R.attention_keys(busier)
    assert R.decide(busier, last, 1_000_000.0 + 600, 1200) is None
    assert R.decide(busier, last, 1_000_000.0 + 1200, 1200) == "periodic"


def test_said_items_are_not_repeated_one_by_one():
    s = snap()
    last = R.remember(s, 1_000_000.0)
    again = R.build_script(s, last, NAMES, "he", when=time.localtime(0))
    assert "הושלם" not in again  # the done item was already announced
    assert "החדשים" not in again and "חדש:" not in again and "למזג" not in again  # nothing new: no item repeated
    more = snap(state={"decisions": [{"id": "a1", "title": "x"}, {"id": "b2", "title": "לפרסם את הפרק?", "age": 5}]})
    text = R.build_script(more, last, NAMES, "he", when=time.localtime(0))
    assert "חדש: החלטה בקוקפיט, לפרסם את הפרק?" in text and "x" not in text


def test_ledger_reads_only_open_owner_items_and_recent_done(tmp_path):
    now = time.time()
    (tmp_path / "orbit.json").write_text(json.dumps({"items": [
        {"id": 1, "text": "a", "status": "open", "wait": "owner", "updated": now},
        {"id": 2, "text": "b", "status": "open", "wait": "team", "updated": now},
        {"id": 3, "text": "c", "status": "done", "wait": "team", "updated": now - 10},
        {"id": 4, "text": "d", "status": "done", "wait": "team", "updated": now - 3 * 86400}]}))
    (tmp_path / "broken.json").write_text("{")
    waiting, done, team = R.ledger_items(tmp_path, now)
    assert [w["key"] for w in waiting] == ["orbit#1"] and [d["key"] for d in done] == ["orbit#3"] and team == 1


def test_team_quota_reads_weekly_for_claude_and_codex_and_keeps_short_windows_for_others():
    rows = R.team_quota(["printf", "Codex: 5h 12%% · week 79%% (x)\\nClaude: by the app\\n"
                                   "AGY Gemini: 5h 0%% · week 19%%\\nClaude work: week 55%%\\n"])
    assert rows == [{"label": "Codex", "pct": 79, "window": "week"},
                    {"label": "AGY Gemini", "pct": 0, "window": "5h"},
                    {"label": "Claude work", "pct": 55, "window": "week"}]
    # a Codex line with only a "5h" number (the old team-quota label) is never read as a 5-hour quota
    assert R.team_quota(["printf", "Codex: 5h 81%% (x)\\n"]) == []


# --- the voice and its daily cap -----------------------------------------------------------------

def _settings(**kw):
    s = dict(radio.DEFAULTS, lang="he", say_voice="Carmit")
    s.update(kw)
    return s


def test_daily_cap_switches_to_the_free_voice(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(V, "vertex", lambda text, *a, **k: calls.append(text) or (b"\x01\x00" * V.RATE * 10, 10.0))
    monkeypatch.setattr(V, "system_voice", lambda text, voice: b"\x02\x00" * V.RATE)
    usage = V.Usage(tmp_path / "usage.json")
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER)
    s = _settings(tts_daily_minutes=0.5)  # 30 seconds a day
    pcm, route = V.speak("א" * 100, cfg, s, tmp_path / "cache", usage)  # ~7.5 s estimated, 10 s billed
    assert route == "vertex" and usage.today() == pytest.approx(10.0)
    pcm, route = V.speak("א" * 100, cfg, s, tmp_path / "cache", usage)
    assert route == "cache" and usage.today() == pytest.approx(10.0)  # cached: not billed again
    V.speak("ב" * 150, cfg, s, tmp_path / "cache", usage)  # 11 s estimated: still fits
    pcm, route = V.speak("ג" * 200, cfg, s, tmp_path / "cache", usage)  # 15 s more would pass 30 s
    assert route == "say" and len(calls) == 2


def test_cap_zero_never_calls_the_paid_voice(tmp_path, monkeypatch):
    monkeypatch.setattr(V, "vertex", lambda *a, **k: pytest.fail("paid voice called"))
    monkeypatch.setattr(V, "system_voice", lambda text, voice: b"\x00\x00" * 10)
    pcm, route = V.speak("שלום", Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER), _settings(tts_daily_minutes=0),
                         tmp_path / "c", V.Usage(tmp_path / "u.json"))
    assert route == "say"


def test_usage_resets_on_a_new_day(tmp_path):
    u = V.Usage(tmp_path / "u.json")
    u.add(100, now=time.mktime((2026, 11, 10, 12, 0, 0, 0, 0, -1)))
    assert u.today(now=time.mktime((2026, 11, 10, 18, 0, 0, 0, 0, -1))) == 100
    assert u.today(now=time.mktime((2026, 11, 11, 8, 0, 0, 0, 0, -1))) == 0


# --- the mixer -------------------------------------------------------------------------------------

def test_mixer_fades_the_bed_out_plays_the_voice_and_fades_back():
    bed = (b"\x10\x27" * radio.RATE)  # constant 10000
    m = radio.Mixer(bed)
    assert m.next() == b"\x10\x27" * radio.FRAME
    voice = b"\x01\x00" * (radio.FRAME * 3)
    m.add(voice)
    frames = [m.next() for _ in range(int(radio.Mixer.FADE_OUT * radio.RATE / radio.FRAME) + 5)]
    assert b"\x01\x00" * radio.FRAME in frames  # the voice played after the fade
    first_voice = frames.index(b"\x01\x00" * radio.FRAME)
    assert all(f != b"\x10\x27" * radio.FRAME for f in frames[:first_voice])  # the bed was fading
    for _ in range(int((radio.Mixer.GAP + radio.Mixer.FADE_IN) * radio.RATE / radio.FRAME) + 5):
        last = m.next()
    assert last == b"\x10\x27" * radio.FRAME and m.pending() == 0


# --- the panel switch ------------------------------------------------------------------------------

@pytest.fixture
def radio_server(monkeypatch):
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he", radio={"every_minutes": 20})
    srv = panel.PanelServer(("127.0.0.1", 0), cfg)
    switched = []
    srv.radio_switch = switched.append
    monkeypatch.setattr(radio, "job_pid", lambda: None)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv, switched
    srv.shutdown()
    srv.server_close()


def test_radio_switch_needs_a_fresh_owner_login(radio_server):
    srv, switched = radio_server
    body = b'{"on": true}'
    assert call(srv, path="/api/radio", init=None, body=body)[0] == 403
    assert call(srv, path="/api/radio", init=signed(user_id=OWNER + 1), body=body)[0] == 403
    stale = signed(auth_date=int(time.time()) - panel.DECIDE_MAX_AGE - 5)
    assert call(srv, path="/api/radio", init=stale, body=body)[0] == 403
    assert switched == []
    code, _, data = call(srv, path="/api/radio", init=signed(), body=body)
    assert code == 202 and json.loads(data) == {"ok": True, "pending": True}
    assert call(srv, path="/api/radio", init=signed(), body=b'{"on": false}')[0] == 202
    assert switched == ["start", "stop"]


@pytest.mark.parametrize("body", [b'{"on": 1}', b'{"on": true, "x": 1}', b"[]", b"nope", b"{}"])
def test_radio_switch_rejects_bad_bodies(radio_server, body):
    srv, switched = radio_server
    assert call(srv, path="/api/radio", init=signed(), body=body)[0] == 400 and switched == []


def test_radio_switch_absent_without_config(server):
    assert call(server, path="/api/radio", init=signed(), body=b'{"on": true}')[0] == 404
    assert "radio" not in json.loads(call(server, init=signed())[2])


def test_state_shows_the_radio_when_configured(radio_server):
    srv, _ = radio_server
    assert json.loads(call(srv, init=signed())[2])["radio"] == {"on": False, "pending": "", "failed": False,
                                                                "op_at": 0, "tts_minutes_today": 0.0,
                                                                "tts_daily_minutes": 25}


def test_a_switch_stays_pending_until_it_really_finished(radio_server, monkeypatch):
    srv, _ = radio_server
    monkeypatch.setattr(radio.subprocess, "Popen", lambda *a, **k: None)
    radio.spawn("start")  # what the panel's switch runs
    state = json.loads(call(srv, init=signed())[2])["radio"]
    assert state["pending"] == "start" and state["op_at"] > 0  # every refresh still says "in progress"
    monkeypatch.setattr(radio, "start", lambda cfg: (_ for _ in ()).throw(radio.RadioError("radio_call_busy")))
    with pytest.raises(radio.RadioError):
        radio.switch(srv.cfg, "start")  # the detached `ctb radio start` finishing with an error
    state = json.loads(call(srv, init=signed())[2])["radio"]
    assert state["pending"] == "" and state["failed"] is True and state["op_at"] > 0
    monkeypatch.setattr(radio, "stop", lambda cfg: "radio_stopped")
    assert radio.switch(srv.cfg, "stop") == "radio_stopped"
    assert radio.operation() == dict(radio.operation(), pending="", failed=False)


def test_the_panel_keeps_its_pending_switch_outside_the_render():
    """The page re-renders every 15 s; a pending flag declared inside the render was reset each time."""
    js = panel.PAGE[panel.PAGE.index('<script nonce="__NONCE__">'):]

    def depth(at):
        # braces inside quoted strings are text, not blocks (the Cockpit apps bridge checks a body starts with "{")
        code = re.sub(r'"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'', '""', js[:at])
        return code.count("{") - code.count("}")
    assert "var radioWait" not in js
    assert depth(js.index("var radioOp")) == depth(js.index("function load(")) <= depth(js.index("function radioCard("))
    radio_card = js[js.index("function radioCard("):js.index("function modelCard(")]
    assert "var radioOp" not in radio_card
    assert "r.pending" in js


@pytest.fixture
def server():
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he")
    srv = panel.PanelServer(("127.0.0.1", 0), cfg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


# --- secrets ---------------------------------------------------------------------------------------

def test_encoder_command_never_carries_the_stream(tmp_path):
    for enc in ("h264_videotoolbox", "libx264"):
        args = radio.encoder_args(enc, 3)
        joined = " ".join(args)
        assert "rtmp" not in joined and SECRET_KEY not in joined and args[-1] == "pipe:1"


def test_account_helper_gets_commands_not_secrets(monkeypatch):
    seen = {}

    def runner(argv, input, capture_output, timeout):
        seen["argv"], seen["input"] = argv, json.loads(input)
        return subprocess.CompletedProcess(argv, 0, stdout=b'{"ok": true, "active": false}\n')
    s = dict(radio.DEFAULTS, chat=-1001234, title="רדיו", telethon_python="/py",
             account_api_command=["kc", "api"], account_session_command=["kc", "session"])
    assert radio.call_helper(s, "status", runner=runner) == {"ok": True, "active": False}
    assert seen["argv"][:2] == ["/py", "-I"] and seen["argv"][2].endswith("radio_call.py")
    assert seen["input"]["allowed"] == [-1001234] and seen["input"]["chat"] == -1001234
    with pytest.raises(radio.RadioError):
        radio.call_helper(dict(s, chat=12345), "status", runner=runner)  # never a private chat


def test_errors_never_echo_the_url_or_key():
    for url in ("http://x/s", "rtmps://", "rtmps://host"):
        with pytest.raises(radio_rtmp.RtmpError) as e:
            radio_rtmp.Publisher(url + SECRET_KEY, SECRET_KEY)
        assert SECRET_KEY not in str(e.value) and "host" not in str(e.value)
    with pytest.raises(radio_rtmp.RtmpError) as e:
        radio_rtmp.Publisher(SECRET_URL, "bad key " + SECRET_KEY)
    assert SECRET_KEY not in str(e.value)
    assert radio_rtmp.split_url("rtmps://dc4-1.rtmp.t.me/s/")[:4] == ("dc4-1.rtmp.t.me", 443, True, "s")


def test_amf_round_trip():
    data = (radio_rtmp.amf_encode("_result") + radio_rtmp.amf_encode(1.0) + radio_rtmp.amf_encode(None)
            + radio_rtmp.amf_encode({"code": "NetStream.Publish.Start", "ok": True}))
    assert radio_rtmp.amf_decode_all(data) == ["_result", 1.0, None, {"code": "NetStream.Publish.Start", "ok": True}]


def test_a_missing_key_is_logged_without_details(monkeypatch, tmp_path):
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, chat_id=-1001234,
                 radio={"rtmp_url_command": ["false"], "rtmp_key_command": ["echo", SECRET_KEY]})
    monkeypatch.setattr(radio, "make_bed", lambda kind, db: bytes(4))
    monkeypatch.setattr(radio, "_end_call", lambda s: True)
    monkeypatch.setattr(radio, "_bootout_self", lambda: None)
    assert radio.Station(cfg).run() == 0
    text = (radio.folder() / "radio.log").read_text()
    assert "stream key is not available" in text and SECRET_KEY not in text
    assert json.loads((radio.folder() / "status.json").read_text())["gave_up"] is True


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_live_pipeline_publishes_without_the_key_on_any_argv(tmp_path):
    port = 19000 + int(time.time() * 1000) % 900
    listener = subprocess.Popen(["ffmpeg", "-nostdin", "-v", "error", "-listen", "1", "-timeout", "20", "-i",
                                 f"rtmp://127.0.0.1:{port}/s/{SECRET_KEY}", "-f", "null", "-"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(1.0)
        card = tmp_path / "card.png"
        from ctb import radio_card
        radio_card.plain(card)
        assert card.exists()
        mixer = radio.Mixer(bytes(radio.RATE * 2))
        mixer.add(b"\x00\x10" * radio.RATE)
        pids = []
        pipe = radio.Pipeline(f"rtmp://127.0.0.1:{port}/s/", SECRET_KEY, card, "libx264", mixer, pids.append)
        result = {}
        th = threading.Thread(target=lambda: result.setdefault("why", pipe.run()), daemon=True)
        th.start()
        deadline = time.time() + 8
        while not pids and time.time() < deadline:
            time.sleep(0.1)
        assert pids
        ps = subprocess.run(["ps", "-ww", "-p", str(pids[0]), "-o", "command="], capture_output=True, text=True).stdout
        assert "ffmpeg" in ps and SECRET_KEY not in ps and "rtmp" not in ps
        time.sleep(2)
        assert pipe.mixer.pending() == 0 or pipe.proc.poll() is None  # audio is flowing
        pipe.quit.set()
        th.join(timeout=15)
        assert result.get("why") == ""  # stopped on request, not by a failure
        assert pipe.proc.poll() is not None  # and the encoder is gone
    finally:
        listener.kill()


# --- /radio in the home group ----------------------------------------------------------------------

def _command(tmp_path, text, chat=OWNER, chat_type="private"):
    from conftest import FakeBot
    from ctb import inbox as I
    from ctb.topics import TopicMap
    c = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he", ui_language="he",
               radio={"every_minutes": 20})
    bot = FakeBot()
    topics = TopicMap(tmp_path / "state" / "topics.json", bot, OWNER)
    m = {"message_id": 9, "date": 2_000_000_000, "text": text, "chat": {"id": chat, "type": chat_type},
         "from": {"id": OWNER}}
    r = I.handle({"update_id": 3, "message": m}, c, bot, topics, I.Inbox(tmp_path / "state" / "inbox"), None, 1000, None)
    return r, bot


def test_radio_command_reports_status_and_switches_in_a_thread(tmp_path, monkeypatch):
    monkeypatch.setattr(radio, "job_pid", lambda: None)
    r, bot = _command(tmp_path, "/radio")
    assert r == "radio" and "כבוי" in bot.methods("sendMessage")[-1]["text"]
    done = threading.Event()
    monkeypatch.setattr(radio, "start", lambda cfg: done.set() or "radio_started")
    r, bot = _command(tmp_path, "/radio on")
    assert r == "radio" and done.wait(5)
    time.sleep(0.2)
    texts = [p["text"] for p in bot.methods("sendMessage")]
    assert any("מפעיל" in x for x in texts) and any("פועל" in x for x in texts)


def test_radio_command_only_in_the_home_group(tmp_path, monkeypatch):
    monkeypatch.setattr(radio, "start", lambda cfg: pytest.fail("started from another chat"))
    r, bot = _command(tmp_path, "/radio on", chat=-100555, chat_type="supergroup")
    assert r != "radio"


def test_job_path_keeps_configured_and_shell_folders(monkeypatch, tmp_path):
    shell_bin = tmp_path / "shell-bin"
    shell_bin.mkdir()
    monkeypatch.setenv("PATH", f"{shell_bin}:relative:/usr/bin")
    path = radio.job_path(["/opt/cloud/bin"]).split(":")
    assert path[0] == "/opt/cloud/bin" and str(shell_bin) in path and "relative" not in path
    assert path.count("/usr/bin") == 1


# --- the paid voice's cap is hard ---------------------------------------------------------------------------

def test_cap_holds_even_when_every_request_bills_its_whole_reservation(tmp_path, monkeypatch):
    """Worst case: each answer is as long as the request allowed. The day's total still never passes the cap."""
    asked = []

    def worst(text, cfg, model, voice, style, max_seconds=0.0, **k):
        asked.append(max_seconds)
        return b"\x01\x00" * int(V.RATE * max_seconds), max_seconds
    monkeypatch.setattr(V, "vertex", worst)
    monkeypatch.setattr(V, "system_voice", lambda text, voice: b"\x02\x00" * 10)
    usage = V.Usage(tmp_path / "u.json")
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER)
    for i in range(20):
        V.speak(f"{i} " + "א" * 120, cfg, _settings(tts_daily_minutes=1), tmp_path / "c", usage)
    assert asked and all(m > 0 for m in asked)
    assert usage.today() <= 60.0 + 1e-6


def test_an_uncertain_request_keeps_its_whole_reservation(tmp_path, monkeypatch):
    monkeypatch.setattr(V, "vertex", lambda *a, **k: (b"", None))  # sent, but the answer never came
    monkeypatch.setattr(V, "system_voice", lambda text, voice: b"\x02\x00" * 10)
    usage = V.Usage(tmp_path / "u.json")
    pcm, route = V.speak("א" * 100, Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER), _settings(tts_daily_minutes=5),
                         tmp_path / "c", usage)
    assert route == "say" and usage.today() == pytest.approx(100 * V.SECONDS_PER_CHAR * V.RESERVE_FACTOR
                                                              + V.RESERVE_EXTRA)


def _fake_vertex_answer(monkeypatch, answer):
    sent = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a):
            return json.dumps(answer).encode()

    def urlopen(req, timeout=0):
        sent.append(json.loads(req.data))
        return Resp()
    monkeypatch.setattr(V.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(V, "to_pcm", lambda raw, args: b"\x00\x00" * (len(raw) // 2) * 2)
    return sent


def _vertex_cfg():
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, gemini_vertex_project="p", gemini_token_command=["echo", "t"])


def test_vertex_asks_for_no_more_audio_than_reserved_and_bills_by_tokens(monkeypatch):
    import base64
    audio = base64.b64encode(b"\x00\x00" * 24000 * 2).decode()  # 2 s at 24 kHz
    sent = _fake_vertex_answer(monkeypatch, {
        "candidates": [{"finishReason": "STOP", "content": {"parts": [
            {"inlineData": {"mimeType": "audio/L16;codec=pcm;rate=24000", "data": audio}}]}}],
        "usageMetadata": {"candidatesTokenCount": 100}})  # 100 tokens = 4 s: more than the audio, so 4 s count
    pcm, billed = V.vertex("שלום", _vertex_cfg(), "gemini-2.5-flash-tts", "Sadaltager", "", max_seconds=12)
    assert sent[0]["generationConfig"]["maxOutputTokens"] == 12 * V.AUDIO_TOKENS_PER_SECOND
    assert pcm and billed == pytest.approx(4.0)


def test_vertex_cut_at_the_ceiling_is_billed_but_not_aired(monkeypatch):
    import base64
    _fake_vertex_answer(monkeypatch, {
        "candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [
            {"inlineData": {"mimeType": "audio/L16;codec=pcm;rate=24000", "data": base64.b64encode(b"\0" * 48000).decode()}}]}}],
        "usageMetadata": {"candidatesTokenCount": 25}})
    pcm, billed = V.vertex("שלום", _vertex_cfg(), "gemini-2.5-flash-tts", "Sadaltager", "", max_seconds=1)
    assert pcm == b"" and billed == pytest.approx(1.0)


def test_vertex_broken_answer_counts_its_tokens(monkeypatch):
    _fake_vertex_answer(monkeypatch, {"candidates": [], "usageMetadata": {"candidatesTokenCount": 50}})
    assert V.vertex("שלום", _vertex_cfg(), "m", "v", "", max_seconds=5) == (b"", pytest.approx(2.0))


# --- why the paid voice was not used, and the token kept between clips (2026-11-10: `say` at 13:23) ---------

def test_a_slow_token_command_is_named_in_the_log_reason(tmp_path, monkeypatch):
    import subprocess as sp
    V._tokens.clear()

    def slow(*a, **k):
        raise sp.TimeoutExpired(a[0], k.get("timeout"))
    monkeypatch.setattr(V.subprocess, "run", slow)
    monkeypatch.setattr(V, "system_voice", lambda text, voice: b"\x02\x00" * 10)
    pcm, route = V.speak("שלום", _vertex_cfg(), _settings(tts_daily_minutes=5), tmp_path / "c", V.Usage(tmp_path / "u"))
    assert route == "say" and "timed out after 90 s" in V.last_reason


def test_the_token_is_fetched_once_and_reused(monkeypatch):
    V._tokens.clear()
    calls = []

    class R:
        returncode, stdout, stderr = 0, "ya29.secret-token\n", ""
    monkeypatch.setattr(V.subprocess, "run", lambda *a, **k: calls.append(a) or R())
    assert V.access_token(_vertex_cfg()) == "ya29.secret-token"
    assert V.access_token(_vertex_cfg()) == "ya29.secret-token" and len(calls) == 1
    V.forget_token(_vertex_cfg())
    V.access_token(_vertex_cfg())
    assert len(calls) == 2


def test_a_refused_request_logs_the_status_never_the_token(monkeypatch):
    import io
    V._tokens.clear()
    V._tokens[("echo", "t")] = ("ya29.abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGH", 1e18)

    def refuse(req, timeout=0):
        raise V.urllib.error.HTTPError(req.full_url, 403, "Forbidden", {},
                                       io.BytesIO(b'{"error": {"message": "Permission denied on resource"}}'))
    monkeypatch.setattr(V.urllib.request, "urlopen", refuse)
    why = []
    assert V.vertex("שלום", _vertex_cfg(), "m", "v", "", max_seconds=5, why=why) == (b"", 0.0)
    assert why == ["Vertex answered HTTP 403: Permission denied on resource"]
    assert ("echo", "t") not in V._tokens  # a refused token is fetched again next time
    V._tokens.clear()


# --- short news between the full updates (2026-11-10: one update at 13:23, then silence) -----------------

def _working(*states):
    return {"sessions": [{"id": f"s{n}", "name": "תיקון הגשר", "state": st} for n, st in enumerate(states)]}


def test_a_session_that_finished_is_news_after_the_short_gap():
    before = snap(state=_working("working", "idle"), asking=[])
    last = R.remember(before, 1_000_000.0)
    after = snap(state=_working("idle", "idle"), asking=[])
    assert R.decide(after, last, 1_000_000.0 + 60, 1200, news_gap=180) is None  # not right after the last one
    assert R.decide(after, last, 1_000_000.0 + 200, 1200, news_gap=180) == "news"
    assert R.decide(after, last, 1_000_000.0 + 200, 1200, news_gap=None) is None  # paid minutes run low
    text = R.build_news(after, last, NAMES, "he")
    assert "הסשן תיקון הגשר סיים לעבוד וממתין" in text and len(text) < 200
    assert R.decide(after, R.remember(after, 1_000_200.0), 1_000_000.0 + 600, 1200, news_gap=180) is None


def test_a_background_agent_end_and_a_quota_band_are_news():
    base = snap(asking=[])
    last = R.remember(base, 1_000_000.0)
    later = R.snapshot(_working("idle"), [], ([], [], 0), [{"label": "Codex", "pct": 86}], 1_000_300.0,
                       agents_done=[{"key": "a:x1", "session": "s0", "at": 1_000_100.0},
                                    {"key": "a:old", "session": "s0", "at": 999_000.0}])
    kinds = sorted(k for k, _ in R.news(later, last))
    assert kinds == ["agent", "quota"]  # the agent that ended before the last update is not news
    text = R.build_news(later, last, NAMES, "he")
    assert "סוכן רקע אחד סיים" in text and "קודקס" in text and "שמונים ושישה" in text


def test_news_gap_stretches_as_the_paid_minutes_run_low(tmp_path, monkeypatch):
    st = radio.Station.__new__(radio.Station)
    st.s = {"tts_daily_minutes": 25, "news_minutes": 3}
    st.usage = V.Usage(tmp_path / "u.json")
    assert st.news_gap() == 180
    st.usage.add(25 * 60 * 0.7)
    assert st.news_gap() == 540
    st.usage.add(25 * 60 * 0.2)
    assert st.news_gap() == 540
    st.usage.add(25 * 60 * 0.1)  # used up: the free voice goes on at the base pace (2026-11-10)
    assert st.news_gap() == 180


def test_agents_done_reads_the_agents_topic_records(tmp_path):
    from ctb.config import state_dir
    root = state_dir() / "agents-topics"
    root.mkdir(parents=True)
    (root / "abcdef1234.json").write_text(json.dumps({"finished": {"ag1": 1_000_000.0, "ag0": 1.0}}))
    assert radio.agents_done(1_000_100.0) == [{"key": "a:ag1", "session": "abcdef12", "at": 1_000_000.0}]


# --- the intro chime and the card's captions ---------------------------------------------------------

def test_the_intro_is_soft_short_and_always_the_same():
    import array
    chime = V.intro()
    assert chime is V.intro()  # the same every time: the owner learns it
    samples = array.array("h", chime)
    assert 1.6 <= len(samples) / V.RATE <= 2.2
    assert max(abs(x) for x in samples) <= int(32767 * V.INTRO_PEAK) + 1  # about -16 dBFS, under the voice
    assert max(abs(x) for x in samples[: V.RATE // 50]) < 200  # it rises from silence, never a sudden start
    assert not any(samples[-int(V.INTRO_GAP * V.RATE):])  # a short pause before the voice


def _station(tmp_path, monkeypatch, pcm=b"\x01\x00" * radio.RATE):
    st = radio.Station.__new__(radio.Station)
    st.cfg, st.s, st.dir = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER), {"lang": "he"}, tmp_path
    st.mixer, st.usage, st.cache = radio.Mixer(bytes(4)), V.Usage(tmp_path / "u.json"), tmp_path / "c"
    st.status, st.snap = {}, None
    st.draw_lock, st.caption_lock, st.captions, st.tag, st.headline = (threading.Lock(), threading.Lock(), {}, 0,
                                                                        ("", ""))
    monkeypatch.setattr(radio, "folder", lambda: tmp_path)
    monkeypatch.setattr(V, "speak", lambda *a, **k: (pcm, "vertex"))
    return st


def test_every_update_starts_with_the_intro_and_registers_its_captions(tmp_path, monkeypatch):
    st = _station(tmp_path, monkeypatch)
    text = "רדיו הצוות, השעה אחת וחצי. הסשן תיקון הגשר סיים לעבוד וממתין. זה הכול לעכשיו."
    assert st.speak(text)
    clip, tag = st.mixer.clips[0]
    assert clip.startswith(V.intro()) and len(clip) == len(V.intro()) + radio.RATE * 2
    intro_len, voice_len, chunks = st.captions[tag]
    assert intro_len == len(V.intro()) and chunks and st.headline[0].startswith("הסשן תיקון הגשר סיים")


def test_captions_follow_the_audio():
    chunks = R.caption_chunks("משפט ראשון קצר. " + "משפט שני ארוך יותר " * 8 + ". סוף.", limit=60)
    assert len(chunks) >= 3
    assert R.chunk_at(chunks, 0.0) == 0 and R.chunk_at(chunks, 0.999) == len(chunks) - 1
    m = radio.Mixer(bytes(4))
    m.gain = 0.0
    m.add(b"\x01\x00" * 4000, 7)
    assert m.progress() == (None, 0)
    m.next(1000)
    assert m.progress() == (7, 2000)
    assert all(len(line) <= 70 for line in R.card_lines("מילה " * 60, 3)) and len(R.card_lines("מילה " * 60, 3)) == 3


def test_a_model_that_refuses_the_audio_ceiling_is_asked_again_without_it(monkeypatch):
    """2026-11-10: gemini-3.8-flash-tts answers 400 "invalid argument" to maxOutputTokens, so every update
    went out in `say`. The refused request is free; the same clip is asked again without the ceiling."""
    import base64
    import io
    V._tokens.clear()
    V.NO_CEILING.discard("gemini-3.8-flash-tts")
    sent = []
    audio = base64.b64encode(b"\x00\x00" * 24000).decode()

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a):
            return json.dumps({"candidates": [{"finishReason": "STOP", "content": {"parts": [
                {"inlineData": {"mimeType": "audio/L16;rate=24000", "data": audio}}]}}],
                "usageMetadata": {"candidatesTokenCount": 25}}).encode()

    def urlopen(req, timeout=0):
        body = json.loads(req.data)
        sent.append(body)
        if "maxOutputTokens" in body["generationConfig"]:
            raise V.urllib.error.HTTPError(req.full_url, 400, "Bad", {},
                                           io.BytesIO(b'{"error": {"message": "Request contains an invalid argument."}}'))
        return Resp()
    monkeypatch.setattr(V.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(V, "to_pcm", lambda raw, args: b"\x00\x00" * (len(raw) // 2) * 2)
    why = []
    pcm, billed = V.vertex("שלום", _vertex_cfg(), "gemini-3.8-flash-tts", "Sadaltager", "", max_seconds=8, why=why)
    assert pcm and billed == pytest.approx(1.0) and why == [] and len(sent) == 2
    V.vertex("שלום", _vertex_cfg(), "gemini-3.8-flash-tts", "Sadaltager", "", max_seconds=8)
    assert len(sent) == 3  # remembered: no refused request first any more
    V.NO_CEILING.discard("gemini-3.8-flash-tts")
    V._tokens.clear()


def test_done_items_with_english_titles_are_counted_never_a_bare_greeting(monkeypatch):
    items = [("done", {"text": "Promote main 905a900f8 to production", "project": "bridge"}),
             ("done", {"text": "Fix PR #151", "project": "bridge"})]
    monkeypatch.setattr(R, "news", lambda snap, last: items)  # review of #151
    he = R.build_news({}, {}, NAMES, "he")
    assert he != "עדכון קצר." and "הושלמו ברשימת המשימות" in he
    assert "2 task list items done." in R.build_news({}, {}, NAMES, "en") or "Done:" in R.build_news({}, {}, NAMES, "en")
    monkeypatch.setattr(R, "news", lambda snap, last: items[:1])
    assert "הושלם ברשימת המשימות" in R.build_news({}, {}, NAMES, "he")


# --- round 4: bad ages from the source, the rest of the done items counted aloud ----------------------------

import math  # noqa: E402


@pytest.mark.parametrize("age", ["bad", -5, math.nan, math.inf, True, [], {}])
def test_snapshot_survives_bad_ages_from_the_decisions_and_reminders_source(age):
    state = {"decisions": [{"id": "ok", "title": "תקין?", "age": 60}, {"id": "bad", "title": "גיל שבור?", "age": age}],
             "reminders": [{"topic": "t", "text": "תזכורת שבורה", "age": age}], "sessions": [], "quota": []}
    s = R.snapshot(state, [], ([], [], 0), [], NOW)
    keys = R.attention_keys(s)
    assert keys == ["d:ok"]  # a bad age means old
    text = R.build_script(s, {}, NAMES, "he", when=time.localtime(0))
    assert "שבורה" not in text and "שבור?" not in text


def test_a_negative_age_is_old_and_a_missing_one_is_now():
    s = R.snapshot({"decisions": [{"id": "n", "title": "x", "age": -10}, {"id": "m", "title": "y"}],
                    "sessions": [], "quota": []}, [], ([], [], 0), [], NOW)
    assert R.attention_keys(s) == ["d:m"]


def test_news_counts_the_done_items_it_did_not_read_even_when_some_were_read():
    before = only()
    last = R.remember(before, NOW - 600)
    after = only(done=[(1, "הסרטון עלה", FRESH), (2, "Shipped", FRESH - 1), (3, "Merged", FRESH - 2)])
    text = R.build_news(after, last, NAMES, "he")
    assert "הושלם: מסלול: הסרטון עלה." in text and "ועוד שניים הושלמו." in text
    one = only(done=[(1, "הסרטון עלה", FRESH), (2, "Shipped", FRESH - 1)])
    assert "ועוד אחד הושלם." in R.build_news(one, last, NAMES, "he")
    allread = only(done=[(1, "הסרטון עלה", FRESH), (2, "עוד משימה", FRESH - 1)])
    assert "ועוד" not in R.build_news(allread, last, NAMES, "he")
    assert "And 1 more done." in R.build_news(after, last, NAMES, "en")  # English titles can all be read


@pytest.mark.parametrize("big", [10 ** 400, -10 ** 400, 10 ** 400 + 0.5 if False else 10 ** 1000])
def test_a_huge_integer_age_or_time_never_raises(big):
    state = {"decisions": [{"id": "ok", "title": "תקין?", "age": 60}, {"id": "big", "title": "ענק?", "age": big}],
             "reminders": [{"topic": "t", "text": "ענקית", "age": big}], "sessions": [], "quota": []}
    s = R.snapshot(state, [], ([], [], 0), [], NOW)
    assert R.attention_keys(s) == ["d:ok"]
    for field in ("at",):
        item = {"key": "d:x", "text": "x", "topic": "", field: big}
        assert not R.is_fresh("decisions", item, {"at": NOW}) and R._age(item, NOW) is None
        s["decisions"].append(item)
    s["done"].append({"key": "x:q", "project": "orbit", "text": "גמור", "updated": big})
    s["at"] = big  # even the snapshot's own clock
    assert R.waiting_split(s)[1] >= 0 and R.recent_done(s) == [] and R._num(big) is None
    R.build_script(s, {}, NAMES, "he", when=time.localtime(0))
