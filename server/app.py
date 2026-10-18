import asyncio
import os
from contextlib import asynccontextmanager, suppress
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

from .engine import Sender
from .sessions import Sessions
from .stream import browser_stream

sessions = Sessions()
allowed_origins = [s.strip() for s in os.getenv("ALLOWED_ORIGINS", "https://dev-cat0.github.io").split(",") if s.strip()]


@asynccontextmanager
async def lifespan(app):
    cleanup = asyncio.create_task(sessions.cleanup())
    yield
    cleanup.cancel()
    with suppress(asyncio.CancelledError):
        await cleanup
    await sessions.shutdown()


app = FastAPI(title="Padlet Sender", lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware,
    allow_origins=allowed_origins,
    allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type"])


async def authorize(authorization: str = Header(default="")):
    return sessions.authorize(authorization)


@app.post("/api/session")
async def create_session(origin: str = Header(default="")):
    if origin not in allowed_origins:
        raise HTTPException(403, "웹 패널에서 연결하세요.")
    return {"token": sessions.create()}


@app.middleware("http")
async def headers(request, call_next):
    if int(request.headers.get("content-length", "0")) > 65536:
        return Response(status_code=413)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


class OpenRequest(BaseModel):
    url: str = Field(min_length=20, max_length=2000)


class SendRequest(BaseModel):
    subject: str = Field(min_length=1, max_length=1000)
    body: str = Field(default="", max_length=20000)
    limit: int = Field(default=50, ge=0, le=1000000)
    delay: float = Field(default=0.3, ge=0, le=86400, allow_inf_nan=False)

    @field_validator("subject")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("제목을 입력하세요.")
        return value.strip()


class BrowserAction(BaseModel):
    kind: Literal["click", "text", "key", "scroll", "tab"]
    x: float = Field(default=0, ge=0, le=1100, allow_inf_nan=False)
    y: float = Field(default=0, ge=0, le=720, allow_inf_nan=False)
    text: str = Field(default="", max_length=20000)
    key: Literal["Enter", "Tab", "Shift+Tab", "Backspace", "Delete", "Escape", "Control+a",
                 "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Space"] = "Enter"
    delta: int = Field(default=0, ge=-1500, le=1500)
    index: int = Field(default=0, ge=0, le=20)


@app.websocket("/api/browser/stream")
async def stream(socket: WebSocket):
    if socket.headers.get("origin") not in allowed_origins:
        await socket.close(code=1008)
        return
    await socket.accept()
    try:
        # Authenticate in the first message, never in URL/access logs.
        raw = await asyncio.wait_for(socket.receive_text(), timeout=5)
        if len(raw) > 1024:
            raise ValueError()
        import json
        data = json.loads(raw)
        sender = sessions.authorize("Bearer " + data["token"])
    except (TimeoutError, ValueError, KeyError, TypeError, HTTPException, WebSocketDisconnect):
        with suppress(Exception):
            await socket.close(code=1008)
        return
    await browser_stream(socket, sender, BrowserAction)


@app.get("/")
@app.get("/healthz")
async def health():
    return {"status": "ok", "app": "padlet-sender"}


@app.get("/api/state")
async def state(sender: Sender = Depends(authorize)):
    return sender.status()


@app.post("/api/browser/open")
async def open_browser(data: OpenRequest, sender: Sender = Depends(authorize)):
    try:
        await sessions.open(sender, data.url)
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    return sender.status()


@app.get("/api/browser/frame")
async def frame(sender: Sender = Depends(authorize)):
    try:
        return Response(await sender.screenshot(), media_type="image/jpeg")
    except Exception:
        raise HTTPException(409, "브라우저가 닫혔거나 화면을 갱신 중입니다.") from None


@app.get("/api/browser/tabs")
async def tabs(sender: Sender = Depends(authorize)):
    pages = sender.context.pages if sender.context else []
    # Do not expose OAuth query strings or authorization codes.
    from urllib.parse import urlsplit
    return [{"index": i, "host": urlsplit(p.url).hostname or "새 탭", "active": p == sender.page}
            for i, p in enumerate(pages)]


@app.post("/api/browser/action")
async def control(data: BrowserAction, sender: Sender = Depends(authorize)):
    try:
        await sender.control(data)
    except Exception:
        raise HTTPException(409, "브라우저 조작 실패. 화면을 새로 확인하세요.") from None
    return {"ok": True}


@app.post("/api/start")
async def start(data: SendRequest, sender: Sender = Depends(authorize)):
    try:
        await sender.start(data.subject, data.body, data.limit, data.delay)
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    return sender.status()


@app.post("/api/stop")
async def stop(sender: Sender = Depends(authorize)):
    sender.stop()
    return sender.status()


@app.post("/api/reset")
async def reset(sender: Sender = Depends(authorize)):
    try:
        await sender.reset()
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    return sender.status()
