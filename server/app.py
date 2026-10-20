import asyncio
import os
from contextlib import asynccontextmanager, suppress
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .engine import Sender
from .sessions import Sessions

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
    subject: str = Field(default="", max_length=1000)
    body: str = Field(default="", max_length=20000)
    limit: int = Field(default=50, ge=0, le=1000000)
    delay: float = Field(default=0.3, ge=0, le=86400, allow_inf_nan=False)
    attachment: str = Field(default="", max_length=2000)
    custom_properties: dict[str, str | float | int | None] = Field(default_factory=dict, max_length=100)


class BrowserAction(BaseModel):
    kind: Literal["click", "text", "key", "scroll", "tab", "reload"]
    x: float = Field(default=0, ge=0, le=1100, allow_inf_nan=False)
    y: float = Field(default=0, ge=0, le=720, allow_inf_nan=False)
    text: str = Field(default="", max_length=20000)
    key: Literal["Enter", "Tab", "Shift+Tab", "Backspace", "Delete", "Escape", "Control+a",
                 "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Space"] = "Enter"
    delta: int = Field(default=0, ge=-1500, le=1500)
    index: int = Field(default=0, ge=0, le=20)


class RunRequest(BaseModel):
    url: str = Field(min_length=20, max_length=2000)
    subject: str = Field(default="", max_length=1000)
    body: str = Field(default="", max_length=20000)
    limit: int = Field(default=50, ge=0, le=1000000)
    delay: float = Field(default=0.3, ge=0, le=86400, allow_inf_nan=False)


@app.get("/")
@app.get("/healthz")
async def health():
    return {"status": "ok", "app": "padlet-sender"}


@app.get("/api/state")
async def state(sender: Sender = Depends(authorize)):
    return sender.status()


@app.post("/api/board/connect")
@app.post("/api/browser/open")
async def connect_board(data: OpenRequest, sender: Sender = Depends(authorize)):
    try:
        await sessions.open(sender, data.url)
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    return sender.status()


@app.get("/api/browser/frame")
async def browser_frame(sender: Sender = Depends(authorize)):
    if not sender.browser or sender.running or sender.ready:
        raise HTTPException(409, "브라우저 화면을 사용할 수 없습니다.")
    try:
        return Response(await sender.browser.screenshot(), media_type="image/jpeg")
    except Exception:
        raise HTTPException(409, "화면을 불러오지 못했습니다. 잠시 후 다시 시도하세요.") from None


@app.get("/api/browser/tabs")
async def browser_tabs(sender: Sender = Depends(authorize)):
    return sender.browser.tabs() if sender.browser and sender.browser.context else []


@app.post("/api/browser/action")
async def browser_action(data: BrowserAction, sender: Sender = Depends(authorize)):
    if not sender.browser:
        raise HTTPException(409, "먼저 브라우저를 여세요.")
    try:
        await sender.browser.control(data)
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    except Exception:
        raise HTTPException(409, "조작을 완료하지 못했습니다. 화면을 확인하세요. 자동 재시도하지 않습니다.") from None
    return sender.status()


@app.post("/api/start")
async def start(data: SendRequest, sender: Sender = Depends(authorize)):
    try:
        await sender.start(data.subject, data.body, data.limit, data.delay,
                           data.attachment, data.custom_properties)
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    return sender.status()


@app.post("/api/run")
async def run(data: RunRequest, sender: Sender = Depends(authorize)):
    try:
        await sessions.run(sender, data)
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
