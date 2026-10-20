"""Anonymous bookmarklet tests. Every network request is fulfilled locally."""
import copy
import json
from pathlib import Path
from urllib.parse import quote, urlsplit

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "web/anonymous.js").read_text(encoding="utf-8")
BOOKMARK = "javascript:" + quote("void " + SOURCE.strip().removesuffix(";"), safe="")
STATE = {"canIWrite": True, "user": {"id": 11, "hashid": "guest", "registered_at": None},
         "wall": {"id": 22, "hashid": "board", "title": "Fixture"}}
PAYLOAD = {"wall_id": 22, "author_id": 11, "author_hashid": "guest", "wall_hashid": "board",
           "subject": "Sample", "body": "<p><b>Original body</b></p>", "attachment": "https://example.com/image.png",
           "custom_properties": {"required-image": {"asset_id": 42}}, "published": True}
HTML = """<!doctype html><meta name="csrf-token" content="fixture-csrf">
<body><h1>Padlet fixture</h1><button id="manual">Publish one sample</button>
<script src="/fixture.js"></script></body>"""


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        def scenario(kind):
            context = browser.new_context(viewport={"width": 1366, "height": 950})
            page = context.new_page()
            state = copy.deepcopy(STATE)
            posts, outbound, errors = [], [], []
            attempts = {}
            if kind == "logged-in":
                state["user"]["registered_at"] = "2020-01-01"
            if kind == "unknown-identity":
                del state["user"]["registered_at"]
            page.on("pageerror", lambda error: errors.append(str(error)))

            def route(route):
                request = route.request
                parsed = urlsplit(request.url)
                outbound.append(request.url)
                assert parsed.hostname == "padlet.com", request.url
                if parsed.path == "/example/board":
                    return route.fulfill(content_type="text/html", body=HTML, headers={
                        "Content-Security-Policy": "script-src 'self'; connect-src 'self'; style-src 'unsafe-inline'",
                        "Set-Cookie": "fixture=anonymous; Path=/; Secure; HttpOnly; SameSite=Lax"})
                if parsed.path == "/fixture.js":
                    code = "document.getElementById('manual').onclick=()=>fetch('/api/9/wishes',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(" + json.dumps(PAYLOAD) + ")});"
                    if kind == "xhr":
                        code = "document.getElementById('manual').onclick=()=>{const x=new XMLHttpRequest();x.open('POST','/api/9/wishes');x.setRequestHeader('Content-Type','application/json');x.send(JSON.stringify(" + json.dumps(PAYLOAD) + "));};"
                    return route.fulfill(content_type="application/javascript", body=code)
                if parsed.path.startswith("/api/11/padlet-starting-state"):
                    return route.fulfill(json=state)
                if parsed.path == "/api/5/wall_sections":
                    return route.fulfill(json={"data": [
                        {"attributes": {"id": 1, "wall_id": 22, "title": "First"}},
                        {"attributes": {"id": 2, "wall_id": 22, "title": "Second"}}]})
                if parsed.path == "/api/9/wishes":
                    payload = request.post_data_json
                    posts.append(payload)
                    if len(posts) == 1:
                        return route.fulfill(status=201, json={"data": {"attributes": {"id": 100}}})
                    assert "fixture=anonymous" in request.all_headers().get("cookie", "")
                    section = payload["wall_section_id"]
                    attempts[section] = attempts.get(section, 0) + 1
                    if kind == "challenge":
                        return route.fulfill(status=403, headers={"cf-mitigated": "challenge"}, body="Verify")
                    if kind == "unconfirmed":
                        return route.fulfill(status=200, json={"error": "rejected"})
                    if kind == "rate-limit" and section == 2 and attempts[section] == 1:
                        return route.fulfill(status=429, headers={"Retry-After": "0"}, json={"errors": []})
                    return route.fulfill(status=201, json={"data": {"id": len(posts) + 100}})
                raise AssertionError("Unexpected request " + request.url)

            page.route("**/*", route)
            page.goto("https://padlet.com/example/board")
            # Emulate a browser bookmark navigation, with a strict page CSP in force.
            cdp = context.new_cdp_session(page)
            cdp.send("Page.navigate", {"url": BOOKMARK, "transitionType": "auto_bookmark"})
            root = page.locator("#padlet-anonymous-sender")
            root.wait_for()
            page.wait_for_function("() => !document.querySelector('#padlet-anonymous-sender').shadowRoot.querySelector('#capture').disabled")
            if kind in {"logged-in", "unknown-identity"}:
                assert "익명 세션이 아닙니다" in root.locator("#status").inner_text()
                assert root.locator("#start").is_disabled()
                page.locator("#manual").click()
                page.wait_for_timeout(150)
                assert root.locator("#start").is_disabled()
                assert len(posts) == 1  # The tool never sends; this is the explicit fixture click.
                context.close()
                return
            assert "익명 확인 완료" in root.locator("#status").inner_text()
            page.locator("#manual").click()
            page.wait_for_function("() => !document.querySelector('#padlet-anonymous-sender').shadowRoot.querySelector('#start').disabled")
            assert root.locator("#subject").input_value() == "Sample"
            assert root.locator("#body").input_value() == "Original body"
            root.locator("#subject").fill("Edited")
            root.locator("#limit").fill("1")
            root.locator("#delay").fill("0")
            if kind == "identity-change":
                state["user"]["registered_at"] = "2026-01-01"
            if kind == "author-change":
                state["user"]["id"] = 12
            root.locator("#start").click()
            page.wait_for_function("() => document.querySelector('#padlet-anonymous-sender').shadowRoot.querySelector('#stop').disabled")
            # The stop button is disabled again only after the finished/error state.
            page.wait_for_function("() => !document.querySelector('#padlet-anonymous-sender').shadowRoot.querySelector('#capture').disabled")
            if kind in {"identity-change", "author-change"}:
                assert len(posts) == 1
                assert root.locator("#start").is_disabled()
            elif kind in {"challenge", "unconfirmed"}:
                assert len(posts) <= 3 and root.locator("#start").is_disabled()
                assert "중지" in root.locator("#status").inner_text()
            else:
                assert len(posts) == (4 if kind == "rate-limit" else 3)
                assert "성공 2건" in root.locator("#status").inner_text()
                assert attempts[1] == 1
                for post in posts[1:]:
                    assert post["subject"] == "Edited" and post["body"] == PAYLOAD["body"]
                    assert post["custom_properties"] == PAYLOAD["custom_properties"]
                    assert post["attachment"] == PAYLOAD["attachment"] and post["author_id"] == 11
                    assert post["cid"].startswith("c_new")
            assert not errors, errors
            if kind == "success" and (ROOT / ".local").is_dir():
                page.screenshot(path=str(ROOT / ".local/anonymous-panel.png"))
            root.locator("#close").click()
            assert root.count() == 0
            context.close()

        for kind in ["success", "xhr", "logged-in", "unknown-identity", "identity-change", "author-change",
                     "challenge", "unconfirmed", "rate-limit"]:
            scenario(kind)
            print(kind + ": passed", flush=True)
        browser.close()
        print(json.dumps({"anonymous_scenarios": 9, "real_posts": 0, "remote_server_requests": 0}))


if __name__ == "__main__":
    main()
