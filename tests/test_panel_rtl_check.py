"""In Hebrew the hero ring's arc runs from the right, but the check mark inside it is never mirrored."""
import re

from ctb import panel_page


def test_only_the_ring_arc_is_mirrored_in_rtl():
    page = panel_page.PAGE
    assert ':root[dir="rtl"] .ring>svg{transform:scaleX(-1)}' in page
    assert not re.search(r'\.ring svg\{[^}]*transform', page)  # a descendant rule would also flip the check icon
