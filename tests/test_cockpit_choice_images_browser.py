"""Phone visual choices in both directions, with the real history stack and answer payload."""
import json
import time

import pytest

from ctb import panel_page
import panel_browser
from test_cards_images import png
from test_cockpit_compact_browser import state, assert_no_overflow
from test_cockpit_overflow_browser import browser  # noqa: F401



def wait_true(page, expression, timeout=5.0):
    """page.wait_for_function compiles its predicate with eval, which this page's CSP (kept on: it is under test) forbids;
    page.evaluate runs over the debugging protocol instead."""
    import time
    end = time.monotonic() + timeout
    while True:
        if page.evaluate(expression):
            return
        if time.monotonic() > end:
            raise AssertionError("timed out waiting for " + expression)
        page.wait_for_timeout(50)

def open_choices(browser, lang, pictures=True):
    ctx = browser.new_context(viewport={'width': 390, 'height': 844}, has_touch=True, bypass_csp=False,
                              locale='he-IL' if lang == 'he' else 'en-US')
    page = ctx.new_page()
    data = state()
    labels = ['עיצוב כחול', 'עיצוב בהיר'] if lang == 'he' else ['Blue design', 'Light design']
    card = dict(id='a' * 16, kind='choice', title='איזה עיצוב?' if lang == 'he' else 'Which design?',
                detail='', age=2, session='compact-session', here=True, options=labels)
    if pictures:
        card['images'] = ['b' * 32, 'c' * 32]
    data['cards'] = [card]
    answers, mints, errors = [], [], []
    page.on('pageerror', lambda error: errors.append(str(error)))

    def respond(route):
        url = route.request.url
        if url.endswith('telegram-web-app.js'):
            route.fulfill(content_type='application/javascript', body=panel_browser.STUB % json.dumps('test'))
        elif url.endswith('/api/state'):
            route.fulfill(content_type='application/json', body=json.dumps(data))
        elif url.endswith('/api/card-media-token'):
            mints.append(route.request.post_data_json)
            route.fulfill(content_type='application/json', body=json.dumps({'token': 'image-only', 'expires': int(time.time()) + 600}))
        elif '/card-media/' in url:
            assert '?auth=image-only' in url
            route.fulfill(content_type='image/png', body=png(800, 600))
        elif url.endswith('/api/card'):
            answers.append(route.request.post_data_json)
            route.fulfill(content_type='application/json', body=json.dumps({'ok': True, 'delivery': 'queued', 'key': 'key'}))
        else:
            route.fulfill(content_type='text/html', body=panel_page.render_page(lang, 'test').decode(),
                          headers={'Content-Security-Policy': panel_page.csp('test', "'self'")})
    page.route('**/*', respond)
    page.goto('http://panel.test/')
    page.wait_for_selector('.k-choice')
    return ctx, page, card, answers, mints, errors


@pytest.mark.parametrize('lang', ['he', 'en'])
@pytest.mark.parametrize('closing', ['button', 'browser', 'telegram', 'escape'])
def test_phone_tiles_viewer_back_zoom_and_choice(browser, lang, closing):
    ctx, page, card, answers, mints, errors = open_choices(browser, lang)
    try:
        assert page.locator('html').get_attribute('dir') == ('rtl' if lang == 'he' else 'ltr')
        tiles = page.locator('.choice-tile')
        assert tiles.count() == 2
        wait_true(page, "[...document.querySelectorAll('.choice-preview')].every(e=>!e.disabled)")
        assert mints == [{'id': ident} for ident in card['images']]
        assert tiles.nth(0).locator('.choice-label').inner_text() == card['options'][0]
        first, second = tiles.nth(0).bounding_box(), tiles.nth(1).bounding_box()
        assert abs(first['y'] - second['y']) < 2  # two columns on a 390 px phone
        assert (first['x'] > second['x']) if lang == 'he' else (first['x'] < second['x'])
        assert tiles.first.locator('.copt').bounding_box()['height'] >= 44
        assert_no_overflow(page)
        tiles.first.locator('.choice-preview').click()
        viewer = page.locator('.card-image-viewer')
        assert viewer.is_visible()
        assert viewer.locator('img').get_attribute('alt') == card['options'][0]
        assert answers == []  # opening the image never answers
        assert viewer.bounding_box()['width'] == 390
        assert viewer.bounding_box()['height'] == 844
        assert page.locator(':focus').get_attribute('class') == 'card-image-close'
        assert_no_overflow(page)
        page.locator('.card-image-stage').dblclick()
        assert 'scale(2)' in viewer.locator('img').get_attribute('style')
        # Two touch pointers zoom; one pointer then pans inside the full-screen stage.
        page.evaluate('''() => {
          const s=document.querySelector('.card-image-stage');
          const send=(t,id,x,y)=>s.dispatchEvent(new PointerEvent(t,{pointerId:id,clientX:x,clientY:y,pointerType:'touch',bubbles:true}));
          s.setPointerCapture=()=>{};
          send('pointerdown',11,120,400);send('pointerdown',12,220,400);
          send('pointermove',12,270,400);send('pointerup',12,270,400);
          send('pointermove',11,145,420);send('pointerup',11,145,420);
        }''')
        assert 'scale(3)' in viewer.locator('img').get_attribute('style')
        assert viewer.locator('img').evaluate('e=>{const m=new DOMMatrix(getComputedStyle(e).transform);return m.m41!==0||m.m42!==0}')
        if closing == 'button':
            viewer.locator('.card-image-close').click()
        elif closing == 'browser':
            page.go_back()
        elif closing == 'telegram':
            page.evaluate('window.__backClick()')
        else:
            page.keyboard.press('Escape')
        wait_true(page, "!document.querySelector('.card-image-viewer').open")
        wait_true(page, 'history.state.cockpitBack.index === 0')
        assert answers == []
        assert page.locator(':focus').get_attribute('class') == 'choice-preview'
        assert page.evaluate('document.body.style.overflow') == ''
        assert_no_overflow(page)
        tiles.nth(1).locator('.copt').click()
        wait_true(page, "!!document.querySelector('.k-choice .done-c')")
        assert answers == [{'id': card['id'], 'pick': 1}]
        assert not errors
    finally:
        ctx.close()


@pytest.mark.parametrize('lang', ['he', 'en'])
def test_text_only_choices_keep_original_buttons_and_answer_flow(browser, lang):
    ctx, page, card, answers, mints, errors = open_choices(browser, lang, False)
    try:
        assert page.locator('.choice-tile').count() == 0
        assert page.locator('.opts .copt').count() == 2
        assert page.locator('.opts .copt').all_text_contents() == card['options']
        page.locator('.opts .copt').first.click()
        wait_true(page, "!!document.querySelector('.k-choice .done-c')")
        assert answers == [{'id': card['id'], 'pick': 0}] and mints == [] and errors == []
        assert_no_overflow(page)
    finally:
        ctx.close()
