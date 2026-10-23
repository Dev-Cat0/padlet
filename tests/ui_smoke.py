"""One-click panel smoke check, all network responses mocked."""
import json
from pathlib import Path
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright
ROOT = Path(__file__).resolve().parents[1]

def main():
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
        assert page.locator("#sendForm input,#sendForm textarea").count()==5
        page.locator("#url").fill("https://padlet.com/example/board")
        page.locator("#subject").fill("Test title")
        page.locator("#body").fill("Test body")
        page.locator("#limit").fill("3")
        page.locator("#delay").fill("0.5")
        page.locator("#start").click()
        page.wait_for_function("() => !document.getElementById('stop').disabled")
        assert calls==[{"url":"https://padlet.com/example/board","subject":"Test title","body":"Test body","limit":3,"delay":.5}]
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
            sent:3,errors:0,limit:3,rounds:1,logs:[]})""")
        assert abs(page.locator("#progress").evaluate("(el) => el.value") - 100 / 3) < .01
        assert "전체 3개 섹션" in page.locator("#board").inner_text()
        assert "총 9건 예정" in page.locator("#board").inner_text()
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
        print(json.dumps({"one_use_stays_disabled_after_stop_and_reload":True,"expiry_disables_button":True,"five_fields":True,"one_click_run":True,"stop_during_prepare":True,
                         "emergency_delete_and_retry":True,"backend_key_login":True,"key_not_stored":True,"real_posts":0,"real_deletions":0}))
        browser.close()
if __name__=="__main__":main()
