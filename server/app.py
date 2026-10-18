import asyncio
import hmac
import os
from contextlib import asynccontextmanager, suppress
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

from .engine import Sender

sender = Sender()


@asynccontextmanager
async def lifespan(app):
    cleanup = asyncio.create_task(sender.expire())
    yield
    cleanup.cancel()
    with suppress(asyncio.CancelledError):
        await cleanup
    await sender.shutdown()


app = FastAPI(title="Padlet Sender", lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware,
    allow_origins=[s.strip() for s in os.getenv("ALLOWED_ORIGINS", "https://dev-cat0.github.io").split(",") if s.strip()],
    allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type"])


async def authorize(authorization: str = Header(default="")):
    token = os.getenv("PANEL_TOKEN", "")
    if len(token) < 32:
        raise HTTPException(503, "Render 환경변수 PANEL_TOKEN을 32자 이상으로 설정하세요.")
    if not hmac.compare_digest(authorization.encode(), f"Bearer {token}".encode()):
        raise HTTPException(401, "패널 접속 키를 확인하세요.")


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


@app.get("/")
@app.get("/healthz")
async def health():
    return {"status": "ok", "app": "padlet-sender"}


@app.get("/api/state", dependencies=[Depends(authorize)])
async def state():
    return sender.status()


@app.post("/api/browser/open", dependencies=[Depends(authorize)])
async def open_browser(data: OpenRequest):
    try:
        await sender.open(data.url)
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    return sender.status()


@app.get("/api/browser/frame", dependencies=[Depends(authorize)])
async def frame():
    try:
        return Response(await sender.screenshot(), media_type="image/jpeg")
    except Exception:
        raise HTTPException(409, "브라우저가 닫혔거나 화면을 갱신 중입니다.") from None


@app.get("/api/browser/tabs", dependencies=[Depends(authorize)])
async def tabs():
    pages = sender.context.pages if sender.context else []
    # Do not expose OAuth query strings or authorization codes.
    from urllib.parse import urlsplit
    return [{"index": i, "host": urlsplit(p.url).hostname or "새 탭", "active": p == sender.page}
            for i, p in enumerate(pages)]


@app.post("/api/browser/action", dependencies=[Depends(authorize)])
async def control(data: BrowserAction):
    try:
        await sender.control(data)
    except Exception:
        raise HTTPException(409, "브라우저 조작 실패. 화면을 새로 확인하세요.") from None
    return {"ok": True}


@app.post("/api/start", dependencies=[Depends(authorize)])
async def start(data: SendRequest):
    try:
        await sender.start(data.subject, data.body, data.limit, data.delay)
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    return sender.status()


@app.post("/api/stop", dependencies=[Depends(authorize)])
async def stop():
    sender.stop()
    return sender.status()


@app.post("/api/reset", dependencies=[Depends(authorize)])
async def reset():
    try:
        await sender.reset()
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    return sender.status()
