"""390 px he/en owner confirmation and collapsed older requests, using the real page offline."""
import json
import time

import pytest
from ctb import panel_page
import panel_browser
from test_cockpit_overflow_browser import browser


@pytest.mark.parametrize('lang', ['he','en'])
def test_owner_controls_and_long_waiting_requests_fit_phone(browser, lang):
    now=time.time()
    def row(n, older=False):
        return dict(project='Example', id=n, title='בקשה לבדיקה / A request to review', status='open',
                    wait='owner', age=900000 if older else 100, updated=now-100, idle=900000 if older else 100, long=older, older=older)
    data={'now':now,'sessions':[], 'cards':[], 'decisions':[], 'requests':[row(1),row(2,True)],
          'settings':{'auto_compact':400,'cold_compact':'on'}}
    requests=[]
    ctx=browser.new_context(viewport={'width':390,'height':844},bypass_csp=True)
    page=ctx.new_page()
    def route(r):
        url=r.request.url
        if url.endswith('telegram-web-app.js'):
            r.fulfill(content_type='application/javascript',body=panel_browser.STUB % json.dumps('test'))
        elif url.endswith('/api/state'):
            r.fulfill(content_type='application/json',body=json.dumps(data))
        elif url.endswith('/api/request-detail'):
            r.fulfill(content_type='application/json',body=json.dumps({'ok':True,'title':'בקשה לבדיקה / A request to review','note':'','steps':[]}))
        elif url.endswith('/api/request'):
            body=r.request.post_data_json
            requests.append(body)
            if body['action']=='done': data['requests'][0]['status']='done'
            r.fulfill(content_type='application/json',body=json.dumps({'ok':True}))
        else:
            r.fulfill(content_type='text/html',body=panel_page.render_page(lang,'test').decode())
    page.route('**/*',route)
    try:
        page.goto('http://panel.test/')
        page.wait_for_selector('.requests')
        assert page.locator('html').get_attribute('dir') == ('rtl' if lang=='he' else 'ltr')
        s=panel_page.STRINGS[lang]
        old=page.locator('[data-block="requests-long"]')
        mine='[data-block^="requests-group:owner:"]'
        assert old.locator(':scope > button').get_attribute('aria-expanded')=='false'
        assert page.locator(mine+' .request-row').count()==1
        assert page.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth')
        old.locator(':scope > button').click()
        page.wait_for_timeout(700)
        assert old.locator(':scope > button').get_attribute('aria-expanded')=='true'
        old.locator('.request-toggle').first.click()
        old.locator('.request-actions button').first.wait_for(state='visible')
        # An item untouched for a week offers keep-open and drop, not done and push.
        assert [b.inner_text() for b in old.locator('.request-actions button:visible').all()]==[s['requestTouch'],s['requestDrop']]
        assert page.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth')
        page.locator(mine+' .request-toggle').first.click()
        action=page.locator(mine+' .request-actions').get_by_role('button',name=s['requestDone'],exact=True)
        action.click()
        confirm=page.locator(mine+' .request-actions').get_by_role('button',name=s['requestConfirm'],exact=True)
        assert confirm.is_visible() and requests == []
        assert page.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth')
        confirm.click()
        page.wait_for_function('document.querySelector(\'[data-block^="requests-group:owner:"] .request-actions\') === null')
        assert [r['action'] for r in requests]==['done']
        assert requests[0] == {'project':'Example', 'id':1, 'action':'done'}
        page.locator('.tabs .tab',has_text=s['settings']).first.click()
        page.get_by_text(s['set_cold_compact'],exact=True).wait_for()
        assert page.get_by_text(s['hint_cold_compact'],exact=True).is_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth')
    finally:
        ctx.close()
