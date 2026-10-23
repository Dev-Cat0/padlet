"""Signed management client. The bot does not store or validate website keys."""
import hashlib
import hmac
import json
import secrets
import time
from datetime import timedelta, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen

OWNER_ID = 481652220618604544
LOG_CHANNEL_ID = 1562751204935602258
KST = timezone(timedelta(hours=9))


class LicenseError(ValueError):
    pass


class RemoteStore:
    def __init__(self, url, secret):
        if not url.startswith('https://') or len(secret) < 32:
            raise ValueError('HTTPS server and management secret are required')
        self.url, self.secret = url.rstrip('/'), secret
        self.connected = False

    @staticmethod
    def require_owner(actor):
        if int(actor) != OWNER_ID:
            raise LicenseError('관리자만 사용할 수 있습니다.')

    def call(self, action, actor=OWNER_ID, **data):
        path = '/internal/licenses/' + action
        body = json.dumps({'actor': actor, **data}, separators=(',', ':')).encode()
        stamp, nonce = str(int(time.time())), secrets.token_hex(16)
        message = ('POST\n' + path + '\n' + stamp + '\n' + nonce + '\n').encode() + body
        signature = hmac.new(self.secret.encode(), message, hashlib.sha256).hexdigest()
        request = Request(self.url + path, data=body, headers={
            'Content-Type': 'application/json', 'X-Key-Time': stamp,
            'X-Key-Nonce': nonce, 'X-Key-Signature': signature})
        try:
            with urlopen(request, timeout=45) as response:
                result = json.load(response)
            self.connected = True
            return result
        except HTTPError as error:
            self.connected = False
            if error.code in {401, 403, 409}:
                raise LicenseError('요청이 거부되었습니다. 관리자 권한·발급권·서버 연결 설정을 확인하세요.') from None
            raise LicenseError('키 서버가 응답하지 않습니다. 잠시 후 다시 시도하세요.') from None
        except Exception:
            self.connected = False
            raise LicenseError('키 서버 연결 실패. 잠시 후 다시 시도하세요.') from None

    def grant(self, actor, user, kind, amount, quantity=1):
        return self.call('grant', actor, user=user, kind=kind, amount=amount, quantity=quantity)['id']

    def available(self, user):
        return self.call('available', user, user=user)['grants']

    def prepare(self, user, grant_id):
        return self.call('prepare', user, user=user, grant_id=grant_id)

    def delivered(self, license_id, user):
        return self.call('delivered', user, license_id=license_id)

    def delivery_failed(self, license_id, user):
        return self.call('delivery_failed', user, license_id=license_id)

    def revoke(self, actor, license_id):
        return self.call('revoke', actor, license_id=license_id)['count']

    def revoke_grants(self, actor, user):
        return self.call('revoke_grants', actor, user=user)

    def ticket(self, guild, user, channel=None):
        return self.call('ticket', user, user=user, guild=guild, channel=channel)['channel']
