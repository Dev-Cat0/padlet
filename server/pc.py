"""Short-lived pairing and commands for a user's local Chromium helper."""
import copy
import secrets
import time
from collections import deque

from fastapi import HTTPException


class PCLink:
    def __init__(self):
        self.code = self.token = ""
        self.expires = self.last_seen = 0
        self.command = None
        self.active = False
        self.captured_id = ""

    @property
    def connected(self):
        return bool(self.token) and time.monotonic() - self.last_seen < 30

    def pair(self):
        self.code = secrets.token_hex(5).upper()
        self.expires = time.monotonic() + 300
        return {"code": self.code, "expires_in": 300}

    def enqueue(self, kind, **values):
        self.command = {"id": secrets.token_hex(16), "kind": kind, **values}

    def cancel(self):
        self.active = False
        self.captured_id = ""
        self.enqueue("close")


class PCConnections:
    def __init__(self, sessions):
        self.sessions = sessions
        self.attempts = deque()

    def connect(self, code):
        now = time.monotonic()
        while self.attempts and now - self.attempts[0] > 60:
            self.attempts.popleft()
        if len(self.attempts) >= 30:
            raise HTTPException(429, "잠시 후 다시 연결하세요.")
        self.attempts.append(now)
        for session in self.sessions.items.values():
            pc = session.sender.pc
            if pc.code and pc.expires > now and secrets.compare_digest(pc.code, code):
                pc.code = ""
                pc.token = secrets.token_urlsafe(32)
                pc.last_seen = session.last_seen = now
                session.sender.log("내 PC 브라우저 도우미가 연결되었습니다.", "success")
                return {"token": pc.token}
        raise HTTPException(401, "연결 코드가 틀리거나 만료됐습니다. 패널에서 다시 발급하세요.")

    def authorize(self, authorization):
        scheme, _, token = authorization.partition(" ")
        if scheme == "Bearer" and token:
            for session in self.sessions.items.values():
                pc = session.sender.pc
                if pc.token and secrets.compare_digest(pc.token, token):
                    pc.last_seen = session.last_seen = time.monotonic()
                    return session.sender
        raise HTTPException(401, "패널과 다시 연결하세요.")


async def open_pc(sender, url):
    from .engine import padlet_url
    padlet_url(url)
    async with sender.lock:
        if sender.running or sender.opening:
            raise ValueError("진행 중인 작업을 먼저 중지하세요.")
        if not sender.pc.connected:
            raise ValueError("먼저 PC 도우미에 연결 코드를 입력하세요.")
        await sender.close_browser()
        sender.generation += 1
        sender.endpoint, sender.headers, sender.template = "", {}, {}
        sender.ready = False
        sender.opening = sender.pc.active = True
        sender.phase = "opening"
        sender.pc.captured_id = ""
        sender.pc.enqueue("open", url=url)
        sender.last_activity = time.monotonic()
        sender.log("내 PC에서 브라우저를 여는 중입니다.")


async def accept_capture(sender, data):
    from .engine import is_capture_url
    async with sender.lock:
        pc = sender.pc
        if pc.captured_id == data.command_id and sender.ready:
            return  # Retrying this upload must not create a second operation.
        if not pc.active or not pc.command or pc.command['id'] != data.command_id:
            raise HTTPException(409, "이미 종료되거나 교체된 캡처입니다.")
        if not is_capture_url(data.endpoint) or data.status not in {200, 201} or not data.template:
            raise HTTPException(422, "성공한 Padlet 게시 요청만 캡처할 수 있습니다.")
        headers = {}
        for name, value in data.headers.items():
            name = name.lower()
            if not name or any(c in name + value for c in "\r\n\0"):
                raise HTTPException(422, "잘못된 요청 헤더입니다.")
            if name not in {"host", "content-length", "content-type", "connection", "accept-encoding"}:
                headers[name] = value
        sender.endpoint, sender.headers = data.endpoint, headers
        sender.template = copy.deepcopy(data.template)
        sender.ready, sender.opening, sender.phase = True, False, "ready"
        sender.last_activity = time.monotonic()
        pc.captured_id, pc.active = data.command_id, False
        pc.enqueue("close")
        sender.log("PC 게시 요청 캡처 완료. Render에서 전송할 수 있습니다.", "success")
