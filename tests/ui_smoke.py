"""One-click panel smoke check, all network responses mocked."""
import json
from pathlib import Path
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright
ROOT = Path(__file__).resolve().parents[1]

def main():
    (ROOT/'.local').mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        page=browser.new_page(viewport={"width":1366,"height":1000})
        state={"ready":False,"running":False,"opening":False,"phase":"waiting","board":None,"sent":0,"errors":0,"limit":0,"logs":[]}
        state["access"]={"known":True,"remaining":1,"expires":None,"can_run":True}
        calls,errors,deletes,requests=[],[],[],[]
        page.on("pageerror",lambda error:errors.append(str(error)))
        def route(route):
            requests.append((route.request.url,route.request.post_data or ''))
            path=urlsplit(route.request.url).path
            if path=="/api/auth/login":
                if route.request.post_data_json != {'key':'A1B2C'}:
                    return route.fulfill(status=403,json={'detail':'사용할 수 없는 키입니다.'})
                return route.fulfill(json={'token':'fixture'})
            if path=="/api/state": return route.fulfill(json=state)
            if path=="/api/run":
                assert len(route.request.headers['x-request-id']) >= 16
                calls.append(route.request.post_data_json)
                state["access"].update(remaining=0,can_run=False)
                state.update(opening=True,phase="preparing",limit=calls[-1]["limit"])
                return route.fulfill(json=state)
            if path=="/api/stop":
                state.update(opening=False,phase="stopped")
                return route.fulfill(json=state)
            if path=="/api/emergency-delete":
                deletes.append(route.request.post_data_json)
                state.update(deleting=True,phase="deleting",delete_pending=3)
                return route.fulfill(json=state)
            names={"/":"index.html","/app.js":"app.js","/style.css":"style.css"}
            assert path in names, path
            mime="text/html" if path=="/" else "text/css" if path.endswith("css") else "application/javascript"
            route.fulfill(content_type=mime,body=(ROOT/"web"/names[path]).read_text(encoding="utf-8"))
        page.route("**/*",route)
        page.goto("https://dev-cat0.github.io/")
        page.locator('#accessKey').wait_for()
        assert page.locator('#authGate').is_visible()
        assert page.locator('#panel').evaluate('(el) => el.inert')
        assert page.locator('#accessKey').get_attribute('type') == 'password'
        if (ROOT/'.local').is_dir():page.screenshot(path=str(ROOT/'.local/key-auth-desktop.png'),full_page=True)
        page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth') <= 390
        if (ROOT/'.local').is_dir():page.screenshot(path=str(ROOT/'.local/key-auth-mobile.png'),full_page=True)
        page.set_viewport_size({'width':1366,'height':1000})
        page.locator('#accessKey').fill('Z9Y8X')
        page.locator('#keySubmit').click()
        page.locator('#authStatus').filter(has_text='사용할 수 없는 키').wait_for()
        assert page.locator('#authGate').is_visible()
        assert page.locator('#accessKey').input_value() == ''
        page.locator('#accessKey').fill('a1b2c')
        page.locator('#keySubmit').click()
        page.wait_for_function("() => !document.getElementById('start').disabled")
        assert page.locator('#authGate').is_hidden()
        assert page.locator("#sendForm input,#sendForm textarea").count()==11
        assert page.locator('#passwordSettings').is_hidden()
        assert page.locator('#boardPassword').is_disabled()
        page.locator('#passwordEnabled').check()
        assert page.locator('#passwordSettings').is_visible()
        assert not page.locator('#boardPassword').evaluate('el => el.checkValidity()')
        page.locator('#boardPassword').fill('must-clear')
        page.locator('#passwordEnabled').uncheck()
        assert page.locator('#boardPassword').input_value() == ''
        assert page.locator('#commentSettings').is_hidden()
        assert page.locator('#commentBody').is_disabled()
        count = page.locator('#limit')
        for value, valid in [('0', True), ('5000', True), ('5001', False)]:
            count.fill(value)
            assert count.evaluate('el => el.checkValidity()') == valid
        state['access'].update(remaining=None, post_limit=1000000)
        page.evaluate('s => render(s)', state)
        assert count.evaluate('el => el.checkValidity()')
        assert count.get_attribute('min') == '0'
        state['access'].update(remaining=1, post_limit=5000)
        page.evaluate('s => render(s)', state)
        assert not count.evaluate('el => el.checkValidity()')
        assert '0은 무제한' in page.locator('#limitHint').inner_text()
        page.locator("#url").fill("https://padlet.com/example/board")
        page.locator("#subject").fill("Test title")
        page.locator("#body").fill("Test body")
        page.locator("#limit").fill("3")
        page.locator("#delay").fill("0.5")
        page.locator('#commentsEnabled').check()
        assert page.locator('#commentSettings').is_visible()
        assert not page.locator('#commentBody').evaluate('el => el.checkValidity()')
        page.locator('#commentBody').fill('Comment text')
        page.locator('#commentCount').fill('2')
        page.locator('#commentDelay').fill('0')
        page.locator('#passwordEnabled').check()
        page.locator('#boardPassword').fill('board-password-fixture')
        page.locator("#start").click()
        page.wait_for_function("() => !document.getElementById('stop').disabled")
        assert calls==[{"url":"https://padlet.com/example/board","subject":"Test title","body":"Test body","limit":3,"delay":.5,
                       "comments":{"enabled":True,"body":"Comment text","count":2,"delay":0},"password":"board-password-fixture"}]
        assert page.locator('#boardPassword').input_value() == ''
        assert page.locator('#passwordEnabled').is_disabled()
        assert page.locator('#commentsEnabled').is_disabled()
        assert page.locator('#commentCount').is_disabled()
        assert page.locator("#start").is_disabled()
        assert "준비" in page.locator("#phase").inner_text()
        page.locator("#stop").click()
        page.wait_for_function("() => document.getElementById('phase').textContent.includes('중지')")
        assert page.locator("#start").is_disabled()
        assert "남은 실행 0회" in page.locator("#accessStatus").inner_text()
        page.locator("#sendForm").evaluate("el => el.dispatchEvent(new Event('submit', {cancelable:true}))")
        assert len(calls) == 1
        page.reload()
        page.wait_for_function("() => document.getElementById('accessStatus').textContent.includes('0회')")
        assert page.locator("#start").is_disabled()
        assert "중지" in page.locator("#phase").inner_text()
        assert len(calls) == 1
        page.evaluate("""() => render({ready:false,running:false,opening:false,phase:'finished',
            board:{title:'Fixture',sections:[{id:1},{id:2},{id:3}]},target_count:3,
            sent:3,comments_sent:6,comments_targets:3,comments_errors:1,errors:1,limit:3,rounds:1,logs:[]})""")
        assert page.locator('#commentsSent').inner_text() == '6'
        assert '대상 3개 게시글' in page.locator('#commentStatus').inner_text()
        assert '댓글 오류 1건' in page.locator('#commentStatus').inner_text()
        assert abs(page.locator("#progress").evaluate("(el) => el.value") - 100 / 3) < .01
        assert "전체 3개 섹션" in page.locator("#board").inner_text()
        assert "총 9건 예정" in page.locator("#board").inner_text()
        for post_phase, comment_phase, expected in [
            ('sending','backoff','게시글: 작성 중 / 댓글: 요청 제한 · 대기 중'),
            ('error','commenting','게시글: 오류로 중단 / 댓글: 작성 중'),
            ('backoff','commenting','게시글: 요청 제한 · 대기 중 / 댓글: 작성 중')]:
            page.evaluate("s => render(s)", dict(state, running=True, opening=False,
                phase='sending', execution_mode='both', posts_phase=post_phase, comments_phase=comment_phase))
            assert page.locator('#phase').inner_text() == expected
            assert page.locator('#start').is_disabled() and not page.locator('#stop').is_disabled()
        page.evaluate("""() => render({ready:false,running:true,opening:false,phase:'commenting',
            execution_mode:'comments',comments_total:4,comments_sent:2,
            board:{title:'Fixture',sections:[]},sent:0,errors:0,limit:50,logs:[]})""")
        assert page.locator('#progress').evaluate('el => el.value') == 50
        assert '댓글만' in page.locator('#board').inner_text()
        assert '댓글 4건 예정' in page.locator('#board').inner_text()
        assert "섹션당 게시물 개수" in page.locator("label", has=page.locator("#limit")).inner_text()
        state.update(delete_pending=3)
        page.evaluate("s => render(s)", state)
        page.locator("#emergencyDelete").click()
        page.wait_for_function("() => document.getElementById('emergencyDelete').textContent === '삭제 중…'")
        assert deletes == [{}]
        assert page.locator("#start").is_disabled() and page.locator("#stop").is_disabled()
        assert page.locator("#emergencyDelete").is_disabled()
        state.update(deleting=False,phase="delete_incomplete",delete_pending=1,deleted=2,delete_errors=1)
        page.evaluate("s => render(s)", state)
        assert page.locator("#emergencyDelete").is_enabled()
        assert "삭제 확인 2건" in page.locator("#deleteStatus").inner_text()
        state.update(phase="deleted",delete_pending=0,deleted=3,delete_errors=0)
        page.evaluate("s => render(s)", state)
        assert page.locator("#emergencyDelete").is_disabled()
        assert page.locator("#start").is_disabled()
        state["access"].update(remaining=None,expires=page.evaluate("Date.now()/1000 + .3"),can_run=True)
        page.evaluate("s => render(s)", state)
        assert page.locator("#start").is_enabled()
        page.wait_for_function("() => document.getElementById('start').disabled")
        assert "만료" in page.locator("#accessStatus").inner_text()
        if (ROOT/".local").is_dir():page.screenshot(path=str(ROOT/".local/simple-panel.png"),full_page=True)
        page.set_viewport_size({"width":390,"height":844})
        assert page.evaluate("document.documentElement.scrollWidth")<=390
        assert not errors, errors
        assert not any('/api/session' in url for url, _ in requests)
        assert page.locator('#accessKey').input_value() == ''
        assert all('A1B2C' not in body for url,body in requests if '/api/auth/login' not in url)
        assert page.evaluate("JSON.stringify({...sessionStorage, ...localStorage})") == '{"padlet-session":"fixture"}'
        state['access'].update(remaining=1, expires=None, can_run=True)
        state.update(phase='waiting', comments_sent=0, comments_targets=0)
        page.evaluate('s => render(s)', state)
        page.locator('#commentsEnabled').check()
        page.locator('#commentBody').fill('댓글 내용 예시')
        page.locator('#commentCount').fill('2')
        page.locator('#commentDelay').fill('0')
        assert page.evaluate('document.documentElement.scrollWidth') <= 390
        page.screenshot(path=str(ROOT/'.local/comments-mobile.png'), full_page=True)
        page.set_viewport_size({'width':1366,'height':1000})
        page.screenshot(path=str(ROOT/'.local/comments-desktop.png'), full_page=True)
        print(json.dumps({"one_use_stays_disabled_after_stop_and_reload":True,"expiry_disables_button":True,"comment_controls":True,"one_click_run":True,"stop_during_prepare":True,
                         "emergency_delete_and_retry":True,"backend_key_login":True,"key_not_stored":True,"real_posts":0,"real_deletions":0}))
        browser.close()
if __name__=="__main__":main()
