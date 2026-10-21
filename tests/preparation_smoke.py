"""Real Chromium regression: a stalled board script must not block preparation."""
import asyncio
import sys
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.browser import BrowserHTTP, BrowserSession
from server.engine import Sender


async def main():
    script_requested = asyncio.Event()
    release_script = asyncio.Event()
    requests = []

    async def route(self, route):
        requests.append(route.request.method)
        path = urlsplit(route.request.url).path
        if path == '/example/board':
            await route.fulfill(content_type='text/html',
                headers={'Set-Cookie': 'guest=fixture; Path=/; HttpOnly; Secure'},
                body='''<meta name="csrf-token" content="fixture">
                    <script>window.ww = {uid: 'fixture-uid'};</script>
                    <script defer src="/slow.js"></script><body>Board</body>''')
        elif path == '/slow.js':
            script_requested.set()
            await release_script.wait()
            await route.abort()
        elif path == '/api/state':
            headers = await route.request.all_headers()
            assert 'guest=fixture' in headers.get('cookie', '')
            assert headers['x-csrf-token'] == 'fixture'
            assert headers['x-uid'] == 'fixture-uid'
            await route.fulfill(json={'ok': True})
        else:
            await route.abort()

    browser = BrowserSession(Sender())
    browser.capture_enabled = False
    with patch.object(BrowserSession, 'route', route):
        try:
            # Launch time is separate from the document-readiness regression.
            await browser.initialize()
            with patch.object(browser, 'initialize', return_value=None):
                await asyncio.wait_for(browser.open('https://padlet.com/example/board'), 3)
            source = browser.page
            await asyncio.wait_for(script_requested.wait(), 1)
            assert await source.evaluate('document.readyState') != 'complete'
            page = await browser.sending_page()
            assert source.is_closed() and len(browser.context.pages) == 1
            transport = BrowserHTTP(page, 'https://padlet.com/api/9/wishes')
            assert (await transport.get('https://padlet.com/api/state')).json() == {'ok': True}
            assert set(requests) == {'GET'}
            print('PASS: stalled script, preserved anonymous session, GET only, no real posts')
        finally:
            release_script.set()
            await browser.close()


if __name__ == '__main__':
    asyncio.run(main())
