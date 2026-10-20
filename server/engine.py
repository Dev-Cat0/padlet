"""Memory-only browser capture and sender, preserving the section send loop."""
import asyncio
import copy
import html
import math
import secrets
import time
from collections import deque
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlsplit

from curl_cffi.requests import AsyncSession
from .anonymous import padlet_url, prepare, check_response, read_sections, build_template
from .browser import BrowserSession, BrowserHTTP, created_post
from .errors import rejection_message
from .fields import validate_fields, read_fields


class PlainText(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.parts = []
        self.feed(source)

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"p", "div", "li"}:
            self.parts.append("\n")

    def text(self):
        return "".join(self.parts).rstrip("\n")


def make_payload(template: dict, subject: str, body: str) -> dict:
    payload = copy.deepcopy(template)
    payload["subject"] = subject
    payload["body"] = "<p>" + html.escape(body).replace("\n", "<br>") + "</p>" if body else ""
    payload["cid"] = "c_new" + secrets.token_hex(12)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    payload.update(created_at=now, updated_at=now)
    return payload


class Sender:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.stop_event = asyncio.Event()
        self.logs = deque(maxlen=300)
        self.sequence = 0
        self.worker = self.http = None
        self.browser = None
        self.captured_body = None
        self.endpoint = ""
        self.headers, self.template, self.board = {}, {}, None
        self.ready = self.running = self.opening = False
        self.sent = self.errors = self.limit = self.rounds = 0
        self.target_count = 1
        self.phase = "waiting"
        self.last_activity = time.monotonic()

    def log(self, message, level="info"):
        self.sequence += 1
        self.logs.append({"id": self.sequence, "time": datetime.now(timezone.utc).isoformat(),
                          "level": level, "message": message})

    def status(self):
        return {"ready": self.ready, "running": self.running, "opening": self.opening,
                "browser": self.browser is not None,
                "board": self.board, "phase": self.phase, "sent": self.sent, "errors": self.errors,
                "limit": self.limit, "rounds": self.rounds, "target_count": self.target_count,
                "logs": list(self.logs)}

    async def clear_connection(self):
        self.ready = False
        if self.browser:
            browser, self.browser = self.browser, None
            await browser.close()
        self.captured_body = None
        self.endpoint, self.headers, self.template, self.board = "", {}, {}, None
        if self.http:
            await self.http.close()
            self.http = None

    async def open(self, url):
        url = padlet_url(url)
        async with self.lock:
            if self.running or self.opening:
                raise ValueError("진행 중인 작업이 끝난 후 다시 여세요.")
            self.opening, self.phase = True, "opening"
            try:
                await self.clear_connection()
                self.browser = BrowserSession(self)
                await self.browser.open(url)
                self.phase = "capturing"
                self.log("브라우저에서 필요한 확인을 마친 뒤 글을 1건 게시하세요. 성공한 요청을 캡처합니다.")
            except Exception:
                await self.clear_connection()
                self.phase = "error"
                self.log("브라우저를 열지 못했습니다. 잠시 후 다시 시도하세요.", "error")
                raise ValueError("브라우저를 열지 못했습니다. 잠시 후 다시 시도하세요.") from None
            finally:
                self.opening = False
                self.last_activity = time.monotonic()

    async def capture_browser(self, browser, page, endpoint, payload):
        async with self.lock:
            if self.browser is not browser or self.ready or self.running:
                return
            self.opening = True
            try:
                transport = BrowserHTTP(page, endpoint)
                url = padlet_url(page.url)
                response = await transport.get(transport.origin + "/api/11/padlet-starting-state" + urlsplit(url).path)
                check_response(response, "게시판 상태 조회")
                state = response.json()
                wall = state.get("wall") or {}
                anonymous = build_template(state)
                if payload.get("author_id") != anonymous["author_id"]:
                    raise ValueError("익명 사용자와 게시 요청 작성자가 달라 캡처를 중단했습니다.")
                if (state.get("canIWrite") is not True or state.get("isReadOnly") or wall.get("frozen")
                        or wall.get("is_archived") or wall.get("is_trashed")):
                    raise ValueError("현재 게시판의 쓰기 권한을 확인할 수 없습니다.")
                if type(payload.get("wall_id")) is not int or wall.get("id") != payload["wall_id"]:
                    raise ValueError("성공한 요청과 현재 게시판이 달라 캡처를 중단했습니다.")
                sections = await read_sections(transport, transport.origin, {}, payload["wall_id"])
                fields = await read_fields(transport, transport.origin, {}, payload["wall_id"])
                draft = {key: payload.get(key) or "" for key in ("subject", "attachment")}
                draft["body"] = PlainText(payload.get("body") or "").text()
                draft["custom_properties"] = {str(k): v for k, v in (payload.get("custom_properties") or {}).items()
                                              if isinstance(v, (str, int, float)) or v is None}
                self.http, self.endpoint, self.headers = transport, endpoint, {}
                self.template = copy.deepcopy(payload)
                self.captured_body = draft["body"]
                self.board = {"url": url, "title": str(wall.get("title") or "게시판"),
                    "approval_required": bool(wall.get("require_approval")),
                    "sections": sections, "fields": fields, "draft": draft, "capture_id": secrets.token_hex(8)}
                self.ready, self.phase = True, "ready"
                self.log("성공한 게시 요청 캡처 완료 · 같은 브라우저 세션으로 전송합니다. 방금 게시한 1건은 반복 횟수에 포함되지 않습니다.", "success")
            finally:
                self.opening = False
                self.last_activity = time.monotonic()

    async def connect(self, url):
        url = padlet_url(url)
        async with self.lock:
            if self.running or self.opening:
                raise ValueError("진행 중인 작업이 끝난 후 다시 연결하세요.")
            self.opening, self.phase = True, "opening"
            self.ready = False
        try:
            await self.clear_connection()
            self.http = AsyncSession(impersonate="chrome120", timeout=15)
            async with asyncio.timeout(40):
                self.endpoint, self.headers, self.template, self.board = await prepare(self.http, url)
            self.ready, self.phase = True, "ready"
            self.log("익명 게시판 연결 완료 · 테스트 글을 게시하지 않았습니다.", "success")
            if self.board["approval_required"]:
                self.log("이 게시판은 게시 후 관리자 승인이 필요합니다.", "warning")
        except Exception as error:
            await self.clear_connection()
            self.phase = "error"
            message = str(error) if isinstance(error, ValueError) else "게시판 연결에 실패했습니다. 잠시 후 다시 연결하세요."
            self.log(message, "error")
            raise ValueError(message) from None
        finally:
            self.opening = False
            self.last_activity = time.monotonic()

    async def start(self, subject, body, limit, delay, attachment="", custom_properties=None):
        async with self.lock:
            if not self.ready or self.opening:
                raise ValueError("먼저 브라우저에서 성공한 게시 요청을 캡처하세요.")
            if self.running:
                raise ValueError("이미 전송 중입니다.")
            subject, body, fields = validate_fields((self.board or {}).get("fields"), subject, body,
                                                   attachment, custom_properties or {},
                                                   self.template.get("custom_properties") if self.browser else None)
            self.template.update(fields)
            self.stop_event.clear()
            self.sent = self.errors = self.rounds = 0
            self.limit = limit
            self.target_count = len((self.board or {}).get("sections", [])) or 1
            self.running, self.phase = True, "sending"
            self.log(f"전송 시작 · 대상 {self.target_count}곳 · 섹션당 {'무제한' if limit == 0 else str(limit) + '회'} · 주기 {delay}초")
            self.worker = asyncio.create_task(self.run(subject, body, limit, delay))

    async def wait(self, seconds):
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=seconds)
            return True
        except TimeoutError:
            return False

    async def send_section(self, section, subject, body, semaphore, paused):
        async with semaphore:
            # Never cancel a POST already in flight: its result may be ambiguous.
            if self.stop_event.is_set() or paused.is_set():
                return "pending", 0
            template = copy.deepcopy(self.template)
            if section is not None:
                template["wall_section_id"] = section["id"]
                template.pop("wall_section_hashid", None)
                if section.get("hashid"):
                    template["wall_section_hashid"] = section["hashid"]
            label = section["title"] if section else "게시판"
            try:
                payload = make_payload(template, subject, body)
                if self.browser and body == self.captured_body:
                    payload["body"] = template.get("body", "")
                response = await self.http.post(self.endpoint,
                    json=payload, headers=self.headers,
                    allow_redirects=False, timeout=10)
            except Exception:
                if self.browser:
                    self.ready = False
                self.errors += 1
                paused.set()
                self.log(f"{label}: 네트워크 오류로 중지했습니다. 마지막 게시 성공 여부를 확인하세요.", "error")
                return "error", 0
            status = response.status_code
            challenge = getattr(response, "headers", {}).get("cf-mitigated") == "challenge"
            success = status in {200, 201} and not challenge
            if success and self.browser:
                try:
                    success = created_post(response.json())
                except (ValueError, TypeError):
                    success = False
            if success:
                self.sent += 1
                self.log(f"{label} · 전송 성공 #{self.sent}", "success")
                return "ok", 0
            self.errors += 1
            paused.set()
            if status == 429 and not challenge:
                try:
                    retry = float(response.headers.get("Retry-After", 0))
                    retry = max(0, retry) if math.isfinite(retry) else 0
                except ValueError:
                    retry = 0
                return "limited", retry
            if status in {401, 403} or challenge or status in {200, 201}:
                self.ready = False
            private_values = [value for key, value in self.headers.items()
                              if key.lower() in {"x-csrf-token", "authorization", "cookie", "x-uid"}]
            cookies = getattr(self.http, "cookies", None)
            if cookies is not None:
                private_values.extend(cookie.value for cookie in cookies.jar)
            reason = ("보안 확인이 필요합니다. 브라우저에서 직접 확인 후 다시 캡처하세요." if challenge else
                      "게시 성공 응답을 확인하지 못했습니다. 마지막 게시 여부를 확인하고 다시 캡처하세요."
                      if status in {200, 201} else rejection_message(response, private_values))
            self.log(f"{label}: {reason}", "error")
            return "error", 0

    async def run(self, subject, body, limit, delay):
        # Each round covers every discovered section exactly once. Rounds never overlap.
        targets = copy.deepcopy((self.board or {}).get("sections", [])) or [None]
        semaphore = asyncio.Semaphore(8)
        next_round = time.monotonic() + delay
        try:
            if self.http is None:
                raise ValueError("익명 세션이 없습니다.")
            while not self.stop_event.is_set() and (limit == 0 or self.rounds < limit):
                if await self.wait(max(0, next_round - time.monotonic())):
                    break
                started = time.monotonic()
                pending, backoff = list(targets), 0
                while pending and not self.stop_event.is_set():
                    self.phase = "sending"
                    paused = asyncio.Event()
                    results = await asyncio.gather(*(self.send_section(section, subject, body, semaphore, paused)
                                                     for section in pending))
                    if any(result == "error" for result, _ in results):
                        self.phase = "error"
                        return
                    pending = [section for section, (result, _) in zip(pending, results) if result != "ok"]
                    if pending and not self.stop_event.is_set():
                        backoff = max(min(backoff + 3, 30), max(retry for _, retry in results))
                        self.phase = "backoff"
                        self.log(f"429 요청 제한 · {backoff:g}초 대기 · 미완료 {len(pending)}곳만 재시도", "warning")
                        if await self.wait(backoff):
                            break
                if not pending:
                    self.rounds += 1
                # Response time counts towards the interval; no random extra delay or catch-up bursts.
                next_round = max(started + delay, time.monotonic())
            if self.stop_event.is_set():
                self.phase = "stopped"
                self.log("사용자가 전송을 중지했습니다.")
            else:
                self.phase = "finished"
                self.log(f"전송 완료 · {self.rounds}회 반복 / 성공 {self.sent}건 / 오류 {self.errors}건", "success")
        except asyncio.CancelledError:
            self.phase = "stopped"
            raise
        except Exception:
            self.phase = "error"
            self.log("전송 작업 오류. 게시판에 다시 연결한 후 시도하세요.", "error")
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
                raise ValueError("게시판 연결이 끝난 후 다시 시도하세요.")
            self.stop()
            if self.worker and not self.worker.done():
                await self.worker
            await self.clear_connection()
            self.phase = "waiting"
            self.log("브라우저 세션과 캡처 정보를 삭제했습니다.")

    async def shutdown(self):
        self.stop()
        if self.worker and not self.worker.done():
            await self.worker
        await self.clear_connection()
