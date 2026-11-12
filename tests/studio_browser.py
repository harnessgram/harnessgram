"""A real panel, a real browser and the installed Studio app, for the Studio's tests and screenshots. The API is
answered by the real command unless a scenario replaces it at the browser's network layer (status and body of
`POST /app/studio/api`), which is how the error, offline and bridge-off states are reached without touching the
server's own switches."""
import copy
import json
import threading

from conftest import GOOD_TOKEN
from ctb import cockpit_apps, panel
from ctb.config import Config
from ctb.studio import install, provider
from test_panel import OWNER, signed
import panel_browser

API = "**/app/studio/api"


def start_panel(lang="he"):
    cockpit_apps._warned.clear()
    install.install()
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language=lang, ui_language=lang)
    srv = panel.PanelServer(("127.0.0.1", 0), cfg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def stop_panel(srv):
    srv.shutdown()
    srv.server_close()


def sample_desk(**override):
    d = copy.deepcopy(provider.sample())
    d.update(override)
    return d


def ok(desk):
    return 200, {"ok": True, "desk": desk}


class Scenario:
    """What the app's API answers while a test runs: (status, JSON body); `hang` never answers (the loading state);
    `abort` fails the request (no network). `set` changes the answer for the next call; `calls` counts the calls."""

    def __init__(self, status=200, body=None, hang=False, abort=False):
        self.calls = 0
        self.set(status, body, hang, abort)

    def set(self, status=200, body=None, hang=False, abort=False):
        self.status, self.body, self.hang, self.abort = status, body, hang, abort

    def __call__(self, route):
        self.calls += 1
        if self.hang:
            return
        if self.abort:
            route.abort()
            return
        route.fulfill(status=self.status, content_type="application/json",
                      body=json.dumps(self.body if self.body is not None else {}))


def scenario(page, status=200, body=None, hang=False, abort=False):
    sc = Scenario(status, body, hang, abort)
    page.route(API, sc)
    return sc


def wake(fr):
    """Make the app look at the desk again (the page becoming visible)."""
    fr.evaluate("document.dispatchEvent(new Event('visibilitychange'))")


def open_studio(browser, srv, lang="he", scheme="light", width=390, height=844, setup=None):
    """The panel with the Studio tab opened. `setup(page)` runs before the tab is clicked (install a scenario)."""
    stub = panel_browser.STUB
    panel_browser.STUB = stub.replace('colorScheme: "light"', 'colorScheme: "%s"' % scheme)  # the Telegram theme
    try:
        ctx, page = panel_browser.open_panel(browser, srv, signed(), lang=lang, width=width, height=height, scheme=scheme)
    finally:
        panel_browser.STUB = stub
    if setup:
        setup(page)
    page.click(".tab.app")
    fr = panel_browser.app_frame(page, "studio")
    return ctx, page, fr


class Router:
    """Answers the app's API per op: `answers[op]` is (status, body), `"hang"` (never answers) or `"abort"` (no
    network); an op without an answer goes to the real command. `seen` lists the bodies the page sent."""

    def __init__(self, answers=None):
        self.answers = dict(answers or {})
        self.seen = []

    def __call__(self, route):
        try:
            body = json.loads(route.request.post_data or "{}")
        except ValueError:
            body = {}
        self.seen.append(body)
        a = self.answers.get(body.get("op") if isinstance(body, dict) else None)
        if a is None:
            route.continue_()
        elif a == "hang":
            return
        elif a == "abort":
            route.abort()
        else:
            route.fulfill(status=a[0], content_type="application/json", body=json.dumps(a[1]))

    def ops(self, op=None):
        return [b for b in self.seen if isinstance(b, dict) and (op is None or b.get("op") == op)]


def router(page, answers=None):
    r = Router(answers)
    page.route(API, r)
    return r


def project_ok(pid="weekly-12", marks=None):
    return 200, {"ok": True, "project": provider.project(pid), "marks": marks or {}}
