"""Server-owned authorization; never calls the Discord bot during a web request."""
import asyncio
import hashlib
import hmac
import json
import os
import re
import time
from collections import deque

from fastapi import HTTPException
from .license_store import LicenseError, RateLimited


class KeyAccess:
    def __init__(self):
        self.store = None
        self.attempts = deque(maxlen=600)
        self.audit_signal = asyncio.Event()

    async def start(self):
        self.audit_signal = asyncio.Event()
        if self.store is not None:
            return
        pepper = os.getenv('KEY_HASH_SECRET', '')
        project = os.getenv('FIREBASE_PROJECT_ID', '')
        if not project or len(pepper) < 32:
            return  # Fail closed without taking down stop/delete endpoints.
        if os.getenv('RENDER') and os.getenv('FIRESTORE_EMULATOR_HOST'):
            raise RuntimeError('Emulator credentials must never be used in production')
        from google.cloud import firestore
        from google.oauth2 import service_account
        from .firestore_store import FirestoreStore
        info = os.getenv('FIREBASE_SERVICE_ACCOUNT_JSON')
        credentials = service_account.Credentials.from_service_account_info(json.loads(info)) if info else None
        if credentials and credentials.project_id != project:
            raise RuntimeError('Firebase project does not match its service account')
        client = firestore.Client(project=project, credentials=credentials,
                                  database=os.getenv('FIREBASE_DATABASE_ID', '(default)'))
        self.store = FirestoreStore(client, pepper)

    def require_store(self):
        if self.store is None:
            raise HTTPException(503, '키 저장소 설정 중입니다. 관리자에게 문의하세요.')
        return self.store

    def login_limit(self, peer):
        self.require_store()
        now = time.monotonic()
        client_id = hmac.new(os.getenv('KEY_HASH_SECRET', '').encode(), peer.encode(), hashlib.sha256).hexdigest()
        recent = [(t, p) for t, p in self.attempts if now - t < 600]
        if sum(now - t < 60 for t, _ in recent) >= 60 or sum(p == client_id for _, p in recent) >= 5:
            raise HTTPException(429, '인증 시도가 너무 많습니다. 10분 후 다시 시도하세요.')
        self.attempts.append((now, client_id))
        return client_id

    async def rpc(self, action, license_id, request_id='', **payload):
        store = self.require_store()
        try:
            if action == 'verify_key':
                return await asyncio.to_thread(store.verify_key, payload['key'], payload['client_id'])
            if action == 'check':
                return await asyncio.to_thread(store.check, license_id)
            if action == 'consume':
                # Persist a hash, never a browser bearer token contained in the namespace.
                receipt = hashlib.sha256(request_id.encode()).hexdigest()
                return await asyncio.to_thread(store.consume, license_id, receipt)
            raise HTTPException(400, '알 수 없는 인증 작업입니다.')
        except RateLimited:
            raise HTTPException(429, '인증 시도가 너무 많습니다. 10분 후 다시 시도하세요.') from None
        except LicenseError:
            raise HTTPException(403, '키가 만료·폐기되었거나 사용 횟수가 소진되었습니다.') from None
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, '키 저장소에 연결할 수 없습니다. 잠시 후 다시 시도하세요.') from None
        finally:
            self.audit_signal.set()

    async def authenticate_admin(self, request):
        secret = os.getenv('KEY_ADMIN_SECRET', '')
        timestamp = request.headers.get('x-key-time', '')
        nonce = request.headers.get('x-key-nonce', '')
        signature = request.headers.get('x-key-signature', '')
        try:
            fresh = abs(time.time() - int(timestamp)) <= 60
        except ValueError:
            fresh = False
        if (len(secret) < 32 or not fresh or not re.fullmatch(r'[a-f0-9]{32}', nonce)
                or not re.fullmatch(r'[a-f0-9]{64}', signature)
                or request.headers.get('origin')):
            raise HTTPException(401, '관리 요청 인증 실패')
        body = await request.body()
        if len(body) > 16384:
            raise HTTPException(413, '관리 요청이 너무 큽니다.')
        signed = request.method + '\n' + request.url.path + '\n' + timestamp + '\n' + nonce + '\n'
        expected = hmac.new(secret.encode(), signed.encode() + body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise HTTPException(401, '관리 요청 인증 실패')
        try:
            await asyncio.to_thread(self.require_store().accept_nonce, nonce, time.time())
        except LicenseError:
            raise HTTPException(409, '이미 처리한 관리 요청입니다.') from None

    async def close(self):
        if self.store:
            await asyncio.to_thread(self.store.close)
        self.store = None
        self.attempts.clear()
