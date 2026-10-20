"""All requests are fulfilled in-process; never posts to an actual board."""
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import patch

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))

from server.browser import BrowserSession
from server.engine import Sender
from server.app import BrowserAction

SAMPLE = {"wall_id": 12, "wall_hashid": "board", "author_id": 31, "author_hashid": "anon",
          "subject": "Sample", "body": "<p><strong>Rich body</strong></p>",
          "attachment": "https://example.com/photo.png", "published": False,
          "custom_properties": {"photo": {"asset_id": 42}, "answer": "Captured answer"}}
HTML = """<!doctype html><html><head><meta name="csrf-token" content="fixture-csrf"></head>
<body><input id="field"><button id="post">Publish sample</button><script>
document.querySelector('#post').onclick=async()=>fetch('/api/9/wishes',{
method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':'fixture-csrf'},
body:JSON.stringify(SAMPLE)});
</script></body></html>""".replace("JSON.stringify(SAMPLE)", "JSON.stringify(" + json.dumps(SAMPLE) + ")")


async def main():
    posts = []
    challenge = False

    async def route(self, route):
        path = route.request.url.split("padlet.com", 1)[-1].split("?", 1)[0]
        if path == "/example/board":
            return await route.fulfill(content_type="text/html", body=HTML,
                headers={"Set-Cookie": "fixture-session=valid; Path=/; HttpOnly; Secure; SameSite=Lax"})
        if path == "/api/9/wishes":
            posts.append(route.request.post_data_json)
            headers = await route.request.all_headers()
            assert headers["x-csrf-token"] == "fixture-csrf"
            assert "fixture-session=valid" in headers.get("cookie", "")
            if challenge:
                return await route.fulfill(status=403, headers={"cf-mitigated": "challenge"},
                                           content_type="text/html", body="Human verification")
            return await route.fulfill(status=201, json={"data": {"id": len(posts)}})
        if path.startswith("/api/11/padlet-starting-state"):
            return await route.fulfill(json={"wall": {"id": 12, "title": "Fixture", "require_approval": True},
                                             "canIWrite": True})
        if path == "/api/5/wall_sections":
            return await route.fulfill(json={"data": []})
        if path.endswith("/post-properties"):
            return await route.fulfill(json={"data": {"attributes": {"required_fields": {"body": True}}}})
        if path.endswith("/custom-post-properties"):
            return await route.fulfill(json={"data": [
                {"attributes": {"id": "photo", "name": "Photo", "data_type": "image", "required": True}},
                {"attributes": {"id": "answer", "name": "Answer", "data_type": "text", "required": True}}]})
        await route.abort()

    sender = Sender()
    with patch.object(BrowserSession, "route", route):
        try:
            await sender.open("https://padlet.com/example/board")
            browser = sender.browser
            await browser.page.locator("#field").click()
            await browser.control(BrowserAction(kind="text", text="한글 입력"))
            assert await browser.page.locator("#field").input_value() == "한글 입력"
            assert len(await browser.screenshot()) > 1000
            await browser.page.locator("#post").click()
            async with asyncio.timeout(10):
                while not sender.ready:
                    await asyncio.sleep(.05)
            assert sender.browser is browser and not browser.page.is_closed()
            assert sender.sent == 0 and len(posts) == 1
            assert "fixture-csrf" not in json.dumps(sender.status())
            await sender.start("Edited", "Rich body", 1, 0, SAMPLE["attachment"],
                               {"answer": "Edited answer"})
            await sender.worker
            assert sender.sent == 1 and len(posts) == 2
            assert posts[-1]["subject"] == "Edited"
            assert posts[-1]["body"] == SAMPLE["body"]
            assert posts[-1]["custom_properties"] == {"photo": {"asset_id": 42}, "answer": "Edited answer"}
            assert posts[-1]["published"] is False
            assert posts[-1]["cid"].startswith("c_new")
            challenge = True
            await sender.start("Edited", "Rich body", 5, 0, SAMPLE["attachment"], {"answer": "Edited answer"})
            await sender.worker
            assert len(posts) == 3 and sender.sent == 0 and sender.errors == 1 and not sender.ready
            assert sender.browser is browser
            await sender.reset()
            assert sender.browser is None and browser.closed
            print(json.dumps({"browser_capture": "passed", "same_session_cookie": True,
                              "mock_posts": len(posts), "real_posts": 0, "challenge_stopped": True}))
        finally:
            await sender.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
