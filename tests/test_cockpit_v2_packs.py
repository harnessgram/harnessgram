"""Pack Settings page keeps the Cockpit's RTL and narrow-layout contracts."""
import re
from pathlib import Path

from ctb import panel_page

SRC = Path(__file__).resolve().parents[1] / "src" / "ctb"


def test_hebrew_copy_and_page_direction_are_inherited():
    he = panel_page.V2_STRINGS["he"]["packs"]
    assert he["title"] == "חבילות" and he["unknown"] == "לא ידוע"
    js = (SRC / "cockpit_v2_packs.js").read_text()
    css = (SRC / "cockpit_v2_packs.css").read_text()
    assert "direction=" not in js and "dir=" not in js
    assert "direction:inherit" in css


def test_pack_styles_use_logical_sizing_and_prevent_phone_overflow():
    css = (SRC / "cockpit_v2_packs.css").read_text()
    assert not re.search(r"(?:margin|padding|border)-(?:left|right)|(?:^|[;{])\s*(?:left|right):", css)
    assert "min-inline-size:0" in css and "overflow-wrap:anywhere" in css
    assert "@media(max-width:420px)" in css
    assert 'app.appendChild(vpEntry())' in (SRC / "cockpit_v2_screens.js").read_text()


def test_pack_assets_are_embedded_in_the_nonce_page():
    assert "cockpit_v2_packs.css" in Path(panel_page.__file__).read_text()
    assert "cockpit_v2_packs.js" in Path(panel_page.__file__).read_text()
    assert 'data-v2-section","packs"' in panel_page.PAGE


def test_items_link_to_existing_controls_and_locked_items_show_lock():
    js = (SRC / "cockpit_v2_packs.js").read_text()
    assert 'vkGo({kind:t.kind,vault:!!t.locked,history:false,item:t.id})' in js
    assert 'vaGo(true)' in js and 'data-va-key=' in js
    assert 'data-vp-locked' in js and 'icon("vklock")' in js
    assert 'String(p.count||0)+" "+vpT("items")' in js
    assert 'action:"undo"' not in js


def test_pack_switch_writes_only_after_a_good_preview_and_a_confirm():
    js = (SRC / "cockpit_v2_packs.js").read_text()
    assert js.count('action:"set"') == 1 and '["s","et"]' not in js
    body = js[js.index("function vpSwitch("):js.index("function vpMove(")]
    preview, write = body.index('action:"preview"'), body.index('action:"set"')
    checked, confirm = body.index("if(!r.ok)throw r.status", preview), body.index('vpT("confirm")')
    assert preview < checked < confirm < write and '"/api/packs-edit"' in body[confirm:write + 40]
