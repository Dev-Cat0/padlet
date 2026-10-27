"""Same-page transport; bot deletion credentials stay in server memory."""
import asyncio
import ipaddress
import json
import re
import socket
import time
from contextlib import suppress
from urllib.parse import urlencode, urlsplit

from playwright.async_api import async_playwright, TimeoutError as BrowserTimeoutError

from .anonymous import padlet_url

VIEWPORT = {"width": 1100, "height": 720}
ALLOWED_DOMAINS = ("padlet.com", "padlet.net", "padlet.pics", "padletcdn.com",
                   "padletusercontent.com", "cloudflare.com", "google.com",
                   "gstatic.com", "googleapis.com")


def capture_url(url):
    parsed = urlsplit(url)
    return (parsed.scheme == "https" and parsed.hostname in {"padlet.com", "www.padlet.com"}
            and parsed.port in {None, 443} and not parsed.username and not parsed.password
            and parsed.path == "/api/9/wishes")


def created_post(data):
    if not isinstance(data, dict) or data.get("errors") or data.get("error"):
        return False
    post = data.get("data")
    if not isinstance(post, dict):
        return False
    attrs = post.get("attributes")
    return bool(post.get("id") or isinstance(attrs, dict) and attrs.get("id"))


def deletion_hashid(data):
    """Only use the hashid returned by creation, never a board/list supplied ID."""
    if not created_post(data):
        return None
    post = data["data"]
    attrs = post.get("attributes")
    value = attrs.get("hashid") if isinstance(attrs, dict) else None
    value = value or post.get("hashid")
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) else None


class BrowserResponse:
    def __init__(self, result):
        self.status_code = result["status"]
        self.text = result["text"]
        self.content = self.text.encode()
        self.headers = result["headers"]

    def json(self):
        return json.loads(self.text)


class BrowserHTTP:
    """Run fetch inside the captured page, with its own current CSRF and cookies."""
    def __init__(self, page, endpoint):
        self.page, self.endpoint = page, endpoint
        self.page_url = page.url
        parsed = urlsplit(page.url)
        self.origin = f"{parsed.scheme}://{parsed.netloc}"

    async def request(self, method, url, payload=None, params=None, timeout=15):
        if params:
            url += "?" + urlencode(params)
        if self.page.is_closed() or self.page.url != self.page_url:
            raise ValueError("캡처한 브라우저 페이지가 변경되었습니다. 다시 캡처하세요.")
        parsed = urlsplit(url)
        if (f"{parsed.scheme}://{parsed.netloc}" != self.origin or parsed.username
                or parsed.password or method not in {"GET", "POST", "DELETE"}
                or method == "POST" and url not in {self.endpoint, self.origin + "/api/8/comments"}
                or method == "DELETE" and not (
                    re.fullmatch(re.escape(self.endpoint) + r"/[A-Za-z0-9_-]{1,128}", url)
                    or re.fullmatch(re.escape(self.origin) + r"/api/5/comments/[1-9][0-9]{0,19}", url))):
            raise ValueError("캡처한 게시판과 다른 주소로 전송할 수 없습니다.")
        result = await self.page.evaluate("""async ({method, url, payload, timeout, pageUrl}) => {
            if (location.href !== pageUrl || new URL(url).origin !== location.origin)
                throw new Error('Captured page changed');
            const headers = {'Accept':'application/json, application/vnd.api+json'};
            const csrf = document.querySelector('meta[name="csrf-token"]')?.content;
            if (csrf) headers['X-CSRF-Token'] = csrf;
            if (window.ww?.uid) headers['X-UID'] = String(window.ww.uid);
            if (method === 'POST') headers['Content-Type'] = 'application/json; charset=utf-8';
            const response = await fetch(url, {method, headers, credentials:'same-origin',
                redirect:'error', cache:'no-store', signal:AbortSignal.timeout(timeout),
                body:method === 'POST' ? JSON.stringify(payload) : undefined});
            const reader = response.body?.getReader();
            const chunks = []; let size = 0;
            if (reader) {
                for (;;) {
                    const {value, done} = await reader.read();
                    if (done) break;
                    size += value.length;
                    if (size > 3000000) { await reader.cancel(); throw new Error('Response too large'); }
                    chunks.push(value);
                }
            }
            const bytes = new Uint8Array(size); let offset = 0;
            for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
            return {status:response.status, text:new TextDecoder().decode(bytes),
                headers:{'Retry-After':response.headers.get('retry-after') || '',
                         'cf-mitigated':response.headers.get('cf-mitigated') || ''}};
        }""", {"method": method, "url": url, "payload": payload, "timeout": int(timeout * 1000),
               "pageUrl": self.page_url})
        return BrowserResponse(result)

    async def get(self, url, **kwargs):
        return await self.request("GET", url, params=kwargs.get("params"), timeout=kwargs.get("timeout", 15))

    async def post(self, url, **kwargs):
        return await self.request("POST", url, payload=kwargs["json"], timeout=kwargs.get("timeout", 10))

    async def delete(self, url, **kwargs):
        return await self.request("DELETE", url, timeout=kwargs.get("timeout", 10))

    async def close(self):
        pass  # BrowserSession owns the context and closes it on reset/expiry.


