"""Static panel smoke check: every API/browser response is a local fixture."""
import json
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

root = Path(__file__).resolve().parents[1]
state = {"ready": False, "running": False, "opening": False, "browser": False, "board": None,
         "phase": "waiting", "sent": 0, "errors": 0, "limit": 1, "rounds": 0, "target_count": 1, "logs": []}
actions, posts, errors = [], [], []


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        fixture = browser.new_page(viewport={"width": 1100, "height": 720})
        fixture.set_content("<body style='font:24px sans-serif;padding:30px'><h1>Padlet · Browser fixture</h1>"
                            "<p>Click a field, type text, then publish one post.</p><input placeholder='Title'>"
                            "<p><button>Publish</button></p></body>")
        frame = fixture.screenshot(type="jpeg")
        fixture.close()

        def route(route):
            path = urlsplit(route.request.url).path
            if path == "/api/session":
                return route.fulfill(json={"token": "fixture"})
            if path == "/api/browser/open":
                state.update(browser=True, phase="capturing")
                return route.fulfill(json=state)
            if path == "/api/browser/frame":
                return route.fulfill(content_type="image/jpeg", body=frame)
            if path == "/api/browser/tabs":
                return route.fulfill(json=[{"index": 0, "host": "padlet.com", "active": True}])
            if path == "/api/browser/action":
                actions.append(route.request.post_data_json)
                return route.fulfill(json=state)
            if path == "/api/start":
                posts.append(route.request.post_data_json)
                return route.fulfill(json=state)
            if path == "/api/state":
                return route.fulfill(json=state)
            if path == "/config.js":
                return route.fulfill(content_type="application/javascript",
                    body='window.PADLET_CONFIG={serverUrl:"http://127.0.0.1:4173"};')
            files = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "application/javascript"),
                     "/style.css": ("style.css", "text/css")}
            if path in files:
                file, mime = files[path]
                return route.fulfill(content_type=mime, body=(root / "web" / file).read_text(encoding="utf-8"))
            route.abort()

        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/*", route)
        page.goto("http://127.0.0.1:4173/")
        page.wait_for_function("() => !document.getElementById('openBtn').disabled")
        page.locator("#targetUrl").fill("https://padlet.com/example/board")
        page.locator("#openBtn").click()
        page.wait_for_function("() => document.getElementById('browserFrame').naturalWidth === 1100")
        assert not page.locator("#browserPanel").is_hidden()
        assert page.locator("#startBtn").is_disabled()
        page.locator("#browserFrame").click(position={"x": 100, "y": 100})
        page.wait_for_function("() => !document.getElementById('browserText').disabled")
        assert actions[-1]["kind"] == "click"
        page.locator("#browserText").fill("한글 게시 입력")
        page.locator("#browserTextForm button").click()
        page.wait_for_function("() => !document.getElementById('browserText').disabled")
        assert actions[-1] == {"kind": "text", "text": "한글 게시 입력"}
        assert page.locator("#browserText").input_value() == ""
        output = root / ".local"
        if output.is_dir():
            page.screenshot(path=str(output / "browser-capture-panel.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            page.screenshot(path=str(output / "browser-capture-mobile.png"), full_page=True)
            assert page.evaluate("document.documentElement.scrollWidth") <= 390
            page.set_viewport_size({"width": 1440, "height": 1100})
        state.update(ready=True, phase="ready", board={
            "url": "https://padlet.com/example/board", "title": "Captured board", "capture_id": "new-capture",
            "sections": [], "fields": {"required": {"body": True}, "custom": [
                {"id": "answer", "name": "Answer", "type": "text", "supported": True, "required": True}]},
            "draft": {"subject": "Sample title", "body": "Sample body",
                      "attachment": "", "custom_properties": {"answer": "Captured answer"}}})
        page.wait_for_function("() => !document.getElementById('startBtn').disabled")
        assert page.locator("#browserPanel").is_hidden()
        assert page.locator("#subject").input_value() == "Sample title"
        assert page.locator("[data-property-id=answer]").input_value() == "Captured answer"
        page.locator("#body").fill("Edited body")
        page.locator("#limit").fill("1")
        page.locator("#startBtn").click()
        page.wait_for_function("() => !document.getElementById('startBtn').disabled")
        assert posts[-1]["body"] == "Edited body"
        assert posts[-1]["custom_properties"] == {"answer": "Captured answer"}
        assert posts[-1]["limit"] == 1
        assert not errors, errors
        print(json.dumps({"ui_capture_controls": "passed", "draft_loaded": True, "real_posts": 0}))
        browser.close()


if __name__ == "__main__":
    main()
