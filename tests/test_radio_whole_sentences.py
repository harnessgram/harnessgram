"""Radio lines end where the voice can stop: whole sentences first (the owner heard lines cut mid-sentence)."""
from ctb import radio_script as R, radio_talk as T


def test_short_keeps_whole_sentences():
    text = "הבנייה הסתיימה בהצלחה. הבדיקות עברו. נשאר רק לחכות לסקירה של הצוות השני שעוד לא התחילה"
    out = R.short(text, 40)
    assert out == "הבנייה הסתיימה בהצלחה. הבדיקות עברו."


def test_short_cuts_a_long_sentence_at_a_clause_then_a_word():
    clause = "הבנייה הסתיימה בלי תקלות, והבדיקות רצות עכשיו על כל המחשבים של הצוות"
    assert R.short(clause, 40) == "הבנייה הסתיימה בלי תקלות…"
    words = "מילה " * 30
    out = R.short(words, 40)
    assert len(out) <= 40 and out.endswith("…") and not out.endswith(" …") and "מיל…" not in out


def test_short_leaves_a_short_line_alone():
    assert R.short("קצר.", 90) == "קצר."


def test_spoken_reply_drops_whole_sentences_to_fit(monkeypatch):
    monkeypatch.setattr(T, "MAX_CHARS", 60)
    monkeypatch.setattr(T, "MAX_SENTENCES", 5)
    reply = "המשימה הושלמה. הגרסה החדשה מותקנת. כל הבדיקות עברו בלי שום תקלה בכלל היום."
    line = T.spoken_reply("", reply, lambda s: s)
    assert line.endswith(": המשימה הושלמה. הגרסה החדשה מותקנת.")


def test_a_decimal_point_at_the_cut_is_not_a_sentence_end():
    from ctb import radio_script as R
    assert R.short("הגרסה 3.5 מוכנה לעבודה נוספת", 8) != "הגרסה 3."
    assert not R.short("הגרסה 3.5 מוכנה לעבודה נוספת", 8).endswith("3.")


def test_a_tiny_limit_never_overflows():
    from ctb import radio_script as R
    for n in (0, 1):
        assert len(R.short("שלום עולם", n)) <= n


def test_the_voice_keeps_a_short_whole_sentence_rather_than_cutting_the_next():
    from ctb import radio_script as R
    assert R.say_short("משפט קצר. עוד משפט ארוך מאוד מאוד מאוד", 25) == "משפט קצר."


def test_the_voice_keeps_one_long_sentence_whole_up_to_twice_the_limit():
    from ctb import radio_script as R
    line = "זה משפט ארוך ללא סימני פיסוק והמשך חשוב מאוד."
    assert R.say_short(line, 30) == line
    assert R.say_short(line + " ועוד אחד.", 30) == line


def test_the_spoken_paths_use_the_voice_cut():
    import inspect
    from ctb import radio, radio_script as R, radio_talk
    assert "say_short(text, 110)" in inspect.getsource(R._item_line)
    assert "say_short(out, 160)" in inspect.getsource(radio.translate_one)
    assert "say_short(text, MAX_CHARS)" in inspect.getsource(radio_talk)


def test_an_initial_is_not_a_sentence_end():
    from ctb import radio_script as R
    line = "הבדיקה בוצעה על ידי ד. כהן והכל עבר בהצלחה. המשך ארוך מאוד מאוד מאוד"
    assert not R.say_short(line, 30).endswith("ד.")
    assert R.say_short(line, 50) == "הבדיקה בוצעה על ידי ד. כהן והכל עבר בהצלחה."
