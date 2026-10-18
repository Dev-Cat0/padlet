"""Memory-only browser capture and sender, adapted from the supplied padlet.py."""
import asyncio
import copy
import html
import ipaddress
import json
import random
import socket
import time
from collections import deque
from datetime import datetime, timezone
from urllib.parse import urlsplit

from curl_cffi.requests import AsyncSession
from playwright.async_api import async_playwright


def padlet_url(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname not in {"padlet.com", "www.padlet.com"}
            or parsed.username or parsed.password or parsed.port not in {None, 443}):
        raise ValueError("https://padlet.com/ 으로 시작하는 게시판 주소를 입력하세요.")
    return value


def is_capture_url(value: str) -> bool:
    try:
        padlet_url(value)
        return urlsplit(value).path.rstrip("/") == "/api/9/wishes"
    except ValueError:
        return False


def make_payload(template: dict, subject: str, body: str) -> dict:
    payload = copy.deepcopy(template)
    payload["subject"] = subject
    payload["body"] = "<p>" + html.escape(body).replace("\n", "<br>") + "</p>"
    payload["cid"] = f"c_new{random.randint(10000, 99999)}"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    payload.update(created_at=now, updated_at=now)
    return payload


class Sender:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.browser_lock = asyncio.Lock()
        self.stop_event = asyncio.Event()
        self.logs = deque(maxlen=300)
        self.sequence = 0
        self.playwright = self.browser = self.context = self.page = None
        self.worker = None
        self.capture_tasks = set()
        self.generation = 0
        self.capture_busy = False
        self.endpoint = ""
        self.headers = {}
        self.template = {}
        self.ready = self.running = self.opening = False
        self.sent = self.errors = 0
        self.limit = 0
        self.phase = "waiting"
        self.last_activity = time.monotonic()
        self.dns_cache = {}
        self.dns_pending = {}
        self.stream_socket = None

    def log(self, message, level="info"):
        self.sequence += 1
        self.logs.append({"id": self.sequence, "time": datetime.now(timezone.utc).isoformat(),
                          "level": level, "message": message})

    def status(self):
        return {"ready": self.ready, "running": self.running, "opening": self.opening,
                "browser": self.page is not None and not self.page.is_closed(),
                "phase": self.phase, "sent": self.sent, "errors": self.errors,
                "limit": self.limit, "logs": list(self.logs)}

    async def close_browser(self):
        async with self.browser_lock:
            if self.browser:
                await self.browser.close()
            if self.playwright:
                await self.playwright.stop()
            self.playwright = self.browser = self.context = self.page = None

    async def route(self, route):
        # Remote browsing must not expose the Render private network or metadata.
        parsed = urlsplit(route.request.url)
        if parsed.scheme in {"data", "blob", "about"}:
            return await route.continue_()
        try:
            if parsed.scheme not in {"https", "http"} or parsed.port not in {None, 80, 443}:
                return await route.abort()
            if not await self.public_host(parsed.hostname):
                return await route.abort()
            await route.continue_()
        except (OSError, ValueError):
            await route.abort()

    async def public_host(self, hostname):
        cached = self.dns_cache.get(hostname)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        if hostname not in self.dns_pending:
            self.dns_pending[hostname] = asyncio.create_task(
                asyncio.to_thread(socket.getaddrinfo, hostname, None))
        task = self.dns_pending[hostname]
        try:
            addresses = await asyncio.shield(task)
            public = bool(addresses) and all(ipaddress.ip_address(a[4][0]).is_global for a in addresses)
            if len(self.dns_cache) >= 256:
                self.dns_cache.clear()
            self.dns_cache[hostname] = (time.monotonic() + 60, public)
            return public
        finally:
            if task.done():
                self.dns_pending.pop(hostname, None)

    def observe(self, response):
        if response.request.method != "POST" or not is_capture_url(response.url):
            return
        task = asyncio.create_task(self.capture(response, self.generation))
        self.capture_tasks.add(task)
        task.add_done_callback(self.capture_tasks.discard)

    async def capture(self, response, generation):
        request = response.request
        if (self.ready or self.capture_busy or generation != self.generation
                or request.method != "POST" or not is_capture_url(response.url)):
            return
        if response.status not in {200, 201}:
            self.log(f"게시 요청 HTTP {response.status}: 성공한 게시 요청을 기다립니다.", "warning")
            return
        self.capture_busy = True
        try:
            payload = json.loads(request.post_data or "")
            if not isinstance(payload, dict) or not payload:
                raise ValueError("Invalid payload")
            headers = await request.all_headers()
            if generation != self.generation:
                return
            for name in ("content-length", "host", "content-type", "connection", "accept-encoding"):
                headers.pop(name, None)
            self.endpoint, self.headers, self.template = response.url, headers, payload
            self.ready, self.phase = True, "ready"
            self.log("게시 요청 캡처 완료. 전송을 시작할 수 있습니다.", "success")
            # Same as the original: release Chromium once a successful post is captured.
            await self.close_browser()
        except Exception:
            if generation == self.generation:
                self.log("게시 요청을 읽지 못했습니다. 게시판에서 다시 게시해 주세요.", "error")
        finally:
            self.capture_busy = False

    def select_page(self, page):
        self.page = page
        page.on("dialog", lambda dialog: dialog.dismiss())

    async def open(self, url):
        padlet_url(url)
        async with self.lock:
            if self.running or self.opening:
                raise ValueError("진행 중인 작업을 먼저 중지하세요.")
            self.generation += 1
            self.ready, self.opening, self.phase = False, True, "opening"
            self.endpoint, self.headers, self.template = "", {}, {}
        try:
            await self.close_browser()
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(
                headless=True, args=["--disable-dev-shm-usage"])
            self.context = await self.browser.new_context(
                viewport={"width": 1100, "height": 720}, locale="ko-KR", service_workers="block",
                reduced_motion="reduce")
            await self.context.route("**/*", self.route)
            self.context.on("response", self.observe)
            self.context.on("page", self.select_page)
            self.page = await self.context.new_page()
            await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
            if not self.ready:
                self.phase = "capturing"
                self.log("아래 브라우저에서 로그인 후 게시물을 한 번 작성하세요.")
            self.last_activity = time.monotonic()
        except Exception:
            await self.close_browser()
            self.phase = "error"
            self.log("브라우저를 열지 못했습니다. 주소와 서버 상태를 확인하세요.", "error")
            raise ValueError("브라우저 실행 실패. Render 로그를 확인하거나 다시 시도하세요.") from None
        finally:
            self.opening = False

    def active_page(self):
        if self.page and not self.page.is_closed():
            return self.page
        pages = self.context.pages if self.context else []
        if pages:
            self.page = pages[-1]
            return self.page
        raise ValueError("열린 브라우저가 없습니다.")

    async def screenshot(self):
        # Image encoding must never hold the input/close lock.
        page = self.active_page()
        return await page.screenshot(type="jpeg", quality=50, timeout=10000)

    async def control(self, action):
        async with self.browser_lock:
            page = self.active_page()
            self.last_activity = time.monotonic()
            if action.kind == "click":
                await page.mouse.click(action.x, action.y)
            elif action.kind == "text":
                await page.keyboard.insert_text(action.text)
            elif action.kind == "key":
                await page.keyboard.press(action.key)
            elif action.kind == "scroll":
                await page.mouse.wheel(0, action.delta)
            elif action.kind == "tab":
                pages = self.context.pages
                if action.index >= len(pages):
                    raise ValueError("탭이 닫혔습니다.")
                self.page = pages[action.index]

    async def start(self, subject, body, limit, delay):
        async with self.lock:
            if not self.ready:
                raise ValueError("먼저 게시 요청을 캡처하세요.")
            if self.running:
                raise ValueError("이미 전송 중입니다.")
            self.stop_event.clear()
            self.sent = self.errors = 0
            self.limit = limit
            self.running, self.phase = True, "sending"
            self.log(f"전송 시작 · 횟수 {'무제한' if limit == 0 else limit} · 간격 {delay}초")
            self.worker = asyncio.create_task(self.run(subject, body, limit, delay))

    async def wait(self, seconds):
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=seconds)
            return True
        except TimeoutError:
            return False

    async def run(self, subject, body, limit, delay):
        backoff = 0
        try:
            async with AsyncSession(impersonate="chrome120", timeout=10) as session:
                while not self.stop_event.is_set() and (limit == 0 or self.sent < limit):
                    if await self.wait(delay + random.uniform(0, 0.2) + backoff):
                        break
                    try:
                        response = await session.post(self.endpoint,
                            json=make_payload(self.template, subject, body), headers=self.headers,
                            allow_redirects=False)
                    except Exception:
                        self.errors += 1
                        self.phase = "error"
                        self.log("네트워크 오류로 중지했습니다. 마지막 게시 성공 여부를 확인한 후 다시 시작하세요.", "error")
                        # A timed-out POST may already have succeeded; do not retry blindly.
                        break
                    status = response.status_code
                    if status in {200, 201}:
                        self.sent += 1
                        backoff = 0
                        self.phase = "sending"
                        self.log(f"전송 성공 #{self.sent}", "success")
                    elif status == 429:
                        self.errors += 1
                        backoff = min(backoff + 3, 30)
                        try:
                            backoff = max(backoff, min(float(response.headers.get("Retry-After", 0)), 3600))
                        except ValueError:
                            pass
                        self.phase = "backoff"
                        self.log(f"429 요청 제한 · {backoff:g}초 대기", "warning")
                    else:
                        self.errors += 1
                        self.phase = "error"
                        self.log(f"HTTP {status} 오류로 중지했습니다. 로그인·게시 권한을 확인하고 다시 캡처하세요.", "error")
                        if status in {401, 403}:
                            self.ready = False
                        break
                if self.stop_event.is_set():
                    self.phase = "stopped"
                    self.log("사용자가 전송을 중지했습니다.")
                elif self.phase != "error":
                    self.phase = "finished"
                    self.log(f"전송 완료 · 성공 {self.sent} / 오류 {self.errors}", "success")
        except asyncio.CancelledError:
            self.phase = "stopped"
            raise
        except Exception:
            self.phase = "error"
            self.log("전송 작업 오류. 다시 캡처한 후 시도하세요.", "error")
        finally:
            self.running = False
            self.last_activity = time.monotonic()

    def stop(self):
        self.stop_event.set()
        if self.running:
            self.phase = "stopping"

    async def reset(self):
        async with self.lock:
            if self.opening:
                raise ValueError("브라우저 실행이 끝난 후 다시 시도하세요.")
            self.stop()
            if self.worker and not self.worker.done():
                await self.worker
            self.generation += 1
            await self.close_browser()
            self.endpoint, self.headers, self.template = "", {}, {}
            self.ready, self.phase = False, "waiting"
            self.log("로그인 세션과 캡처 정보를 삭제했습니다.")

    async def expire(self):
        while True:
            await asyncio.sleep(30)
            if not self.running and not self.opening and time.monotonic() - self.last_activity > 1800:
                if self.browser or self.ready:
                    await self.reset()
                    self.log("30분 동안 조작이 없어 세션을 종료했습니다.")

    async def shutdown(self):
        self.stop()
        if self.worker and not self.worker.done():
            await self.worker
        self.generation += 1
        for task in list(self.capture_tasks):
            task.cancel()
        await asyncio.gather(*self.capture_tasks, return_exceptions=True)
        await self.close_browser()
