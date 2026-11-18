"""Capture deterministic phone fixtures and optional side-by-side images for fidelity review.

Run with PYTHONPATH=src:tests python tests/cockpit_v2_screenshots.py [--milestone 1|2].
Milestone 1 captures Now into build/; milestone 2 captures Projects, one project, Tools (Radio,
Studio) and Settings into build-m2/. Milestone 3 captures "What the system knows" (Hebrew, light and dark) into
knowledge-market/build-s1/ (pass --hifi for its approved design folder). Milestone 4 is the spacing pass: Now,
Projects, one project, Tools, Settings and the knowledge home with a crowded fixture (raw slug names, quiet
projects, no art mapping) into v2-spacing/after/. --out overrides the destination; --width the phone width.
No live owner data, bridge connection or network requests are used.
"""
import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

import panel_browser
from cockpit_v2_browser import assert_contract, assert_screen_contract, crowded_state, open_v2, rich_state

BASE = Path.home() / ".codex" / "state" / ("harnessgram" + "-redesign")


def _settle(page):
    page.evaluate("document.fonts.ready")
    page.evaluate("Promise.all(Array.from(document.images).map(i=>i.decode().catch(()=>{})))")
    page.wait_for_timeout(250)


def go_projects(page):
    page.locator('#tabs [data-v2-tab="agents"]').click()
    page.locator(".v2-plist").wait_for()


def go_project(page):
    go_projects(page)
    page.locator(".v2-pcard").first.click()
    page.locator(".v2-ppage").wait_for()


def go_radio(page):
    page.locator('#tabs [data-v2-tab="tools"]').click()
    page.locator('[data-v2-tool="radio"]').click()
    page.locator(".v2-radio-hero #radio-player .v2-radio-art").wait_for()


def go_addons(page):
    page.locator('#tabs [data-v2-tab="settings"]').click()
    for _ in range(4):  # a knowledge page left open replaces Settings: step back out of it first
        if page.locator('[data-v2-section="addons-entry"], [data-v2-section="addons"]').count():
            break
        page.locator("[data-vk-back]").first.click()
        page.wait_for_timeout(100)
    if page.locator('[data-v2-section="addons"]').count():
        _settle(page)
        return
    page.locator('[data-v2-section="addons-entry"]').click()
    page.locator('[data-v2-section="addons"] .va-group').first.wait_for()
    _settle(page)


def go_studio(page):
    page.locator('#tabs [data-v2-tab="tools"]').click()
    page.locator('[data-v2-tool="studio"]').click()
    frame = panel_browser.app_frame(page, "studio")
    frame.wait_for_selector("#app *", timeout=10000)
    page.wait_for_timeout(600)


def go_settings(page):
    page.locator('#tabs [data-v2-tab="settings"]').click()
    page.locator('[data-v2-section="settings-notify"]').wait_for()
    # The approved design shows these two groups open.
    for group in ("auto", "connect"):
        toggle = page.locator('[data-v2-section="settings-%s"] > .compact-toggle' % group)
        if toggle.get_attribute("aria-expanded") != "true":
            toggle.click()
    page.wait_for_timeout(250)


def go_knowledge(page):
    if not page.locator("[data-v2-section=knowledge-entry]").count():
        go_settings(page)
    page.locator("[data-v2-section=knowledge-entry]").click()
    page.locator(".vk-tiles").wait_for()


def go_k_rules(page):
    go_knowledge(page)
    page.locator('.vk-tile[data-vk-kind="rule"]').click()
    page.locator('.vk-list[data-vk-kind="rule"]').wait_for()


def go_k_item(page):
    go_k_rules(page)
    page.locator('.vk-row[data-vk-item$="one-question"] .vk-row-main').click()
    page.locator(".vk-sheet").wait_for()
    page.wait_for_timeout(200)


def go_k_vault(page):
    go_knowledge(page)
    page.locator('.vk-vault-tile').click()
    page.locator(".vk-vault-list").wait_for()


def go_k_skill(page):
    go_knowledge(page)
    page.locator('.vk-tile[data-vk-kind="skill"]').click()
    page.locator('.vk-row[data-vk-item="skill:letter"] .vk-row-main').click()
    page.locator(".vk-sheet").wait_for()
    page.wait_for_timeout(200)


def go_k_history(page):
    go_knowledge(page)
    page.locator('.vk-history-row').click()
    page.locator(".vk-history").wait_for()


