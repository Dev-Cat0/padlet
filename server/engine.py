"""Memory-only browser capture and sender, preserving the section send loop."""
import asyncio
import copy
import html
import logging
import math
import secrets
import time
from collections import deque
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlsplit

from curl_cffi.requests import AsyncSession
from playwright.async_api import Error as BrowserError, TimeoutError as BrowserTimeoutError
from .anonymous import padlet_url, prepare, check_response, read_sections, build_template
from .browser import BrowserSession, BrowserHTTP, created_post, deletion_hashid
from .errors import rejection_message
from .fields import validate_fields, read_fields
from .comments import CommentOptions, CommentSender, post_id

MAX_INFLIGHT_REQUESTS = 32
logger = logging.getLogger(__name__)


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
        self.automatic = False
        self.captured_body = None
        self.endpoint = ""
        self.headers, self.template, self.board = {}, {}, None
        self.ready = self.running = self.opening = False
        self.sent = self.errors = self.limit = self.rounds = 0
        self.target_count = 1
        self.phase = "waiting"
        self.last_activity = time.monotonic()
        self.deletion_batches = []
        self.active_batch = None
        self.deleting = False
        self.delete_worker = None
        self.deleted = self.delete_errors = self.untracked = 0
        self.delete_retry_at = 0
        self.closing = False
        self.license_expires = None
        self.commenter = None
        self.comments_sent = self.comments_errors = self.comments_targets = 0
        self.execution_mode = "pending"
        self.comments_total = 0
        self.posts_phase = "disabled"
        self.posts_active = False

    def log(self, message, level="info"):
        self.sequence += 1
        self.logs.append({"id": self.sequence, "time": datetime.now(timezone.utc).isoformat(),
                          "level": level, "message": message})

    def status(self):
        return {"ready": self.ready, "running": self.running, "opening": self.opening,
                "browser": self.browser is not None,
                "board": self.board, "phase": "deleting" if self.deleting else self.phase,
                "sent": self.sent, "errors": self.errors,
                "comments_sent": self.comments_sent, "comments_errors": self.comments_errors,
                "posts_phase": self.phase if self.posts_active else self.posts_phase,
                "comments_phase": self.commenter.phase if self.commenter else "disabled",
                "comments_targets": self.comments_targets,
                "execution_mode": self.execution_mode, "comments_total": self.comments_total,
                "limit": self.limit, "rounds": self.rounds, "target_count": self.target_count,
                "deleting": self.deleting, "delete_pending": sum(len(b["posts"]) + len(b.get("comments", ())) for b in self.deletion_batches),
                "deleted": self.deleted, "delete_errors": self.delete_errors, "delete_untracked": self.untracked,
                "delete_retry_after": max(0, math.ceil(self.delete_retry_at - time.monotonic())),
                "logs": list(self.logs)}

    async def clear_connection(self):
        self.ready = False
        if self.active_batch:
            batch, self.active_batch = self.active_batch, None
            try:
                batch["snapshot"] = await self.browser.snapshot()
            except Exception:
                self.log("삭제용 익명 세션을 보관하지 못했습니다. 해당 게시물은 자동 삭제할 수 없습니다.", "error")
        if self.browser:
            browser, self.browser = self.browser, None
            await browser.close()
        self.captured_body = None
        self.endpoint, self.headers, self.template, self.board = "", {}, {}, None
        if self.http:
            await self.http.close()
            self.http = None

    async def queue_run(self, url, subject, body, limit, delay, before_start=None, comments=None):
        """Accept one job promptly; all preparation happens in a fresh bot context."""
        url = padlet_url(url)
        options = CommentOptions.model_validate(comments or {})
        async with self.lock:
            if self.deleting or self.running or self.opening or self.worker and not self.worker.done():
                raise ValueError("이미 진행 중인 작업이 있습니다.")
            if before_start:
                await before_start()
            self.stop_event.clear()
            self.sent = self.errors = self.rounds = 0
            self.comments_sent = self.comments_errors = self.comments_targets = 0
            self.execution_mode, self.comments_total = "pending", 0
            self.posts_phase, self.posts_active = "preparing", False
            self.commenter = CommentSender(self, options) if options.enabled else None
            self.limit, self.target_count = limit, 1
            self.automatic, self.opening, self.phase = True, True, "preparing"
            self.ready = False
            self.log("작업 접수 · 봇 전용 익명 세션을 준비합니다.")
            self.worker = asyncio.create_task(self.automatic_run(url, subject, body, limit, delay))
        await asyncio.sleep(0)  # Enter the task's cleanup scope before it can be stopped.

    async def automatic_run(self, url, subject, body, limit, delay):
        stage = "브라우저 시작·게시판 접속"
        comment_task = None
        try:
            await self.clear_connection()
            self.browser = BrowserSession(self)
            self.browser.capture_enabled = False
            async with asyncio.timeout(75):
                await self.browser.open(url)
                stage = "경량 페이지 준비"
                page = await self.browser.sending_page()
                self.log("경량 전송 준비 · 게시판 화면과 실시간 갱신을 종료했습니다.")
                board_url = padlet_url(page.url)
                endpoint = "https://" + urlsplit(board_url).hostname + "/api/9/wishes"
                transport = BrowserHTTP(page, endpoint)
                stage = "익명 상태 조회"
                response = await transport.get(transport.origin + "/api/11/padlet-starting-state" + urlsplit(board_url).path)
                check_response(response, "익명 상태 조회")
                state = response.json()
                if (state.get("user") or {}).get("registered_at", "unknown") is not None:
                    raise ValueError("봇의 익명 상태를 확인할 수 없어 중단했습니다.")
                # Validate guest identity and frozen/archived state independently
                # from post permission: a commenter may not be allowed to post.
                template = build_template(state, require_write=False)
                can_post = state.get("canIWrite") is True and not state.get("isReadOnly")
                can_comment = self.commenter is not None and state.get("canIComment") is True
                if not can_post and not can_comment:
                    self.execution_mode = "none"
                    raise ValueError("실행 가능한 익명 권한이 없습니다. 게시글·댓글 권한과 댓글 옵션을 확인하세요.")
                if self.commenter and not can_comment:
                    self.commenter = None
                    self.log("익명 댓글이 막혀 있어 게시글만 전송합니다.", "warning")
                self.execution_mode = "both" if can_post and can_comment else "posts" if can_post else "comments"
                if not can_post:
                    self.log("익명 게시글 작성이 막혀 있어 기존 게시글에 댓글만 작성합니다.", "warning")
                sections, fields = [], {}
                if can_post:
                    try:
                        stage = "섹션 목록 조회"
                        sections = await read_sections(transport, transport.origin, {}, template["wall_id"])
                        stage = "필수 입력 항목 조회"
                        fields = await read_fields(transport, transport.origin, {}, template["wall_id"])
                        subject, body, extra = validate_fields(fields, subject, body, "", {})
                        template.update(extra)
                    except Exception:
                        if not can_comment:
                            raise
                        can_post = False
                        self.posts_phase = "error"
                        self.execution_mode = "comments"
                        self.errors += 1
                        self.log("게시글 준비에 실패해 게시글만 중단합니다. 댓글 작업은 계속됩니다.", "error")
                self.http, self.endpoint, self.template = transport, endpoint, template
                self.board = {"url": board_url, "title": str(state["wall"].get("title") or "게시판"),
                    "approval_required": not template["published"], "sections": sections, "fields": fields}
                self.target_count = len(sections) or 1
            if self.stop_event.is_set():
                self.phase = "stopped"
                return
            self.opening = False
            self.running, self.ready = True, True
            if self.commenter:
                comment_task = asyncio.create_task(self.commenter.collect_and_run())
            if can_post:
                self.log(f"익명 준비 완료 · 대상 {self.target_count}곳 · 섹션당 {'무제한' if limit == 0 else str(limit) + '건'} · {delay}초마다 전체 섹션 동시 전송", "success")
                self.posts_active = True
                try:
                    await self.run(subject, body, limit, delay, finalize=False)
                finally:
                    self.posts_phase, self.posts_active = self.phase, False
            elif self.posts_phase != "error":
                self.posts_phase = "disabled"
            if self.commenter:
                self.commenter.finish()
                self.phase = "commenting"
                await comment_task
            if self.stop_event.is_set():
                self.phase = "stopped"
            elif self.posts_phase == "error" or self.commenter and self.commenter.failed:
                self.phase = "error"
            else:
                self.phase = "finished"
            self.log(f"작업 종료 · 게시글 {self.sent}건 / 댓글 {self.comments_sent}건 / 오류 {self.errors}건",
                     "success" if self.phase == "finished" else "warning")
        except asyncio.CancelledError:
            self.phase = "stopped"
            self.stop_event.set()
            self.log("작업을 중지했습니다. 전송 중인 응답을 정리합니다.")
        except Exception as error:
            self.phase = "error"
            self.errors += 1
            # Exception text/tracebacks can contain cookies, tokens, or response
            # bodies. Record only controlled stage names and exception types.
            logger.warning("Anonymous preparation failed: stage=%s error_type=%s", stage, type(error).__name__)
            if isinstance(error, (TimeoutError, BrowserTimeoutError)):
                message = f"{stage} 중 응답 대기 시간이 초과되었습니다. 서버 연결 상태를 확인한 뒤 다시 실행하세요."
            elif isinstance(error, BrowserError):
                reason = ("서버 브라우저가 종료되었습니다" if "closed" in str(error).lower()
                          or "crash" in str(error).lower() else "서버 브라우저 통신에 실패했습니다")
                message = f"{stage} 중 {reason}. 서버 실행 상태를 확인하세요."
            elif isinstance(error, ValueError):
                message = str(error)
            else:
                message = f"{stage} 중 내부 오류가 발생했습니다. 서버 로그를 확인하세요."
            self.log(message, "error")
        finally:
            # Both workers must settle before their shared guest browser is closed.
            if comment_task and not comment_task.done():
                self.stop_event.set()
                self.commenter.finish()
                await asyncio.gather(comment_task, return_exceptions=True)
            self.posts_active = False
            # Close Chromium; retain only our bot's deletion credentials in memory.
            board = self.board
            await self.clear_connection()
            self.board = board
            self.opening = self.running = self.automatic = False
            self.last_activity = time.monotonic()

    async def open(self, url):
        url = padlet_url(url)
        async with self.lock:
            if self.deleting or self.running or self.opening:
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
            if self.browser is not browser or self.ready or self.running or self.deleting:
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
            if self.deleting or self.running or self.opening:
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

    async def start(self, subject, body, limit, delay, attachment="", custom_properties=None, before_start=None):
        async with self.lock:
            if self.deleting or not self.ready or self.opening:
                raise ValueError("먼저 브라우저에서 성공한 게시 요청을 캡처하세요.")
            if self.running:
                raise ValueError("이미 전송 중입니다.")
            subject, body, fields = validate_fields((self.board or {}).get("fields"), subject, body,
                                                   attachment, custom_properties or {},
                                                   self.template.get("custom_properties") if self.browser else None)
            if before_start:
                await before_start()
            self.template.update(fields)
            self.stop_event.clear()
            self.sent = self.errors = self.rounds = 0
            self.commenter = None
            self.comments_sent = self.comments_errors = self.comments_targets = 0
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
            # Deadline is checked at dispatch as well as by the expiry timer.
            if self.license_expires is not None and time.time() >= self.license_expires:
                self.stop_event.set()
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
                self.untracked += 1  # The server may have created a post before the connection failed.
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
                try:
                    hashid = deletion_hashid(response.json())
                except (AttributeError, ValueError, TypeError):
                    hashid = None
                if hashid and self.browser:
                    self.deletion_batch()["posts"].add(hashid)
                else:
                    self.untracked += 1
                self.sent += 1
                self.log(f"{label} · 전송 성공 #{self.sent}", "success")
                if self.commenter:
                    try:
                        identity = post_id(response.json(), self.template["wall_id"])
                    except (ValueError, TypeError, AttributeError):
                        identity = None
                    self.commenter.enqueue(identity)
                return "ok", 0
            self.errors += 1
            if status in {200, 201}:
                self.untracked += 1
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

    async def run(self, subject, body, limit, delay, finalize=True):
        # Each round covers every discovered section exactly once.
        targets = copy.deepcopy((self.board or {}).get("sections", [])) or [None]
        try:
            if self.http is None:
                raise ValueError("익명 세션이 없습니다.")
            await self.run_pipelined(targets, subject, body, limit, delay)
        except asyncio.CancelledError:
            self.phase = "stopped"
            raise
        except Exception:
            self.phase = "error"
            self.log("전송 작업 오류. 게시판에 다시 연결한 후 시도하세요.", "error")
        finally:
            if finalize:
                self.running = False
            self.last_activity = time.monotonic()

    async def run_pipelined(self, targets, subject, body, limit, delay):
        """Overlap scheduled rounds within a fixed request budget."""
        capacity = max(1, MAX_INFLIGHT_REQUESTS // len(targets))
        semaphore = asyncio.Semaphore(MAX_INFLIGHT_REQUESTS)
        paused = asyncio.Event()
        active, retries = {}, deque()
        launched, backoff, retry_after, fatal = 0, 0, 0, False
        next_start = time.monotonic() + delay
        self.log(f"병렬 전송 · 최대 {capacity}회차 / {MAX_INFLIGHT_REQUESTS}건 동시 처리 · 회차 간격 {delay:g}초")

        def collect(future):
            nonlocal fatal, retry_after
            batch = active.pop(future)
            results = future.result()
            fatal |= any(result == "error" for result, _ in results)
            retry_after = max(retry_after, max(wait for _, wait in results))
            pending = [section for section, (result, _) in zip(batch, results) if result != "ok"]
            if pending:
                retries.append(pending)
            else:
                self.rounds += 1

        async def drain():
            if active:
                await asyncio.gather(*active)
                for future in list(active):
                    collect(future)

        try:
            while True:
                for future in list(active):
                    if future.done():
                        collect(future)
                if fatal or self.stop_event.is_set() or paused.is_set():
                    # Let requests already sent settle before counting, retrying, or closing.
                    await drain()
                    if fatal or self.stop_event.is_set():
                        break
                    backoff = max(min(backoff + 3, 30), retry_after)
                    retry_after = 0
                    self.phase = "backoff"
                    self.log(f"429 요청 제한 · {backoff:g}초 대기 · 미완료 요청만 재시도", "warning")
                    if await self.wait(backoff):
                        break
                    paused.clear()
                    next_start = time.monotonic()
                    continue
                more = bool(retries) or limit == 0 or launched < limit
                if more and len(active) < capacity:
                    remaining = next_start - time.monotonic()
                    if remaining > 0:
                        interval_wait = asyncio.create_task(self.wait(remaining))
                        try:
                            # Process completed responses (especially 429/errors) promptly
                            # even when the configured interval is very long.
                            await asyncio.wait([interval_wait, *active],
                                               return_when=asyncio.FIRST_COMPLETED)
                            if not interval_wait.done() or interval_wait.result():
                                continue
                        finally:
                            if not interval_wait.done():
                                interval_wait.cancel()
                            await asyncio.gather(interval_wait, return_exceptions=True)
                    if paused.is_set():
                        continue
                    if retries:
                        batch = retries.popleft()
                    else:
                        batch = targets
                        launched += 1
                    self.phase = "sending"
                    future = asyncio.gather(*(self.send_section(section, subject, body, semaphore, paused)
                                              for section in batch))
                    active[future] = batch
                    next_start = time.monotonic() + delay
                    # Yield even at delay=0 so responses and Stop can be processed.
                    await asyncio.sleep(0)
                elif active:
                    await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
                else:
                    break
            if fatal:
                self.phase = "error"
            elif self.stop_event.is_set():
                self.phase = "stopped"
                self.log("사용자가 전송을 중지했습니다.")
            else:
                self.phase = "finished"
                self.log(f"게시글 전송 완료 · {self.rounds}회 반복 / 게시글 {self.sent}건", "success")
        finally:
            # An unexpected exception must not leave POST tasks running after session cleanup.
            if active:
                await asyncio.gather(*active, return_exceptions=True)

    def deletion_batch(self):
        if self.active_batch is None:
            self.active_batch = {"endpoint": self.endpoint, "posts": set(), "comments": set(), "snapshot": None}
            self.deletion_batches.append(self.active_batch)
        return self.active_batch

    def stop(self):
        first_stop = not self.stop_event.is_set()
        self.stop_event.set()
        if first_stop and self.automatic and self.opening and self.worker and not self.worker.done():
            self.worker.cancel()
            self.phase = "stopping"
        if self.running:
            self.phase = "stopping"

    async def queue_delete(self):
        async with self.lock:
            if self.deleting:
                return  # Duplicate clicks reuse the same deletion job.
            retry = math.ceil(self.delete_retry_at - time.monotonic())
            if retry > 0:
                raise ValueError(f"요청 제한으로 {retry}초 후 삭제를 다시 시도하세요.")
            if self.opening and not self.automatic:
                raise ValueError("게시판 연결이 끝난 후 삭제를 다시 시도하세요.")
            self.deleting = True
            self.delete_errors = 0
            self.stop()
            self.log("긴급 삭제 접수 · 새 전송을 중지하고 진행 중인 응답을 기다립니다.", "warning")
            self.delete_worker = asyncio.create_task(self.delete_sent())

    async def delete_sent(self):
        board = self.board
        try:
            if self.worker and not self.worker.done():
                await self.worker
            board = self.board
            await self.clear_connection()
            for batch in list(self.deletion_batches):
                if self.closing:
                    break
                if not batch["snapshot"]:
                    self.delete_errors += len(batch["posts"]) + len(batch.get("comments", ()))
                    self.log("익명 세션이 없는 게시글·댓글은 삭제할 수 없습니다.", "error")
                    continue
                try:
                    self.browser = BrowserSession(self)
                    page = await self.browser.restore(batch["snapshot"])
                    self.http = BrowserHTTP(page, batch["endpoint"])
                    comments = batch.setdefault("comments", set())
                    targets = [(comments, identity, self.http.origin + "/api/5/comments/" + str(identity))
                               for identity in list(comments)]
                    targets += [(batch["posts"], hashid, batch["endpoint"] + "/" + hashid)
                                for hashid in list(batch["posts"])]
                    for collection, identity, delete_url in targets:
                        if self.closing:
                            break
                        response = await self.http.delete(delete_url, timeout=10)
                        status = response.status_code
                        challenge = response.headers.get("cf-mitigated") == "challenge"
                        confirmed = status in {204, 404} and not challenge
                        if status == 200 and not challenge:
                            try:
                                result = response.json()
                                confirmed = isinstance(result, dict) and not result.get("error") and not result.get("errors")
                            except (ValueError, TypeError):
                                pass
                        if confirmed:
                            collection.remove(identity)
                            self.deleted += 1
                            self.log(f"삭제 확인 #{self.deleted}", "success")
                            continue
                        self.delete_errors += 1
                        if status == 429 and not challenge:
                            try:
                                retry = float(response.headers.get("Retry-After") or 30)
                                retry = max(1, retry) if math.isfinite(retry) else 30
                            except ValueError:
                                retry = 30
                            self.delete_retry_at = time.monotonic() + retry
                            self.log(f"삭제 요청 제한 · {math.ceil(retry)}초 후 긴급 삭제를 다시 누르세요.", "warning")
                            return
                        self.log(f"삭제 실패 · HTTP {status}. 남은 게시물을 보관했습니다.", "error")
                        break
                except Exception:
                    self.delete_errors += 1
                    self.log("삭제 응답을 확인하지 못했습니다. 남은 게시물은 긴급 삭제로 다시 시도할 수 있습니다.", "error")
                finally:
                    if (batch["posts"] or batch.get("comments")) and self.browser:
                        try:
                            batch["snapshot"] = await self.browser.snapshot()
                        except Exception:
                            pass  # Keep the last usable credentials if the browser failed.
                    await self.clear_connection()
                if not batch["posts"] and not batch.get("comments"):
                    self.deletion_batches.remove(batch)
        except Exception:
            self.delete_errors += 1
            self.log("긴급 삭제를 완료하지 못했습니다. 남은 게시물은 다시 시도할 수 있습니다.", "error")
        finally:
            await self.clear_connection()
            self.board = board
            self.deleting = False
            self.phase = "delete_incomplete" if self.deletion_batches or self.untracked or self.delete_errors else "deleted"
            remaining = sum(len(b["posts"]) + len(b.get("comments", ())) for b in self.deletion_batches)
            self.log(f"긴급 삭제 결과 · 삭제 확인 {self.deleted}건 / 남음 {remaining}건 / 추적 불가 {self.untracked}건",
                     "warning" if self.phase == "delete_incomplete" else "success")
            self.last_activity = time.monotonic()

    async def reset(self):
        async with self.lock:
            if self.deleting or self.opening:
                raise ValueError("게시판 연결이 끝난 후 다시 시도하세요.")
            self.stop()
            if self.worker and not self.worker.done():
                await self.worker
            await self.clear_connection()
            self.phase = "waiting"
            self.log("브라우저 세션과 캡처 정보를 삭제했습니다.")

    async def shutdown(self):
        self.closing = True
        self.stop()
        if self.delete_worker and not self.delete_worker.done():
            await self.delete_worker
        if self.worker and not self.worker.done():
            await self.worker
        await self.clear_connection()
        self.deletion_batches.clear()
