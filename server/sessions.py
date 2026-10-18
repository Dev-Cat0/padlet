"""Automatic, isolated sessions for the public web panel."""
import asyncio
import secrets
import time
from collections import deque
from dataclasses import dataclass, field

from fastapi import HTTPException

from .engine import Sender


@dataclass
class PanelSession:
    sender: Sender = field(default_factory=Sender)
    last_seen: float = field(default_factory=time.monotonic)


class Sessions:
    def __init__(self):
        self.items = {}
        self.created = deque()

    def create(self):
        now = time.monotonic()
        while self.created and now - self.created[0] > 60:
            self.created.popleft()
        if len(self.items) >= 32 or len(self.created) >= 20:
            raise HTTPException(429, "서버가 사용 중입니다. 잠시 후 자동으로 다시 연결합니다.")
        token = secrets.token_urlsafe(32)
        self.items[token] = PanelSession()
        self.created.append(now)
        return token

    def authorize(self, authorization):
        scheme, _, token = authorization.partition(" ")
        session = self.items.get(token) if scheme == "Bearer" else None
        if session is None:
            raise HTTPException(401, "세션이 만료되어 자동으로 다시 연결합니다.")
        session.last_seen = time.monotonic()
        return session.sender

    async def collect(self):
        now = time.monotonic()
        for token, session in list(self.items.items()):
            sender = session.sender
            if sender.running or sender.opening:
                continue
            if now - session.last_seen > 1800:
                del self.items[token]
                await sender.shutdown()
            elif now - sender.last_activity > 1800 and sender.ready:
                await sender.reset()

    async def cleanup(self):
        while True:
            await asyncio.sleep(30)
            await self.collect()

    async def shutdown(self):
        await asyncio.gather(*(s.sender.shutdown() for s in self.items.values()))
        self.items.clear()
        self.created.clear()