SCREENS = {
    1: [("now", None, "1-now.png")],
    2: [("projects", go_projects, "2-projects.png"), ("project", go_project, "3-project.png"),
        ("tools-radio", go_radio, "4-tools-radio.png"), ("tools-studio", go_studio, "5-tools-studio.png"),
        ("settings", go_settings, "6-settings.png")],
    # "What the system knows", stage 1 (hi-fi: knowledge-market/hifi-stage1).
    3: [("k-home", go_knowledge, "1-home-light.png", "2-home-dark.png"), ("k-rules", go_k_rules, "3-rules-list.png"),
        ("k-item", go_k_item, "4-item-sheet.png"), ("k-vault", go_k_vault, "5-safety-vault.png"),
        ("k-skill", go_k_skill, "6-skill-before.png"), ("k-history", go_k_history, "7-history.png")],
    # The spacing and responsiveness pass: every top-level v2 screen with the crowded fixture.
    4: [("now", None, "1-now.png"), ("projects", go_projects, "2-projects.png"), ("project", go_project, "3-project.png"),
        ("tools-radio", go_radio, "4-tools-radio.png"), ("settings", go_settings, "6-settings.png"),
        ("k-home", go_knowledge, "1-home-light.png")],
}


def capture(out: Path, milestone=2, hifi=BASE / "hifi", width=390):
    panel_browser.prepare_env()
    out.mkdir(parents=True, exist_ok=True)
    receipt = {"fixture": True, "milestone": milestone, "viewport": {"width": width, "height": 844}, "images": []}
    states = {4: crowded_state}
    with sync_playwright() as p:
        browser = panel_browser.launch(p)
        if browser is None:
            raise RuntimeError("Chromium cannot launch here; run this helper in a browser-capable environment")
        try:
            for lang in (("he",) if milestone == 3 else ("he", "en")):
                for scheme in ("light", "dark"):
                    for name, go, reference, *dark in SCREENS[milestone]:
                        ctx, page, _, errors, state = open_v2(browser, lang, scheme, width=width,
                                                              state=states.get(milestone, rich_state)(lang) if milestone >= 2 else None)
                        try:
                            assert_contract(page, state)
                            if go:
                                go(page)
                                assert_screen_contract(page, name)
                            else:
                                page.locator("#radio-player .v2-radio-art").wait_for()
                            assert not errors, errors
                            _settle(page)
                            path = out / ("%s-%d-%s-%s.png" % (name, width, lang, scheme))
                            viewport = path.with_name(path.stem + "-viewport.png")
                            page.screenshot(path=str(viewport), animations="disabled")
                            # A viewport as tall as the page keeps the fixed tab bar at the bottom of the full image.
                            page.set_viewport_size({"width": width, "height": max(844, page.evaluate("document.documentElement.scrollHeight"))})
                            page.wait_for_timeout(150)
                            page.screenshot(path=str(path), animations="disabled")
                            entry = {"screen": name, "lang": lang, "scheme": scheme, "full": str(path), "viewport": str(viewport)}
                            ref = hifi / (reference if scheme == "light" else (dark[0] if dark else reference))
                            if lang == "he" and (scheme == "light" or dark) and ref.is_file():
                                entry.update(diff_images(path, ref))
                            receipt["images"].append(entry)
                        finally:
                            ctx.close()
        finally:
            browser.close()
    (out / "screenshots.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
    return receipt


def diff_images(actual_path: Path, reference_path: Path):
    from PIL import Image, ImageChops, ImageStat
    actual = Image.open(actual_path).convert("RGB")
    reference = Image.open(reference_path).convert("RGB")
    reference = reference.resize((actual.width, round(reference.height * actual.width / reference.width)))
    # Keep aspect ratios: full-page length reflects readable text and touch
    # targets, so do not stretch the approved concept to hide layout differences.
    height = max(actual.height, reference.height)
    a = Image.new("RGB", (actual.width, height), "white")
    b = a.copy()
    a.paste(actual, (0, 0))
    b.paste(reference, (0, 0))
    diff = ImageChops.difference(a, b)
    path = actual_path.with_name(actual_path.stem + "-diff.png")
    diff.save(path)
    pair = Image.new("RGB", (actual.width * 2, height), "white")
    pair.paste(b, (0, 0))
    pair.paste(a, (actual.width, 0))
    pair_path = actual_path.with_name(actual_path.stem + "-comparison.png")
    pair.save(pair_path)
    return {"diff": str(path), "comparison": str(pair_path), "mean_absolute_pixel_delta": sum(ImageStat.Stat(diff).mean) / 3,
            "reference": str(reference_path), "note": "Visual aid only; not a fidelity pass or owner approval"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--milestone", type=int, choices=(1, 2, 3, 4), default=2)
    parser.add_argument("--width", type=int, default=390)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--hifi", type=Path, default=BASE / "hifi")
    args = parser.parse_args()
    out = args.out or BASE / {1: "build", 2: "build-m2", 3: "knowledge-market/build-s1", 4: "v2-spacing/after"}[args.milestone]
    print(json.dumps(capture(out, args.milestone, args.hifi, args.width), ensure_ascii=False, indent=2))