class BrowserSession:
    def __init__(self, sender):
        self.sender = sender
        self.playwright = self.browser = self.context = self.page = None
        self.tasks = set()
        self.busy = self.closed = False
        self.controls = asyncio.Lock()
        self.dns = {}
        self.capture_enabled = True

    async def public_url(self, value):
        try:
            parsed = urlsplit(value)
            host = parsed.hostname or ""
            if (parsed.scheme not in {"https", "http", "wss"} or parsed.port not in {None, 80, 443}
                    or parsed.username or parsed.password
                    or not any(host == domain or host.endswith("." + domain) for domain in ALLOWED_DOMAINS)):
                return False
            cached = self.dns.get(host)
            if cached and cached[0] > time.monotonic():
                return cached[1]
            addresses = await asyncio.to_thread(socket.getaddrinfo, host, None)
            allowed = bool(addresses) and all(ipaddress.ip_address(row[4][0]).is_global for row in addresses)
            self.dns[host] = (time.monotonic() + 60, allowed)
            return allowed
        except (OSError, ValueError):
            return False

    async def route(self, route):
        if not self.capture_enabled and route.request.resource_type in {"image", "media", "font", "stylesheet"}:
            return await route.abort()
        if await self.public_url(route.request.url):
            await route.continue_()
        else:
            await route.abort()

    async def websocket(self, route):
        # Automatic sending never needs the board's live feed.
        if not self.capture_enabled:
            await route.close()
        elif await self.public_url(route.url):
            route.connect_to_server()
        else:
            await route.close()

    async def initialize(self, storage_state=None):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
        self.context = await self.browser.new_context(viewport=VIEWPORT, locale="ko-KR",
            service_workers="block", accept_downloads=False, storage_state=storage_state)
        self.context.set_default_timeout(10000)
        await self.context.route("**/*", self.route)
        await self.context.route_web_socket("**/*", self.websocket)
        self.context.on("page", self.select_page)
        self.context.on("response", self.observe)

    async def open(self, url, password=None):
        await self.initialize()
        self.page = await self.context.new_page()
        # Deferred app bundles can stall DOMContentLoaded after the inline guest
        # bootstrap is already ready. Automatic jobs only need that bootstrap.
        self.sender.log("게시판 접속 · 익명 세션 정보를 확인합니다.")
        try:
            response = await self.page.goto(padlet_url(url),
                wait_until="domcontentloaded" if self.capture_enabled else "commit", timeout=40000)
        except BrowserTimeoutError:
            raise ValueError("게시판 접속 시간이 초과되었습니다 (40초). 서버에서 Padlet 페이지를 불러오지 못했습니다.") from None
        if response and response.headers.get("cf-mitigated") == "challenge":
            raise ValueError("Padlet이 보안 확인을 요구합니다. 자동 전송을 중단했습니다.")
        if response and response.status in {401, 403} and password is None:
            raise ValueError("Padlet이 접속을 거부했습니다. 게시판 비밀번호 또는 보안 확인이 필요합니다.")
        if response and response.status >= 400 and not (password is not None and response.status in {401, 403}):
            raise ValueError(f"게시판 페이지 조회 실패 (HTTP {response.status}). 게시판 주소와 접근 권한을 확인하세요.")
        padlet_url(self.page.url)
        if password is not None:
            try:
                await self.unlock(password)
            finally:
                password = None
        if not self.capture_enabled:
            try:
                handle = await self.page.wait_for_function("""() => Boolean(
                    document.querySelector('meta[name="csrf-token"]')?.content && window.ww?.uid ||
                    [...document.querySelectorAll('input[type="password"]')].some(el => el.getClientRects().length)
                )""", timeout=15000)
                await handle.dispose()
                if await self.page.locator('input[type="password"]:visible').count():
                    raise ValueError("비밀번호가 필요한 게시판입니다. 비밀번호 게시판 옵션을 켜고 입력하세요.")
            except BrowserTimeoutError:
                raise ValueError("익명 세션 정보를 확인하지 못했습니다 (15초). 로그인·보안 확인이 필요하거나 페이지 로딩이 지연되고 있습니다.") from None

    async def unlock(self, password):
        """Submit the board's own password form once, preserving guest cookies."""
        field = self.page.locator('input[type="password"]:visible')
        try:
            await field.first.wait_for(state="visible", timeout=12000)
        except BrowserTimeoutError:
            # The option can be left on for a board that is already accessible.
            if await self.page.evaluate("() => Boolean(document.querySelector('meta[name=\"csrf-token\"]')?.content && window.ww?.uid)"):
                return
            raise ValueError("게시판 비밀번호 입력 화면을 확인하지 못했습니다.") from None
        board_url = padlet_url(self.page.url)
        if await field.count() != 1 or await self.page.locator('input[type="email"]:visible, input[autocomplete="username"]:visible').count():
            raise ValueError("게시판 비밀번호 화면이 아닌 계정 로그인 화면에서는 인증하지 않습니다.")
        action = await field.evaluate("el => el.form ? el.form.action : null")
        if action:
            target, board = urlsplit(action), urlsplit(board_url)
            if (target.scheme, target.netloc) != (board.scheme, board.netloc) or target.path.startswith(("/auth/", "/login")):
                raise ValueError("게시판 비밀번호 제출 주소를 확인하지 못했습니다.")
        self.sender.log("게시판 비밀번호를 확인합니다.")
        await field.fill(password)
        password = None
        if action:
            await field.press("Enter")
        else:
            button = self.page.get_by_role("button", name=re.compile(r"^(Go|Submit|Continue|이동|입장|들어가기|확인|계속|제출)$", re.I))
            if await button.count() != 1:
                await field.fill("")
                raise ValueError("게시판 비밀번호 확인 버튼을 찾지 못했습니다.")
            await button.click()
        try:
            handle = await self.page.wait_for_function("""() => Boolean(
                document.querySelector('meta[name="csrf-token"]')?.content && window.ww?.uid &&
                ![...document.querySelectorAll('input[type="password"]')].some(el => el.getClientRects().length)
            )""", timeout=15000)
            await handle.dispose()
        except BrowserTimeoutError:
            raise ValueError("비밀번호가 틀렸거나 게시판 접근이 허용되지 않았습니다. 비밀번호를 확인하세요.") from None
        padlet_url(self.page.url)
        self.sender.log("게시판 비밀번호 확인 완료", "success")

    async def snapshot(self):
        page = self.active_page()
        url = page.url
        padlet_url(url)
        bootstrap = await page.evaluate("""() => ({
            csrf: document.querySelector('meta[name="csrf-token"]')?.content || '',
            uid: window.ww?.uid ? String(window.ww.uid) : ''
        })""")
        if page.url != url:
            raise ValueError("삭제 세션의 게시판 주소가 변경되었습니다.")
        return {"url": url, "bootstrap": bootstrap,
                "storage": {"cookies": await self.context.cookies(), "origins": []}}

    async def restore(self, snapshot):
        """Restore our bot's identity in memory without visiting or scraping the board."""
        self.capture_enabled = False
        await self.initialize(snapshot["storage"])
        self.page = await self.context.new_page()
        async def shell(route):
            await route.fulfill(content_type="text/html",
                body='<meta name="csrf-token" content=""><body></body>')
        await self.page.route("**/*", shell, times=1)
        await self.page.goto(padlet_url(snapshot["url"]), wait_until="domcontentloaded")
        await self.page.evaluate("""({csrf, uid}) => {
            document.querySelector('meta[name="csrf-token"]').content = csrf;
            window.ww = {uid};
        }""", snapshot["bootstrap"])
        return self.page

    def select_page(self, page):
        self.page = page
        page.on("dialog", lambda dialog: dialog.dismiss())

    def active_page(self):
        if self.page and not self.page.is_closed():
            return self.page
        pages = self.context.pages if self.context else []
        if pages:
            self.page = pages[-1]
            return self.page
        raise ValueError("열린 브라우저가 없습니다. 게시판을 다시 여세요.")

    async def sending_page(self):
        """Keep the anonymous context, but retire the board's rendering/app loop."""
        if self.capture_enabled:
            raise ValueError("자동 전송 세션에서만 경량 페이지를 사용할 수 있습니다.")
        source = self.active_page()
        source_url = source.url
        padlet_url(source_url)
        bootstrap = await source.evaluate("""() => ({
            csrf: document.querySelector('meta[name="csrf-token"]')?.content || '',
            uid: window.ww?.uid ? String(window.ww.uid) : ''
        })""")
        if source.url != source_url:
            raise ValueError("준비 중 게시판 주소가 변경되어 중단했습니다.")
        old_pages = list(self.context.pages)
        page = await self.context.new_page()

        async def shell(route):
            # A local document at the same origin: no second visit to the board.
            await route.fulfill(content_type="text/html", body=(
                '<!doctype html><html><head><meta charset="utf-8">'
                '<meta name="csrf-token" content=""></head><body></body></html>'
            ))

        try:
            await page.route("**/*", shell, times=1)
            await page.goto(source_url, wait_until="domcontentloaded", timeout=10000)
            await page.evaluate("""({csrf, uid}) => {
                document.querySelector('meta[name="csrf-token"]').content = csrf;
                window.ww = {uid};
            }""", bootstrap)
            if page.url != source_url:
                raise ValueError("전송 페이지의 주소를 확인할 수 없습니다.")
            for old in old_pages:
                await old.close()
            self.page = page
            return page
        except BaseException:
            await page.close()
            raise

    def observe(self, response):
        if (not self.capture_enabled or self.closed or self.busy or self.sender.ready or self.sender.running
                or response.request.method != "POST" or not capture_url(response.url)):
            return
        task = asyncio.create_task(self.capture(response))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def capture(self, response):
        if self.closed or self.busy or self.sender.ready or self.sender.running:
            return
        if response.status not in {200, 201}:
            self.sender.log(f"수동 게시 HTTP {response.status} · 성공한 게시 요청을 기다립니다.", "warning")
            return
        self.busy = True
        try:
            payload = json.loads(response.request.post_data or "")
            result = await response.json()
            if not isinstance(payload, dict) or not created_post(result):
                raise ValueError("게시 성공 응답을 확인하지 못했습니다.")
            page = response.request.frame.page
            padlet_url(page.url)
            if self.closed:
                return
            await self.sender.capture_browser(self, page, response.url, payload)
        except Exception as error:
            if not self.closed:
                message = str(error) if isinstance(error, ValueError) else "게시 요청을 확인하지 못했습니다."
                self.sender.log(message + " 브라우저 상태를 확인하고 다시 게시하세요.", "error")
        finally:
            self.busy = False

    async def screenshot(self):
        return await self.active_page().screenshot(type="jpeg", quality=60, timeout=10000)

    def tabs(self):
        return [{"index": index, "host": urlsplit(page.url).hostname or "새 탭", "active": page == self.page}
                for index, page in enumerate(self.context.pages)]

    async def control(self, action):
        async with self.controls:
            if self.sender.deleting or self.sender.running or self.sender.ready or self.sender.opening or self.busy:
                raise ValueError("전송 준비 중이거나 캡처가 완료되어 브라우저 조작이 잠겨 있습니다.")
            page = self.active_page()
            self.sender.last_activity = time.monotonic()
            if action.kind == "click":
                await page.mouse.click(action.x, action.y)
            elif action.kind == "text":
                await page.keyboard.insert_text(action.text)
            elif action.kind == "key":
                await page.keyboard.press(action.key)
            elif action.kind == "scroll":
                await page.mouse.wheel(0, action.delta)
            elif action.kind == "reload":
                await page.reload(wait_until="domcontentloaded", timeout=15000)
            elif action.kind == "tab":
                if action.index >= len(self.context.pages):
                    raise ValueError("탭이 닫혔습니다.")
                self.page = self.context.pages[action.index]

    async def close(self):
        self.closed = True
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.browser:
            with suppress(Exception):
                await self.browser.close()
        if self.playwright:
            with suppress(Exception):
                await self.playwright.stop()
        self.page = self.context = self.browser = self.playwright = None
