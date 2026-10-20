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
        calls,errors=[],[]
        page.on("pageerror",lambda error:errors.append(str(error)))
        def route(route):
            path=urlsplit(route.request.url).path
            if path=="/api/session": return route.fulfill(json={"token":"fixture"})
            if path=="/api/state": return route.fulfill(json=state)
            if path=="/api/run":
                calls.append(route.request.post_data_json)
                state.update(opening=True,phase="preparing",limit=calls[-1]["limit"])
                return route.fulfill(json=state)
            if path=="/api/stop":
                state.update(opening=False,phase="stopped")
                return route.fulfill(json=state)
            names={"/":"index.html","/app.js":"app.js","/style.css":"style.css"}
            assert path in names, path
            mime="text/html" if path=="/" else "text/css" if path.endswith("css") else "application/javascript"
            route.fulfill(content_type=mime,body=(ROOT/"web"/names[path]).read_text(encoding="utf-8"))
        page.route("**/*",route)
        page.goto("https://dev-cat0.github.io/")
        page.wait_for_function("() => !document.getElementById('start').disabled")
        assert page.locator("input,textarea").count()==5
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
        page.wait_for_function("() => !document.getElementById('start').disabled")
        assert "중지" in page.locator("#phase").inner_text()
        assert page.locator("#subject").input_value()=="Test title"
        page.evaluate("""() => render({ready:false,running:false,opening:false,phase:'finished',
            board:{title:'Fixture',sections:[{id:1},{id:2},{id:3}]},target_count:3,
            sent:3,errors:0,limit:3,rounds:1,logs:[]})""")
        assert abs(page.locator("#progress").evaluate("(el) => el.value") - 100 / 3) < .01
        assert "전체 3개 섹션" in page.locator("#board").inner_text()
        assert "총 9건 예정" in page.locator("#board").inner_text()
        assert "섹션당 게시물 개수" in page.locator("label", has=page.locator("#limit")).inner_text()
        if (ROOT/".local").is_dir():page.screenshot(path=str(ROOT/".local/simple-panel.png"),full_page=True)
        page.set_viewport_size({"width":390,"height":844})
        assert page.evaluate("document.documentElement.scrollWidth")<=390
        assert not errors, errors
        print(json.dumps({"five_fields":True,"one_click_run":True,"stop_during_prepare":True,"real_posts":0}))
        browser.close()
if __name__=="__main__":main()
