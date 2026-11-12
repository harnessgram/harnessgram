"""A headless browser driving a throwaway local panel with a stubbed Telegram Mini App object, for the tests (and
screenshots) that need the page's real behaviour. Nothing here talks to Telegram or the network: the Telegram
script is answered locally and the panel runs on a loopback port."""
import json
import os
import pwd
import sys
from pathlib import Path

STUB = r"""
window.__log = [];
window.Telegram = {WebApp: {
  initData: %s, colorScheme: "light", ready(){}, expand(){}, disableVerticalSwipes(){},
  isVersionAtLeast(){ return true; }, setHeaderColor(){}, setBackgroundColor(){}, setBottomBarColor(){},
  onEvent(){}, enableClosingConfirmation(){}, disableClosingConfirmation(){}, openLink(u){ window.__log.push(["link", u]); },
  openTelegramLink(u){ window.__log.push(["tg", u]); }, HapticFeedback: {impactOccurred(){}, selectionChanged(){}, notificationOccurred(){}},
  BackButton: {show(){ window.__log.push(["back", "show"]); }, hide(){ window.__log.push(["back", "hide"]); },
               onClick(f){ window.__backClick = f; }},
  SettingsButton: {onClick(){}, show(){}}, MainButton: {}
}};
"""

# A sample app page: reports every message it receives to the test through the page title and its own DOM.
SAMPLE_PAGE = """<!doctype html><meta charset="utf-8"><title>Sample</title>
<style>body{margin:0;font:16px sans-serif;background:#eef3fb;min-block-size:100vh;display:grid;grid-template-rows:1fr auto}
main{padding:16px}.dock{padding:12px 16px calc(12px + var(--inset-bottom,0px));background:#fff;border-block-start:1px solid #cdd7e8}
.dock button{inline-size:100%;min-block-size:48px;border:0;border-radius:14px;background:#2f7cf6;color:#fff;font:inherit;font-weight:700}</style>
<main><h2 id="h">sample</h2><p id="log"></p></main>
<div class="dock"><button id="primary" type="button">Primary action</button></div>
<script>
var seen = [];
function show(){ document.getElementById("log").textContent = JSON.stringify(seen); }
window.addEventListener("message", function (e) {
  var m = e.data || {};
  if (m.inset) document.documentElement.style.setProperty("--inset-bottom", m.inset.bottom + "px");
  seen.push(m.cockpit); show();
  if (m.cockpit === "back" && window.ackBack !== false) ask({cockpit: "back_done"});
});
function ask(msg) { parent.postMessage(msg, "*"); }
ask({cockpit: "hello"});
</script>"""


def prepare_env():
    """The test run swaps HOME for a private folder, so point Playwright at the account's own browser cache."""
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        return
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    cache = home / ("Library/Caches/ms-playwright" if sys.platform == "darwin" else ".cache/ms-playwright")
    if cache.is_dir():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(cache)


def launch(playwright):
    """A headless Chromium, or None when none is installed here."""
    try:
        return playwright.chromium.launch()
    except Exception:  # no browser binary on this machine
        return None


def open_panel(browser, srv, init, lang="en", width=390, height=844, scheme="light"):
    ctx = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=2, color_scheme=scheme, bypass_csp=True,
                              locale="he-IL" if lang == "he" else "en-US")
    page = ctx.new_page()
    page.route("https://telegram.org/js/telegram-web-app.js",
               lambda route: route.fulfill(status=200, content_type="application/javascript", body=STUB % json.dumps(init)))
    page.goto("http://127.0.0.1:%d/" % srv.server_address[1])
    page.wait_for_selector(".tabs")
    return ctx, page


def app_frame(page, name="sample"):
    page.wait_for_selector("iframe.appframe")
    for fr in page.frames:
        if fr.url.startswith("http://127.0.0.1") and ("/app/%s/" % name) in fr.url:
            return fr
    raise AssertionError("the app frame did not load")
