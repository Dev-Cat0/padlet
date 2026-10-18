"""Memory-only anonymous HTTP sender, preserving the original send loop."""
import asyncio
import copy
import html
import math
import secrets
import time
from collections import deque
from datetime import datetime, timezone

from curl_cffi.requests import AsyncSession
from .anonymous import padlet_url, prepare


def make_payload(template: dict, subject: str, body: str) -> dict:
    payload = copy.deepcopy(template)
    payload["subject"] = subject
    payload["body"] = "<p>" + html.escape(body).replace("\n", "<br>") + "</p>"
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
                "board": self.board, "phase": self.phase, "sent": self.sent, "errors": self.errors,
                "limit": self.limit, "rounds": self.rounds, "target_count": self.target_count,
                "logs": list(self.logs)}

    async def clear_connection(self):
        self.ready = False
        self.endpoint, self.headers, self.template, self.board = "", {}, {}, None
        if self.http:
            await self.http.close()
            self.http = None

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

    async def start(self, subject, body, limit, delay):
        async with self.lock:
            if not self.ready or self.opening:
                raise ValueError("먼저 익명 게시판에 연결하세요.")
            if self.running:
                raise ValueError("이미 전송 중입니다.")
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
                response = await self.http.post(self.endpoint,
                    json=make_payload(template, subject, body), headers=self.headers,
                    allow_redirects=False, timeout=10)
            except Exception:
                self.errors += 1
                paused.set()
                self.log(f"{label}: 네트워크 오류로 중지했습니다. 마지막 게시 성공 여부를 확인하세요.", "error")
                return "error", 0
            status = response.status_code
            if status in {200, 201}:
                self.sent += 1
                self.log(f"{label} · 전송 성공 #{self.sent}", "success")
                return "ok", 0
            self.errors += 1
            paused.set()
            if status == 429:
                try:
                    retry = float(response.headers.get("Retry-After", 0))
                    retry = max(0, retry) if math.isfinite(retry) else 0
                except ValueError:
                    retry = 0
                return "limited", retry
            if status in {401, 403}:
                self.ready = False
            self.log(f"{label}: HTTP {status} 오류로 중지했습니다. 권한·섹션 목록을 확인하고 다시 연결하세요.", "error")
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
            self.log("익명 세션을 삭제했습니다.")

    async def shutdown(self):
        self.stop()
        if self.worker and not self.worker.done():
            await self.worker
        await self.clear_connection()
