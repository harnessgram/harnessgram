import json
from conftest import GOOD_TOKEN
from ctb.config import Config, state_dir
from ctb import progress as P


def cfg():
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=123, translate=True, target_language="he",
                  engines=["gemini", "google", "mlx", "nllb"])


def test_present_tense_heading_translation_and_retry(monkeypatch, tmp_path):
    c = cfg()
    prompts = []
    responses = ["يقرأ الملف", "קורא את הקובץ"]  # first has Arabic, second is Hebrew present-tense

    def fake_ask(prompt, config):
        prompts.append(prompt)
        return responses.pop(0) if responses else ""

    monkeypatch.setattr("ctb.translate.ask", fake_ask)

    # 1. Calls ask, detects Arabic, retries once, gets Hebrew
    result = P.heading_he("Read the file", c)
    assert result == "קורא את הקובץ"
    assert len(prompts) == 2
    assert "present tense" in prompts[0]
    assert "קורא" in prompts[0]

    # 2. Result is cached in heading-cache.json
    cache_file = state_dir() / P.HEADINGS_CACHE_FILE
    assert cache_file.exists()
    cached_data = json.loads(cache_file.read_text(encoding="utf-8"))
    assert cached_data.get("Read the file") == "קורא את הקובץ"

    # 3. Cache hit does not call translate.ask again
    monkeypatch.setattr("ctb.translate.ask", lambda p, cf: (_ for _ in ()).throw(RuntimeError("should not call")))
    assert P.heading_he("Read the file", c) == "קורא את הקובץ"

    # 4. Fallback to existing translation if retry still has Arabic letters
    prompts.clear()
    monkeypatch.setattr("ctb.translate.ask", lambda p, cf: "يقرأ دائما")
    monkeypatch.setattr("ctb.translate.translate_text", lambda words, cf: "קריאת הקובץ")

    fallback_result = P.heading_he("Read another file", c)
    assert fallback_result == "קריאת הקובץ"

    # 5. translated_heading integrates heading_he for Hebrew
    row = {"l": "📖 Read the file"}
    out = P.translated_heading(row, c)
    assert "קורא את הקובץ" in out["l"]


def test_present_tense_heading_local_only_never_calls_external(monkeypatch):
    called = []
    called_translate = []
    monkeypatch.setattr("ctb.translate.ask", lambda p, cf: called.append(p) or "קורא")
    monkeypatch.setattr("ctb.translate.translate_text", lambda words, cf: called_translate.append(words) or "מקומי")

    # translate=False -> returns original heading, never calls ask or translate_text
    c_off = Config(bot_token=GOOD_TOKEN, owner_chat_id=123, translate=False, target_language="he")
    assert P.heading_he("Read local only", c_off) == "Read local only"
    assert len(called) == 0
    assert len(called_translate) == 0

    # translate=True and engines are local-only -> uses translate_text, never calls external ask
    c_local = Config(bot_token=GOOD_TOKEN, owner_chat_id=123, translate=True, target_language="he",
                     engines=["mlx", "nllb"])
    assert P.heading_he("Read local only 2", c_local) == "מקומי"
    assert len(called) == 0
    assert len(called_translate) == 1


def test_heading_he_offline_never_calls_translate_text_or_external(monkeypatch):
    """translate=False returns original heading without translate_text or ask calls."""
    def fail(*args, **kwargs):
        raise AssertionError("translate_text or ask called when translate=False")

    monkeypatch.setattr("ctb.translate.ask", fail)
    monkeypatch.setattr("ctb.translate.translate_text", fail)

    c_off = Config(bot_token=GOOD_TOKEN, owner_chat_id=123, translate=False, target_language="he")
    assert P.heading_he("Edit foo.py", c_off) == "Edit foo.py"


def test_heading_he_respects_engine_priority_gemini_first_vs_other_and_local(monkeypatch, tmp_path):
    """Guard test for review finding 3:
    - Translation off: returns original heading.
    - FIRST engine is gemini (and external allowed): uses present-tense Gemini prompt.
    - FIRST engine is not gemini (e.g. google): uses translate_text path.
    - Local-only engines (mlx, nllb): uses translate_text path, never calls ask."""
    called_ask = []
    called_translate = []
    monkeypatch.setattr("ctb.translate.ask", lambda p, cf: called_ask.append(p) or "קורא את הקובץ")
    monkeypatch.setattr("ctb.translate.translate_text", lambda words, cf: called_translate.append(words) or "קריאת הקובץ")

    # 1. Translation off -> original heading, no engine called
    c_off = Config(bot_token=GOOD_TOKEN, owner_chat_id=123, translate=False, target_language="he",
                   engines=["gemini", "google"])
    assert P.heading_he("Read the file off", c_off) == "Read the file off"
    assert len(called_ask) == 0
    assert len(called_translate) == 0

    # 2. First engine is gemini and external allowed -> calls ask (Gemini prompt)
    c_gemini_first = Config(bot_token=GOOD_TOKEN, owner_chat_id=123, translate=True, target_language="he",
                            engines=["gemini", "google"])
    res1 = P.heading_he("Read the file gemini", c_gemini_first)
    assert res1 == "קורא את הקובץ"
    assert len(called_ask) == 1
    assert len(called_translate) == 0

    # 3. First engine is google, gemini is second -> does NOT call ask, uses translate_text
    c_google_first = Config(bot_token=GOOD_TOKEN, owner_chat_id=123, translate=True, target_language="he",
                            engines=["google", "gemini"])
    res2 = P.heading_he("Read the file google", c_google_first)
    assert res2 == "קריאת הקובץ"
    assert len(called_ask) == 1  # not incremented!
    assert len(called_translate) == 1

    # 4. Local-only engines (mlx, nllb) -> does NOT call ask, uses translate_text
    c_local = Config(bot_token=GOOD_TOKEN, owner_chat_id=123, translate=True, target_language="he",
                     engines=["mlx", "nllb"])
    res3 = P.heading_he("Read the file local", c_local)
    assert res3 == "קריאת הקובץ"
    assert len(called_ask) == 1  # not incremented!
    assert len(called_translate) == 2


def test_google_engine_alone_does_not_call_gemini(monkeypatch):
    """A 'google' engine choice alone must not enable Gemini (translate.ask)."""
    called_ask = []
    called_translate = []
    monkeypatch.setattr("ctb.translate.ask", lambda p, cf: called_ask.append(p) or "קורא")
    monkeypatch.setattr("ctb.translate.translate_text", lambda words, cf: called_translate.append(words) or "קריאת הקובץ")

    # translate=True with only google engine (no gemini)
    c_google = Config(bot_token=GOOD_TOKEN, owner_chat_id=123, translate=True, target_language="he",
                      engines=["google"])
    res = P.heading_he("Read the test file", c_google)
    assert res == "קריאת הקובץ"
    assert len(called_ask) == 0
    assert len(called_translate) == 1
