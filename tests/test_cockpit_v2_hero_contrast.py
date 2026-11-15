"""The project page header sits on a busy picture: the scrim, the text shadow and the inks must keep it legible."""
import re
from pathlib import Path

import pytest

CSS = (Path(__file__).resolve().parents[1] / "src" / "ctb" / "cockpit_v2.css").read_text(encoding="utf-8")
MIN_CONTRAST = 4.5

# The three places that define the theme tokens: light, dark by system preference, dark chosen in the Cockpit.
THEMES = {
    "light": re.compile(r"^:root\.cockpit-v2\{(--v2-phead-ink:[^}]*)\}", re.M),
    "dark-system": re.compile(r'prefers-color-scheme:dark\)\{:root\.cockpit-v2:not\(\[data-theme="light"\]\)\{(--v2-phead-ink:[^}]*)\}'),
    "dark-chosen": re.compile(r'^:root\.cockpit-v2\[data-theme="dark"\]\{(--v2-phead-ink:[^}]*)\}', re.M),
}


def token(block, name):
    return re.search(r"--%s:([^;}]+)" % name, block).group(1).strip()


def luminance(rgb):
    def lin(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def hexrgb(value):
    assert re.fullmatch(r"#[0-9a-f]{6}", value), value
    return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))


def scrim_rule(ltr):
    selector = r"\.cockpit-v2:dir\(ltr\) \.v2-phead-text::before" if ltr else r"\.cockpit-v2 \.v2-phead-text::before"
    return re.search(selector + r"\{([^}]*)\}", CSS).group(1)


def stops(rule):
    """[(position, alpha)] of the scrim gradient; position is a percentage or 'box-end' (`calc(100% - var(--v2-phead-fade))`)."""
    found = re.findall(r"rgb\(var\(--v2-phead-scrim\) / ([.\d]+)\) (\d+%|calc\(100% - var\(--v2-phead-fade\)\))", rule)
    assert len(found) == 3
    return [("box-end" if pos.startswith("calc") else int(pos[:-1]), float(alpha)) for alpha, pos in found]


def solid_alpha(rule):
    """The weakest alpha anywhere inside the text box: the scrim is the text box itself, solid to its end edge."""
    points = stops(rule)
    assert [p for p, _ in points] == [0, "box-end", 100]
    return min(alpha for _, alpha in points[:2])


@pytest.mark.parametrize("theme", sorted(THEMES))
@pytest.mark.parametrize("ink", ["phead-ink", "phead-ink-soft"])
def test_text_keeps_4_5_to_1_on_the_worst_picture(theme, ink):
    block = THEMES[theme].search(CSS).group(1)
    scrim = tuple(int(x) for x in token(block, "v2-phead-scrim").split())
    text = hexrgb(token(block, "v2-" + ink))
    for ltr in (False, True):
        alpha = solid_alpha(scrim_rule(ltr))
        for picture in ((0, 0, 0), (255, 255, 255)):  # whatever the picture, it is somewhere between black and white
            under = tuple(alpha * s + (1 - alpha) * p for s, p in zip(scrim, picture))
            assert contrast(text, under) >= MIN_CONTRAST, (theme, ink, ltr, picture)


def test_scrim_belongs_to_the_text_box_and_covers_all_of_it():
    """Tied to the text container, not to a fixed share of the header: wide pages grow the box and the scrim with it."""
    rtl, ltr = scrim_rule(False), scrim_rule(True)
    assert "270deg" in rtl and "90deg" in ltr  # the text side is the start side: the right edge in RTL, the left in LTR
    for rule in (rtl, ltr):
        assert solid_alpha(rule) >= 0.9
        assert stops(rule)[-1] == (100, 0.0)  # the picture shows through past the box
        assert not re.search(r"\b(left|right)\b", rule)  # logical CSS only
    # The fade strip is exactly the overhang past the box's end edge, so the solid part ends where the text does.
    geometry = scrim_rule(False)
    assert "inset-inline-end:calc(-1 * var(--v2-phead-fade))" in geometry
    assert "inset-inline-start:calc(-1 * var(--v2-pad))" in geometry and "inset-block:calc(-1 * var(--v2-pad))" in geometry
    assert re.search(r"\.cockpit-v2 \.v2-phead-text\{position:relative;", CSS)  # the pseudo-element's containing block
    assert "::before{content:\"\";position:absolute;inset:0" not in CSS.split(".v2-phead-bg")[1][:400]  # no header-wide fixed scrim left


def test_text_has_a_shadow_and_the_hero_uses_only_the_theme_inks():
    assert re.search(r"\.cockpit-v2 \.v2-phead-text\{[^}]*text-shadow:var\(--v2-phead-glow\)", CSS)
    assert re.search(r"--v2-phead-glow:[^;}]*rgb\(var\(--v2-phead-scrim\)", CSS)
    hero = re.search(r"\.cockpit-v2 \.v2-phead\{([^}]*)\}", CSS).group(1)
    assert "color:var(--v2-phead-ink)" in hero
    assert "color:var(--v2-phead-ink-soft)" in re.search(r"\.cockpit-v2 \.v2-phead \.v2-updated\{([^}]*)\}", CSS).group(1)
