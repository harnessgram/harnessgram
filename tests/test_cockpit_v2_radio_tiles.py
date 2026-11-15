"""The radio tools row places every tile it can hold: each tile gets its own column and its panel spans the row."""
import re
from pathlib import Path

CSS = (Path(__file__).resolve().parents[1] / "src" / "ctb" / "cockpit_v2.css").read_text(encoding="utf-8")
SCREENS = (Path(__file__).resolve().parents[1] / "src" / "ctb" / "cockpit_v2_screens.js").read_text(encoding="utf-8")


def test_three_tiles_get_three_columns_and_full_width_panels():
    # room, announce and podcast: the podcast tile is the third one.
    assert '"v2-podcast"' in SCREENS
    assert re.search(r"\.v2-radio-tiles:has\(>\.v2-tile:nth-child\(3\)\)\{grid-template-columns:repeat\(3,", CSS)
    for n in (1, 2, 3):
        assert re.search(rf"\.v2-tile:nth-child\({n}\)>\.compact-toggle\{{grid-column:{n}\}}", CSS), n
    assert ".v2-tile>.compact-reveal{grid-column:1 / -1" in CSS
    assert ".v2-tile:nth-child(3)>.compact-reveal{grid-row:4}" in CSS
