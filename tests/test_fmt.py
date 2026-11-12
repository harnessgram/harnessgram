from ctb import fmt


def test_markdown_to_telegram_html_basics():
    md = "# Title\n\nSome **bold** and `code` and a [link](https://example.com/a?b=1&c=2).\n\n- one\n* two"
    out = fmt.to_html(md)
    assert "<b>Title</b>" in out
    assert "<b>bold</b>" in out
    assert "<code>code</code>" in out
    assert '<a href="https://example.com/a?b=1&amp;c=2">link</a>' in out
    assert "• one" in out and "• two" in out


def test_html_is_escaped_and_code_spans_stay_literal():
    out = fmt.to_html("a < b && c > d, `**not bold** <i>`")
    assert "a &lt; b &amp;&amp; c &gt; d" in out
    assert "<code>**not bold** &lt;i&gt;</code>" in out
    assert "<b>" not in out


def test_fenced_block_becomes_pre_and_is_not_formatted():
    md = "before **x**\n\n```python\nprint('<hi>')  # **no**\n```\n\nafter"
    out = fmt.to_html(md)
    assert "<pre>print('&lt;hi&gt;')  # **no**</pre>" in out
    assert "before <b>x</b>" in out
    assert "python" not in out


def test_markdown_table_goes_into_pre():
    out = fmt.to_html("| a | b |\n|---|---|\n| 1 | 2 |\n\ntext")
    assert out.startswith("<pre>| a | b |")
    assert "text" in out


def test_nikud_removed_only_for_hebrew():
    pointed = "שָׁלוֹם"
    assert fmt.to_html(pointed, "he") == "שלום"
    assert fmt.to_html(pointed, "en") == pointed


def test_summary_extraction_variants():
    body, s = fmt.split_summary("Details here.\nMore.\n\nSummary: It worked.")
    assert (body, s) == ("Details here.\nMore.", "It worked.")
    assert fmt.split_summary("x\n**Summary:** It worked.")[1] == "It worked."
    assert fmt.split_summary("x\n**Summary**: It worked.")[1] == "It worked."
    assert fmt.split_summary("x\n**Summary: It worked.**")[1] == "It worked."
    assert fmt.split_summary("x\nsummary: **Bold** start")[1] == "**Bold** start"
    assert fmt.split_summary("x\n> Summary: quoted")[1] == "quoted"


def test_summary_absent_or_empty_is_none():
    assert fmt.split_summary("No label here.\nSummary of changes follows")[1] is None
    assert fmt.split_summary("text\nSummary:")[1] is None


def test_last_summary_line_wins():
    _, s = fmt.split_summary("Summary: first\nmiddle\nSummary: last")
    assert s == "last"


def test_join_summary_makes_it_its_own_final_paragraph():
    assert fmt.join_summary("Body line.\nSummary: Done.") == "Body line.\n\nDone."
    assert fmt.join_summary("Summary: Only.") == "Only."
    assert fmt.join_summary("No label.") == "No label."


def test_layout_pins_summary_first_and_folds_rest():
    msgs = fmt.layout("First para.\n\nSecond para.\n\nThe summary.", "en", pin_last=True)
    assert len(msgs) == 1
    assert msgs[0].startswith("\U0001F4CC The summary.")
    assert msgs[0].index("The summary.") < msgs[0].index("First para.")


def test_layout_without_pin_keeps_order():
    msgs = fmt.layout("One.\n\nTwo.", "en", pin_last=False)
    assert msgs == ["One.\n\nTwo."]


def test_chunks_respect_limit_and_never_split_a_fence():
    code = "```\n" + "\n".join(f"line {i}" for i in range(900)) + "\n```"
    parts = fmt.chunks("intro\n\n" + code)
    assert len(parts) > 1
    assert all(len(p) <= 3500 for p in parts)
    for p in parts:
        assert p.count("```") % 2 == 0  # every piece has balanced fences


def test_very_long_single_line_is_cut():
    parts = fmt.chunks("x" * 10000)
    assert all(len(p) <= 3500 for p in parts) and "".join(parts) == "x" * 10000
