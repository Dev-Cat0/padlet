"""Server-side key validation over the authenticated local-agent bridge."""
import asyncio
import hashlib
import hmac
import os
import secrets
import time
from collections import deque

from fastapi import HTTPException, WebSocket, WebSocketDisconnect


class KeyAccess:
    def __init__(self):
        self.socket = None
        self.pending = {}
        self.attempts = deque(maxlen=600)
        self.send_lock = asyncio.Lock()

    def require_online(self):
        if self.socket is None:
            raise HTTPException(503, "관리자의 로컬 인증 봇이 오프라인입니다. 잠시 후 다시 시도하세요.")

    def login_limit(self, peer):
        self.require_online()
        now = time.monotonic()
        client_id = hmac.new(os.getenv("KEY_AGENT_SECRET", "").encode(), peer.encode(), hashlib.sha256).hexdigest()
        recent = [(t, p) for t, p in self.attempts if now - t < 600]
        if sum(now - t < 60 for t, _ in recent) >= 60 or sum(p == client_id for _, p in recent) >= 5:
            raise HTTPException(429, "인증 시도가 너무 많습니다. 10분 후 다시 시도하세요.")
        self.attempts.append((now, client_id))
        return client_id

    async def rpc(self, action, license_id, request_id="", **payload):
        self.require_online()
        socket = self.socket
        if len(self.pending) >= 64:
            raise HTTPException(503, "인증 서버가 사용 중입니다.")
        rpc_id = secrets.token_urlsafe(16)
        future = asyncio.get_running_loop().create_future()
        self.pending[rpc_id] = future
        try:
            async with self.send_lock:
                await socket.send_json({"id": rpc_id, "action": action, "license_id": license_id,
                                             "request_id": request_id, **payload})
            result = await asyncio.wait_for(future, 8)
            if not result.get("ok"):
                if result.get("limited"):
                    raise HTTPException(429, "인증 시도가 너무 많습니다. 10분 후 다시 시도하세요.")
                raise HTTPException(403, "키가 만료·폐기되었거나 사용 횟수가 소진되었습니다.")
            return result
        except (TimeoutError, OSError, RuntimeError, WebSocketDisconnect):
            raise HTTPException(503, "로컬 인증 봇의 응답이 없습니다. 실행 상태를 확인하세요.") from None
        finally:
            self.pending.pop(rpc_id, None)

    async def websocket(self, ws: WebSocket):
        secret = os.getenv("KEY_AGENT_SECRET", "")
        if len(secret) < 32 or not hmac.compare_digest(ws.headers.get("authorization", ""), "Bearer " + secret):
            await ws.close(code=1008)
            return
        if self.socket is not None:
            await ws.close(code=1013)
            return
        await ws.accept()
        self.socket = ws
        try:
            while True:
                message = await ws.receive_json()
                if not isinstance(message, dict):
                    break
                rpc_id = message.get("id")
                if rpc_id in self.pending and not self.pending[rpc_id].done():
                    self.pending[rpc_id].set_result(message)
        except (WebSocketDisconnect, ValueError, RuntimeError):
            pass
        finally:
            self.socket = None
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(OSError("Agent disconnected"))

    async def close(self):
        if self.socket:
            await self.socket.close()
        self.attempts.clear()
