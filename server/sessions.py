"""Automatic, isolated sessions for the public web panel."""
import asyncio
import hashlib
import secrets
import time
from collections import deque
from dataclasses import dataclass, field

from fastapi import HTTPException

from .engine import Sender
from .post_limits import post_limit


@dataclass
class PanelSession:
    sender: Sender = field(default_factory=Sender)
    last_seen: float = field(default_factory=time.monotonic)
    license_id: str = ""
    accepted: dict = field(default_factory=dict)
    expiry_task: asyncio.Task | None = None
    access: dict = field(default_factory=lambda: {"known": False})


class Sessions:
    def __init__(self):
        self.items = {}
        self.created = deque()
        self.browser_lock = asyncio.Lock()

    async def open(self, sender, url):
        async with self.browser_lock:
            if any(s.sender is not sender and (s.sender.browser or s.sender.opening or s.sender.deleting)
                   for s in self.items.values()):
                raise HTTPException(429, "다른 세션이 브라우저를 사용 중입니다. 종료 후 다시 시도하세요.")
            await sender.open(url)

    async def run(self, sender, data, before_start=None, request_id=None):
        async with self.browser_lock:
            session = self.for_sender(sender)
            fingerprint = hashlib.sha256(data.model_dump_json().encode()).hexdigest() if request_id else ""
            if request_id and request_id in session.accepted:
                if session.accepted[request_id] != fingerprint:
                    raise HTTPException(409, "같은 실행 번호에 다른 내용을 사용할 수 없습니다.")
                return
            if request_id and len(session.accepted) >= 1024:
                raise HTTPException(409, "이 접속의 실행 한도입니다. 필요한 삭제 작업을 마친 뒤 다시 인증하세요.")
            if any(s.sender is not sender and (s.sender.browser or s.sender.opening or s.sender.deleting)
                   for s in self.items.values()):
                raise HTTPException(429, "서버가 다른 작업을 처리 중입니다. 잠시 후 실행하세요.")
            options = {}
            if before_start:
                options["before_start"] = before_start
            if getattr(data, "comments", None) is not None:
                options["comments"] = data.comments
            await sender.queue_run(data.url, data.subject, data.body, data.limit, data.delay, **options)
            if request_id:
                session.accepted[request_id] = fingerprint

    def for_sender(self, sender):
        return next(s for s in self.items.values() if s.sender is sender)

    def update_access(self, license_id, result):
        # Share the balance across tabs logged in with the same license; never expose its ID/key.
        for session in self.items.values():
            if session.license_id == license_id:
                session.access = {"known": "remaining" in result, "remaining": result.get("remaining"),
                                  "expires": result.get("expires"), "blocked": False,
                                  "post_limit": post_limit(result)}

    def block_access(self, sender):
        license_id = self.for_sender(sender).license_id
        for session in self.items.values():
            if session.license_id == license_id:
                session.access = {**session.access, "blocked": True}

    def status(self, sender):
        access = self.for_sender(sender).access.copy()
        expires = access.get("expires")
        access["expired"] = expires is not None and time.time() >= expires
        access["can_run"] = not (access.get("blocked") or access["expired"] or access.get("remaining") == 0)
        return {**sender.status(), "access": access}

    def schedule_expiry(self, sender, expires):
        session = self.for_sender(sender)
        sender.license_expires = expires
        if session.expiry_task:
            session.expiry_task.cancel()
        if expires is None:
            session.expiry_task = None
            return

        async def expire():
            await asyncio.sleep(max(0, expires - time.time()))
            sender.stop()
            sender.log("시간제 키 이용 기한이 종료되어 실행을 중지합니다.")

        session.expiry_task = asyncio.create_task(expire())

    async def delete(self, sender):
        async with self.browser_lock:
            if any(s.sender is not sender and (s.sender.browser or s.sender.opening or s.sender.deleting)
                   for s in self.items.values()):
                raise HTTPException(429, "서버가 다른 작업을 처리 중입니다. 잠시 후 삭제를 다시 시도하세요.")
            await sender.queue_delete()

    def create(self, license_id=""):
        now = time.monotonic()
        while self.created and now - self.created[0] > 60:
            self.created.popleft()
        if len(self.items) >= 32 or len(self.created) >= 20:
            raise HTTPException(429, "서버가 사용 중입니다. 잠시 후 자동으로 다시 연결합니다.")
        token = secrets.token_urlsafe(32)
        self.items[token] = PanelSession(license_id=license_id)
        self.created.append(now)
        return token

    def authorize(self, authorization):
        scheme, _, token = authorization.partition(" ")
        session = self.items.get(token) if scheme == "Bearer" else None
        if session is None:
            raise HTTPException(401, "접속이 만료되었습니다. 키로 다시 인증하세요.")
        session.last_seen = time.monotonic()
        return session.sender

    async def collect(self):
        now = time.monotonic()
        for token, session in list(self.items.items()):
            sender = session.sender
            if sender.running or sender.opening or sender.deleting:
                continue
            if now - session.last_seen > 1800:
                del self.items[token]
                if session.expiry_task:
                    session.expiry_task.cancel()
                await sender.shutdown()
            elif now - sender.last_activity > 1800 and (sender.ready or sender.browser):
                await sender.reset()

    async def cleanup(self):
        while True:
            await asyncio.sleep(30)
            await self.collect()

    async def shutdown(self):
        tasks = [s.expiry_task for s in self.items.values() if s.expiry_task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(s.sender.shutdown() for s in self.items.values()))
        self.items.clear()
        self.created.clear()
