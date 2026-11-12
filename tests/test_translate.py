from ctb import translate as T
from ctb.config import Config


def fake(text):
    """Translates each blank-line separated paragraph, keeping the paragraph structure."""
    return "\n\n".join("T:" + p for p in text.split("\n\n"))


def test_mixed_language_one_call_keeps_target_paragraphs_in_place():
    reply = ("First English paragraph.\n\nזה כבר בעברית ולכן נשאר.\n\nSecond English paragraph.\n\n"
             "Third English paragraph.")
    calls = []
    out = T.translate_reply(reply, lambda t: calls.append(t) or fake(t), "he")
    assert calls == ["First English paragraph.\n\nSecond English paragraph.\n\nThird English paragraph."]  # ONE call
    assert out.split("\n\n") == ["T:First English paragraph.", "זה כבר בעברית ולכן נשאר.",
                                  "T:Second English paragraph.", "T:Third English paragraph."]


def test_mismatched_paragraph_count_puts_untouched_paragraphs_first():
    reply = "One English.\n\nזה כבר בעברית ולכן נשאר.\n\nTwo English."
    out = T.translate_reply(reply, lambda t: "everything merged into a single paragraph", "he")
    assert out == "זה כבר בעברית ולכן נשאר.\n\neverything merged into a single paragraph"


def test_all_english_is_a_single_request():
    calls = []
    T.translate_reply("One.\n\nTwo.\n\nThree.", lambda t: calls.append(t) or "x", "he")
    assert calls == ["One.\n\nTwo.\n\nThree."]


def test_code_blocks_never_reach_the_translator_even_with_blank_lines_inside():
    reply = "Run this:\n\n```bash\necho one\n\necho two\n```\n\nThen wait."
    seen = []
    out = T.translate_reply(reply, lambda t: seen.append(t) or fake(t), "he")
    assert seen == ["Run this:\n\nThen wait."]
    assert out == "T:Run this:\n\n```bash\necho one\n\necho two\n```\n\nT:Then wait."


def test_paragraph_of_only_code_or_numbers_is_left_alone():
    seen = []
    T.translate_reply("`a_b_c`\n\n12345", lambda t: seen.append(t) or t, "he")
    assert seen == []


def test_latin_script_target_translates_every_prose_paragraph():
    seen = []
    T.translate_reply("Hola amigo.\n\nHello there.", lambda t: seen.append(t) or t, "es")
    assert seen == ["Hola amigo.\n\nHello there."]


def test_shield_roundtrip():
    text = "See `my_func()` at /usr/local/bin/tool and https://example.com/x?y=1 now"
    shielded, kept = T.shield(text)
    assert "my_func" not in shielded and "/usr/local" not in shielded and "example.com" not in shielded
    assert T.unshield(shielded, kept) == text


def test_google_fallback_across_endpoints_and_shielding():
    calls = []

    def fetch(url, text, timeout):
        calls.append(url)
        if len(calls) == 1:
            raise OSError("429")
        return "שלום [0]"

    out = T.google("Hello `x_y`", "he", fetch)
    assert out == "שלום `x_y`"
    assert len(calls) == 2 and all("tl=he" in u for u in calls)


def test_chain_order_and_failure(monkeypatch):
    cfg = Config(engines=["google", "mlx"], translate_cache=False)
    monkeypatch.setattr(T, "google", lambda text, target: "")
    monkeypatch.setattr(T, "mlx", lambda text, cfg, fast: "תרגום מהמודל המקומי")
    assert T.translate_text("hi", cfg) == "תרגום מהמודל המקומי"
    monkeypatch.setattr(T, "mlx", lambda text, cfg, fast: "")
    try:
        T.translate_text("hi", cfg)
        assert False, "expected TranslateError"
    except T.TranslateError:
        pass


ARABIC = "• تعلم: educational games، quizzes، lesson plans"


def test_an_arabic_answer_for_hebrew_falls_through_to_the_next_engine(monkeypatch):
    cfg = Config(engines=["gemini", "nllb"], translate_cache=False, target_language="he")
    monkeypatch.setattr(T, "gemini", lambda text, c: ARABIC)
    monkeypatch.setattr(T, "nllb", lambda text, c: "• לימוד: משחקים חינוכיים")
    assert T._engines("Learning: educational games, quizzes, lesson plans", cfg, False) == "• לימוד: משחקים חינוכיים"


def test_a_cached_arabic_answer_is_not_served(tmp_path, monkeypatch):
    cfg = Config(engines=["nllb"], translate_cache=True, target_language="he")
    monkeypatch.setattr(T, "state_dir", lambda: tmp_path)
    path = T._cache_path("Learning", cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"text": "%s", "engine": "gemini"}' % ARABIC, encoding="utf-8")
    monkeypatch.setattr(T, "nllb", lambda text, c: "לימוד")
    assert "تعلم" not in T.translate_text("Learning", cfg)


def test_arabic_is_fine_when_arabic_is_the_target():
    assert T.wrong_script(ARABIC, "ar") is False and T.wrong_script(ARABIC, "he") is True


def test_gemini_request_names_the_user_role(monkeypatch):
    # Vertex answers 400 "Please use a valid role" to a turn without one, so every Hebrew reply fell back.
    import json as _json
    import subprocess as _sp
    from ctb import translate as T
    from ctb.config import Config
    sent = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"candidates":[{"content":{"parts":[{"text":"ok"}]}}]}'

    monkeypatch.setattr(T.subprocess, "run", lambda *a, **k: _sp.CompletedProcess(a, 0, "tok", ""))
    monkeypatch.setattr(T.urllib.request, "urlopen", lambda req, timeout=None: sent.append(req) or Resp())
    cfg = Config(bot_token="123456:" + "A" * 35, owner_chat_id=7, gemini_vertex_project="p",
                 gemini_token_command=["tok"])
    assert T._gemini_call("hi", cfg) == "ok"
    assert _json.loads(sent[0].data)["contents"][0]["role"] == "user"


def test_gemini_thinking_level_is_sent_only_when_set(monkeypatch):
    """A thinking model left at its default thinks past the seven-second budget and returns nothing."""
    import dataclasses, io, json as _json, subprocess
    from ctb import translate as T
    from ctb.config import Config
    sent = []

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_open(req, timeout=None):
        sent.append(_json.loads(req.data))
        return Resp(_json.dumps({"candidates": [{"content": {"parts": [{"text": "שלום"}]}}]}).encode())

    monkeypatch.setattr(T.urllib.request, "urlopen", fake_open)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "key\n", ""))
    cfg = Config(gemini_key_command=["key"], gemini_model="gemini-3.8-flash")
    assert T.gemini("hello", cfg) == "שלום"
    assert "thinkingConfig" not in sent[-1]["generationConfig"]
    assert T.gemini("hello", dataclasses.replace(cfg, gemini_thinking="low")) == "שלום"
    assert sent[-1]["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "low"}
    # the instructions go in the system instruction; the user turn carries only the text
    assert "masculine singular" in sent[-1]["systemInstruction"]["parts"][0]["text"]
    assert sent[-1]["contents"][0]["parts"][0]["text"] == "<text>\nhello\n</text>"
