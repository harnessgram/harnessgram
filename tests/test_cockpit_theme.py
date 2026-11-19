"""Cockpit appearance is viewer-local and testable without Telegram or a network."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
NATIVE = ROOT / "src/ctb/cockpit_theme.js"
PANEL = ROOT / "src/ctb/panel_page.py"
CSS = ROOT / "src/ctb/cockpit_v2.css"
BASELINE = ROOT / "tests/cockpit_colour_literals.json"


def test_light_and_dark_define_the_screen_palette():
    panel = PANEL.read_text()
    light = panel[panel.index(":root{"):panel.index(":root[dir=")]
    dark = panel[panel.index(':root[data-theme="dark"]{'):panel.index("*{box-sizing")]
    required = {"bg", "card", "edge", "field", "text", "hint", "ok", "warn", "line", "soft", "shadow", "hero-hot", "hero-calm"}
    for token in required:
        assert f"--{token}:" in light
        assert f"--{token}:" in dark
    v2 = CSS.read_text()
    assert ":root.cockpit-v2 {" in v2 and ':root.cockpit-v2[data-theme="dark"]{' in v2


def test_colour_literal_baseline_rejects_new_screen_colours():
    files = [PANEL, *sorted((ROOT / "src/ctb").glob("cockpit_v2*.css")), *sorted((ROOT / "src/ctb").glob("cockpit_v2*.js")), NATIVE]
    pattern = re.compile(r"(?i)(?:#[0-9a-f]{3,8}\b|rgba?\([^)]*\)|hsla?\([^)]*\))")
    found = {str(p.relative_to(ROOT)): sorted(set(pattern.findall(p.read_text()))) for p in files}
    assert found == json.loads(BASELINE.read_text()), "Use a named theme token; update the baseline only when intentionally changing the palette"


def test_toggle_persists_and_auto_follows_telegram_theme():
    script = NATIVE.read_text() + r'''
+const assert=require("assert"), saved=new Map(), events={};
+const root={attrs:{},setAttribute(k,v){this.attrs[k]=v}};
+window.document={documentElement:root};
+global.localStorage={getItem:k=>saved.get(k)||null,setItem:(k,v)=>saved.set(k,v)};
+const tg={colorScheme:"dark",onEvent:(n,fn)=>events[n]=fn};
+let ctl=window.CockpitTheme.create({telegram:tg,native:false});ctl.init();
+assert.equal(root.attrs["data-theme"],"dark");
+ctl.setPreference("light");assert.equal(saved.get(window.CockpitTheme.key),"light");assert.equal(root.attrs["data-theme"],"light");
+tg.colorScheme="dark";events.themeChanged();assert.equal(root.attrs["data-theme"],"light");
+ctl.setPreference("auto");tg.colorScheme="light";events.themeChanged();assert.equal(root.attrs["data-theme"],"light");
+tg.colorScheme="dark";events.themeChanged();assert.equal(root.attrs["data-theme"],"dark");
+saved.set(window.CockpitTheme.key,"light");tg.CloudStorage={getItem(){throw new Error("no cloud")},setItem(){throw new Error("no cloud")}};
+ctl=window.CockpitTheme.create({telegram:tg,native:true});ctl.init();assert.equal(root.attrs["data-theme"],"light");ctl.setPreference("dark");assert.equal(saved.get(window.CockpitTheme.key),"dark");
+'''.replace("\n+", "\n")
    subprocess.run(["node", "-e", "global.window={matchMedia:()=>({matches:false,addEventListener(){}})};" + script], check=True)
