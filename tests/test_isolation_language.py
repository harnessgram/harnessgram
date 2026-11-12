"""Guard: the UI language is process-wide state (config.load() sets it). A test that switches it must not leak
into the next test: 24 order-dependent failures came from one earlier test leaving English behind."""
from ctb import i18n


def test_a_test_may_switch_the_language():
    i18n.set_lang("en")
    assert i18n.get_lang() == "en"


def test_the_next_test_starts_on_the_default_language_again():
    assert i18n.get_lang() == "he"
