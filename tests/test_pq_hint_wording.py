"""The permission hint must say a plain "no" counts with one open request, never that nothing counts."""
from ctb.i18n import t


def test_reply_needed_hint_names_the_no_answer_in_both_languages():
    he = t("pq_reply_needed", lang="he")
    en = t("pq_reply_needed", lang="en")
    # The old Hebrew read "does not count when only one request is open", the opposite of the English.
    assert "לא נחשב" not in he
    assert "תשובת לא" in he and "בקשה אחת" in he
    assert "plain no" in en and "one request is open" in en
