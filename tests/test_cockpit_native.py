"""The cockpit_native switch without a browser: one wrapper for every new native call, off by default, the config
key, the strings and the documentation. Never loads the real config."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from ctb import config, panel, panel_page

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "ctb"
NATIVE_JS = (SRC / "cockpit_native.js").read_text(encoding="utf-8")
NATIVE_CSS = (SRC / "cockpit_native.css").read_text(encoding="utf-8")
NEW_APIS = ("MainButton", "SecondaryButton", "showPopup", "showConfirm", "CloudStorage", "requestFullscreen",
            "themeParams", "BiometricManager", "setBottomBarColor(\"bottom_bar_bg_color\")")


def page_js(native=False):
    page = panel_page.render_page("he", "n", native=native).decode("utf-8")
    return re.search(r'<script nonce="n">(.*?)</script>', page, re.S)[1]


def functions(source):
    """name -> body of each top-level function in a script."""
    parts = re.split(r"\n(?=function )", "\n" + source)
    return {re.match(r"function (\w+)", p).group(1): p for p in parts if p.startswith("function ")}


def test_the_switch_is_off_by_default_and_only_true_turns_it_on():
    assert config.Config().cockpit_native is False
    assert config.from_dict({"cockpit_native": True}).cockpit_native is True
    for value in (False, "true", 1, "yes", None, {}):
        assert config.from_dict({"cockpit_native": value}).cockpit_native is False
    assert "var NATIVE=false===true" in page_js()
    assert "var NATIVE=true===true" in page_js(native=True)
    assert "var NATIVE=false===true" in panel_page.render_page("en", "n", native="true").decode()
    assert "__NATIVE__" not in panel_page.render_page("en", "n").decode()


def test_the_panel_serves_the_switch_from_its_config():
    assert "var NATIVE=true===true" in panel.render_page("he", "n", native=True).decode()
    assert "var NATIVE=false===true" in panel.render_page("he", "n").decode()
    source = (SRC / "panel.py").read_text(encoding="utf-8")
    assert 'getattr(getattr(self.server, "cfg", None), "cockpit_native", False) is True' in source


def test_new_native_apis_are_used_only_inside_the_native_module():
    rest = page_js().replace(NATIVE_JS.split("\n", 1)[1], "")
    for name in NEW_APIS:
        assert name not in rest, name
    # The app frames and other screens never reach for the new APIs either.
    for path in SRC.glob("*.js"):
        if path.name != "cockpit_native.js":
            text = path.read_text(encoding="utf-8")
            assert not [n for n in NEW_APIS if n in text], path.name


def test_every_native_call_goes_through_the_one_wrapper():
    funcs = functions(NATIVE_JS.split("\n", 1)[1])
    # Only natOn and natCall touch Telegram.WebApp (`wa`); everything else receives it from natCall.
    touching = sorted(name for name, body in funcs.items() if re.search(r"\bwa\b", body))
    assert touching == ["natCall", "natOn"]
    assert "natOn(v)" in funcs["natCall"] and "NATIVE===true" in funcs["natOn"]
    # Haptics stay in the page's one helper; the native catch-all calls it, never HapticFeedback itself.
    assert "HapticFeedback" not in NATIVE_JS and "haptic(" in funcs["natFeel"]
    assert page_js().count("HapticFeedback") == 1
    # Every caller elsewhere checks the wrapper exists, so a sliced-out function in a harness still runs.
    for name in ("natConfirm", "natPref"):
        for path in [*SRC.glob("cockpit_v2*.js"), SRC / "cockpit_mac_tools.js"]:
            text = path.read_text(encoding="utf-8")
            assert text.count(name + "(") == text.count('typeof %s==="function"&&%s(' % (name, name)), (path.name, name)


def test_every_page_changing_rule_needs_a_class_only_the_native_module_sets():
    rules = [r for r in re.findall(r"([^{}]+)\{[^{}]*\}", re.sub(r"/\*.*?\*/", "", NATIVE_CSS, flags=re.S))]
    for selector in rules:
        selector = selector.strip()
        if selector == ":root":  # one variable, unused until a class below applies
            continue
        for part in selector.split(","):
            assert re.match(r":root\.tg-(native|back|bottom|full)\b", part.strip()), part
    for cls in ("tg-native", "tg-back", "tg-bottom", "tg-full"):
        assert cls in NATIVE_JS
        assert cls not in page_js().replace(NATIVE_JS.split("\n", 1)[1], ""), cls


def test_destructive_actions_keep_the_existing_guarded_paths():
    panel_source = (SRC / "panel_page.py").read_text(encoding="utf-8")
    v2 = (SRC / "cockpit_v2.js").read_text(encoding="utf-8")
    mac = (SRC / "cockpit_mac_tools.js").read_text(encoding="utf-8")
    assert "natConfirm" not in panel_source + v2 + mac
    assert "function sureBtn" in panel_source and "setTimeout(rest,4000)" in panel_source
    assert "until=Date.now()+4000" in panel_source
    assert "v2CleanupArmed=true" in v2 and 'arm("clean")' in mac
    assert 'v2CleanupPost({action:"close",ids:ids,confirm:ids.length}' in v2
    assert "BiometricManager" not in page_js(native=True)
    assert "natConfirm" not in (SRC / "sensitive_buttons.py").read_text(encoding="utf-8")


def test_native_module_retains_no_confirmation_callback_that_can_go_stale():
    assert "natConfirm" not in NATIVE_JS
    assert "showPopup" not in NATIVE_JS and "showConfirm" not in NATIVE_JS


def test_strings_exist_in_both_languages():
    strings = json.loads((SRC / "cockpit_v2_strings.json").read_text(encoding="utf-8"))
    assert strings["he"]["native"].keys() == strings["en"]["native"].keys() == {"confirmTitle", "ask", "cleanupAsk"}
    for lang in ("he", "en"):
        n = strings[lang]["native"]
        assert "{action}" in n["ask"] and "{n}" in n["cleanupAsk"] and len(n["confirmTitle"]) <= 64
    assert re.search(r"[֐-׿]", strings["he"]["native"]["ask"])


def test_documented_in_the_readme_changelog_and_cockpit_doc():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert re.search(r"^\| `cockpit_native` \| `false` \|", readme, re.M)
    doc = (ROOT / "docs" / "cockpit-v2.md").read_text(encoding="utf-8")
    for feature in ("themeParams", "BackButton", "MainButton", "SecondaryButton", "showPopup", "CloudStorage",
                    "BiometricManager", "requestFullscreen", "shareMessage", "HapticFeedback"):
        assert feature in doc, feature
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    unreleased = changelog.split("## [Unreleased]", 1)[1].split("\n## [", 1)[0]
    assert "cockpit_native" in unreleased


NODE = shutil.which("node")


@pytest.mark.skipif(not NODE, reason="node is not installed")
@pytest.mark.parametrize("native,has_wa,expect", [(False, True, False), (True, False, False), (True, True, True)])
def test_the_wrapper_does_nothing_when_off_or_outside_telegram(native, has_wa, expect):
    funcs = functions(NATIVE_JS.split("\n", 1)[1])
    src = ("var NATIVE=%s,wa=%s,called=0;function ver(){return true;}\n" % (
        "true" if native else "false", "{x:1}" if has_wa else "undefined")
           + funcs["natOn"] + funcs["natCall"]
           + "var r=natCall('6.2',function(w){called++;return true;});"
           + "var t=natCall('6.2',function(w){throw new Error('x');});"
           + "console.log(JSON.stringify({r:r,called:called,t:t}));")
    out = subprocess.run([NODE, "-"], input=src, capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == {"r": expect, "called": 1 if expect else 0, "t": False}


def test_the_native_files_ship_with_the_package():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"cockpit_native.js"' in pyproject and '"cockpit_native.css"' in pyproject
