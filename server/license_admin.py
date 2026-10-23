"""Signed management API used only by the local Discord bot."""
import asyncio
import re

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from typing import Literal

from .license_store import LicenseError, OWNER_ID
from .audit import LOG_CHANNEL_ID


class AdminRequest(BaseModel):
    actor: int = Field(gt=0)
    user: int | None = Field(default=None, gt=0)
    guild: int | None = Field(default=None, gt=0)
    channel: int | None = Field(default=None, gt=0)
    kind: Literal['uses', 'days', 'month', 'year', 'forever'] | None = None
    amount: int = Field(default=1, ge=1, le=36500)
    quantity: int = Field(default=1, ge=1, le=100)
    grant_id: int | None = Field(default=None, gt=0)
    license_id: str = Field(default='', max_length=100)
    webhook: str = Field(default='', max_length=500)


def routes(access, sessions):
    router = APIRouter()

    @router.post('/internal/licenses/{action}')
    async def manage(action: str, data: AdminRequest, request: Request):
        await access.authenticate_admin(request)
        store = access.require_store()
        try:
            # The signing key grants transport access; owner checks still gate management actions.
            if action == 'grant':
                if data.user is None or data.kind is None:
                    raise LicenseError('대상과 종류가 필요합니다.')
                return {'id': await asyncio.to_thread(store.grant, data.actor, data.user, data.kind, data.amount, data.quantity)}
            if action in {'available', 'prepare'}:
                if data.user != data.actor:
                    raise LicenseError('본인의 발급권만 사용할 수 있습니다.')
                if action == 'available':
                    return {'grants': await asyncio.to_thread(store.available, data.user)}
                if data.grant_id is None:
                    raise LicenseError('발급권이 필요합니다.')
                return await asyncio.to_thread(store.prepare, data.user, data.grant_id)
            if action in {'delivered', 'delivery_failed'}:
                # Only a key's recipient can finish its delivery, even over signed transport.
                def finish():
                    if store.owner_for(data.license_id) != str(data.actor):
                        raise LicenseError('발급 대상이 다릅니다.')
                    getattr(store, action)(data.license_id)
                await asyncio.to_thread(finish)
                return {'ok': True}
            if action == 'ticket':
                if data.user != data.actor or data.guild is None:
                    raise LicenseError('본인의 문의만 조회할 수 있습니다.')
                return {'channel': await asyncio.to_thread(store.ticket, data.guild, data.user, data.channel)}
            store.require_owner(data.actor)
            if action == 'revoke':
                count = await asyncio.to_thread(store.revoke, data.actor, data.license_id)
                for session in list(sessions.items.values()):
                    if session.license_id == data.license_id:
                        sessions.block_access(session.sender)
                        session.sender.stop()
                return {'count': count}
            if action == 'revoke_grants':
                if data.user is None:
                    raise LicenseError('대상이 필요합니다.')
                await asyncio.to_thread(store.revoke_grants, data.actor, data.user)
                return {'ok': True}
            if action == 'revoke_all':
                count = await asyncio.to_thread(store.revoke_all, data.actor)
                for session in list(sessions.items.values()):
                    sessions.block_access(session.sender)
                    session.sender.stop()
                return {'count': count}
            if action == 'configure_log':
                if data.channel != LOG_CHANNEL_ID or not re.fullmatch(r'https://discord\.com/api/webhooks/[0-9]+/[A-Za-z0-9_-]+', data.webhook):
                    raise LicenseError('지정된 로그 채널의 Discord 웹훅이 필요합니다.')
                await asyncio.to_thread(store.set_webhook, data.webhook)
                return {'ok': True}
            if action == 'status':
                return {'ok': True, 'log_configured': bool(await asyncio.to_thread(store.webhook))}
            raise HTTPException(404, '알 수 없는 관리 작업입니다.')
        except LicenseError as error:
            raise HTTPException(403, str(error)) from None
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, '키 저장소 처리 실패. 잠시 후 다시 확인하세요.') from None
        finally:
            access.audit_signal.set()

    return router
