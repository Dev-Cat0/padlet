import asyncio
import os
import secrets
import re
import time
from contextlib import asynccontextmanager, suppress
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Response, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .engine import Sender
from .sessions import Sessions
from .key_access import KeyAccess
from .license_admin import routes as license_routes
from .audit import worker as audit_worker

sessions = Sessions()
key_access = KeyAccess()
allowed_origins = [s.strip() for s in os.getenv("ALLOWED_ORIGINS", "https://dev-cat0.github.io").split(",") if s.strip()]


@asynccontextmanager
async def lifespan(app):
    await key_access.start()
    cleanup = asyncio.create_task(sessions.cleanup())
    audit = asyncio.create_task(audit_worker(key_access))
    yield
    cleanup.cancel()
    audit.cancel()
    with suppress(asyncio.CancelledError):
        await cleanup
    with suppress(asyncio.CancelledError):
        await audit
    await sessions.shutdown()
    await key_access.close()


app = FastAPI(title="Padlet Sender", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(license_routes(key_access, sessions))
app.add_middleware(CORSMiddleware,
    allow_origins=allowed_origins,
    allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type", "X-Request-ID"])


async def authorize(authorization: str = Header(default="")):
    return sessions.authorize(authorization)


def check_origin(origin):
    if origin not in allowed_origins:
        raise HTTPException(403, "웹 패널에서 연결하세요.")


@app.post("/api/session")
async def removed_session(origin: str = Header(default="")):
    check_origin(origin)
    raise HTTPException(403, "키 인증이 필요합니다.")


class LoginRequest(BaseModel):
    key: str = Field(max_length=64)


@app.post("/api/auth/login")
async def login(data: LoginRequest, request: Request, origin: str = Header(default="")):
    check_origin(origin)
    client_id = key_access.login_limit(request.client.host if request.client else "unknown")
    key = data.key.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{5}", key):
        raise HTTPException(403, "사용할 수 없는 키입니다.")
    result = await key_access.rpc("verify_key", "", key=key, client_id=client_id)
    license_id = result.get("license_id")
    if not isinstance(license_id, str) or not 20 <= len(license_id) <= 100:
        raise HTTPException(503, "인증 서버 응답을 확인하지 못했습니다.")
    # Only the independent random session token leaves this endpoint. Never echo the key.
    token = sessions.create(license_id=license_id)
    sessions.update_access(license_id, result)
    return {"token": token}


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, error):
    if request.url.path == "/api/auth/login" or request.url.path.startswith('/internal/'):
        return JSONResponse(status_code=422, content={"detail": "5자리 영문·숫자 키를 입력하세요."})
    return await request_validation_exception_handler(request, error)


@app.websocket("/internal/key-agent")
async def local_agent(websocket: WebSocket):
    # Retired: an old bot must never re-enable the old local key database.
    await websocket.close(code=1008)


async def license_check(sender):
    license_id = sessions.for_sender(sender).license_id
    if not license_id:
        raise HTTPException(403, "키 인증이 필요합니다.")
    try:
        result = await key_access.rpc("check", license_id)
    except HTTPException as error:
        if error.status_code == 403:
            sessions.block_access(sender)
        raise
    sessions.update_access(license_id, result)
    return result


def charge(sender, request_id):
    async def consume():
        license_id = sessions.for_sender(sender).license_id
        if not license_id:
            raise HTTPException(403, "키 인증이 필요합니다.")
        try:
            result = await key_access.rpc("consume", license_id, request_id)
        except HTTPException as error:
            if error.status_code == 403:
                sessions.block_access(sender)
            raise
        # A reply can arrive after expiry even if the local bot approved it just before.
        expires = result.get("expires")
        if expires is not None and expires <= time.time():
            sessions.block_access(sender)
            raise HTTPException(403, "시간제 키의 이용 기한이 종료되었습니다.")
        sessions.update_access(license_id, result)
        sessions.schedule_expiry(sender, result.get("expires"))
    return consume


@app.middleware("http")
async def headers(request, call_next):
    try:
        length = int(request.headers.get("content-length", "0"))
    except ValueError:
        return Response(status_code=400)
    if length > 65536:
        return Response(status_code=413)
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > 65536:
            return Response(status_code=413)
        chunks.append(chunk)
    request._body = b''.join(chunks)
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
    return {"status": "ok", "app": "padlet-sender", "key_auth": "server-v2"}


@app.get("/api/state")
async def state(sender: Sender = Depends(authorize)):
    return sessions.status(sender)


@app.post("/api/board/connect")
@app.post("/api/browser/open")
async def connect_board(data: OpenRequest, sender: Sender = Depends(authorize)):
    await license_check(sender)
    try:
        await sessions.open(sender, data.url)
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    return sessions.status(sender)


@app.get("/api/browser/frame")
async def browser_frame(sender: Sender = Depends(authorize)):
    if not sender.browser or sender.deleting or sender.running or sender.ready:
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
    # A remote click could post directly without a metered /api/run or /api/start.
    raise HTTPException(410, "수동 브라우저 조작은 종료되었습니다. 웹 패널의 실행 버튼을 사용하세요.")


@app.post("/api/start")
async def start(data: SendRequest, sender: Sender = Depends(authorize)):
    try:
        await sender.start(data.subject, data.body, data.limit, data.delay,
                           data.attachment, data.custom_properties,
                           before_start=charge(sender, secrets.token_urlsafe(32)))
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    return sessions.status(sender)


@app.post("/api/run")
async def run(data: RunRequest, sender: Sender = Depends(authorize),
              request_id: str = Header(default="", alias="X-Request-ID")):
    if not 16 <= len(request_id) <= 100 or not all(c.isalnum() or c in "-_" for c in request_id):
        raise HTTPException(400, "실행 요청 번호가 필요합니다.")
    # Session-specific namespace prevents replay across independently authenticated browser sessions.
    session_token = next(token for token, session in sessions.items.items() if session.sender is sender)
    try:
        await sessions.run(sender, data, before_start=charge(sender, session_token + ":" + request_id),
                           request_id=request_id)
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    return sessions.status(sender)


@app.post("/api/stop")
async def stop(sender: Sender = Depends(authorize)):
    sender.stop()
    return sessions.status(sender)


@app.post("/api/reset")
async def reset(sender: Sender = Depends(authorize)):
    try:
        await sender.reset()
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    return sessions.status(sender)


@app.post("/api/emergency-delete")
async def emergency_delete(sender: Sender = Depends(authorize)):
    try:
        await sessions.delete(sender)
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    return sessions.status(sender)
