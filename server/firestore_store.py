"""Firebase ledger. All balance/issuance changes use Firestore transactions."""
import hashlib
import hmac
import json
import secrets
import string
import time

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from .license_store import LicenseError, RateLimited, Store, expiry, KINDS


class FirestoreStore:
    require_owner = staticmethod(Store.require_owner)

    def __init__(self, client, pepper, namespace='padlet_license_v2'):
        if len(pepper) < 32:
            raise ValueError('A server-only key hash secret is required')
        self.client, self.pepper = client, pepper.encode()
        self.root = client.collection(namespace).document('ledger')
        self.meta = self.root.collection('settings').document('epoch')

    def ref(self, collection, document):
        return self.root.collection(collection).document(str(document))

    def tx(self, callback):
        return firestore.transactional(callback)(self.client.transaction(max_attempts=8))

    @staticmethod
    def read(tx, ref):
        return ref.get(transaction=tx, timeout=8).to_dict()

    def epoch(self, tx):
        return (self.read(tx, self.meta) or {}).get('value', 0)

    def user_version(self, tx, user):
        return (self.read(tx, self.ref('users', user)) or {}).get('version', 0)

    def digest(self, key):
        return hmac.new(self.pepper, key.strip().upper().encode(), hashlib.sha256).hexdigest()

    def event(self, tx, event_type, **payload):
        identifier = secrets.token_hex(12)
        now = time.time()
        event = {'id': identifier, 'kind': event_type, 'payload': json.dumps(payload), 'created': now}
        tx.create(self.ref('audit_events', identifier), event)
        tx.create(self.ref('outbox', identifier), {**event, 'dispatch_at': now})

    def license_event(self, tx, kind, row):
        self.event(tx, kind, **{name: row[name] for name in ('id', 'user_id', 'remaining', 'expires', 'kind', 'amount')})

    def valid(self, row, epoch, now):
        if not row or row.get('epoch') != epoch:
            raise LicenseError('폐기된 키입니다.')
        Store.valid(row, now)

    @staticmethod
    def balance(row):
        return {'expires': row['expires'], 'remaining': row['remaining']}

    def grant(self, actor, user, kind, amount, quantity=1):
        self.require_owner(actor)
        if kind not in KINDS or not 1 <= amount <= 36500 or not 1 <= quantity <= 100:
            raise LicenseError('발급 설정을 확인하세요.')
        if kind in {'month', 'year', 'forever'} and amount != 1:
            raise LicenseError('달·년·영구 발급 단위는 1입니다.')
        identifier = secrets.randbelow(2**63 - 1) + 1
        def commit(tx):
            epoch, version = self.epoch(tx), self.user_version(tx, user)
            tx.create(self.ref('grants', identifier), {
                'id': identifier, 'user_id': str(user), 'kind': kind, 'amount': amount,
                'remaining': quantity, 'created': time.time(), 'epoch': epoch, 'version': version,
                'available_scope': f'{epoch}:{user}:{version}'})
            self.event(tx, 'grant', user_id=str(user), kind=kind, amount=amount, quantity=quantity, actor=str(actor))
            return identifier
        return self.tx(commit)

    def available(self, user):
        def query(tx):
            epoch, version = self.epoch(tx), self.user_version(tx, user)
            query = self.root.collection('grants').where(filter=FieldFilter('available_scope', '==', f'{epoch}:{user}:{version}')).limit(25)
            return [s.to_dict() for s in query.stream(transaction=tx, timeout=8)]
        return self.tx(query)

    def prepare(self, user, grant_id):
        identifier = secrets.token_urlsafe(24)
        def commit(tx):
            epoch, version = self.epoch(tx), self.user_version(tx, user)
            grant_ref = self.ref('grants', grant_id)
            grant = self.read(tx, grant_ref)
            if (not grant or grant['user_id'] != str(user) or grant['epoch'] != epoch
                    or grant['version'] != version or grant['remaining'] <= 0):
                raise LicenseError('사용할 수 있는 발급권이 없습니다.')
            for _ in range(100):
                key = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(5))
                if not any(c.isalpha() for c in key) or not any(c.isdigit() for c in key):
                    continue
                key_ref = self.ref('key_hashes', self.digest(key))
                if not self.read(tx, key_ref):
                    break
            else:
                raise LicenseError('키 생성에 실패했습니다.')
            now = time.time()
            row = {'id': identifier, 'user_id': str(user), 'grant_id': grant_id,
                   'kind': grant['kind'], 'amount': grant['amount'], 'epoch': epoch,
                   'remaining': grant['amount'] if grant['kind'] == 'uses' else None,
                   'expires': expiry(grant['kind'], grant['amount'], now), 'state': 'pending', 'created': now}
            tx.create(self.ref('licenses', identifier), row)
            tx.create(key_ref, {'license_id': identifier})
            tx.create(self.ref('pending_delivery', identifier), {'recover_at': now + 600})
            remaining = grant['remaining'] - 1
            tx.update(grant_ref, {'remaining': remaining, 'available_scope': grant['available_scope'] if remaining else ''})
            return {'key': key, 'id': identifier, **self.balance(row)}
        return self.tx(commit)

    def owner_for(self, license_id):
        row = self.ref('licenses', license_id).get(timeout=8).to_dict()
        return row['user_id'] if row else None

    def delivered(self, license_id, user=None):
        def commit(tx):
            epoch = self.epoch(tx)
            ref = self.ref('licenses', license_id)
            row = self.read(tx, ref)
            if not row or row['epoch'] != epoch or user is not None and row['user_id'] != str(user):
                raise LicenseError('유효하지 않은 발급 요청입니다.')
            if row['state'] == 'pending':
                tx.update(ref, {'state': 'active'})
                tx.delete(self.ref('pending_delivery', license_id))
                self.license_event(tx, 'issued', row)
        return self.tx(commit)

    def delivery_failed(self, license_id=None, user=None, *, older_than=None):
        if license_id is None:
            # One small indexed batch per maintenance cycle, never a full collection scan.
            cutoff = (older_than + 600) if older_than is not None else time.time()
            docs = self.root.collection('pending_delivery').where(filter=FieldFilter('recover_at', '<=', cutoff)).limit(25).stream(timeout=8)
            for snapshot in docs:
                self.delivery_failed(snapshot.id)
            return
        def commit(tx):
            epoch = self.epoch(tx)
            ref = self.ref('licenses', license_id)
            row = self.read(tx, ref)
            if not row:
                return
            if user is not None and row['user_id'] != str(user):
                raise LicenseError('발급 대상이 다릅니다.')
            grant_ref = self.ref('grants', row['grant_id'])
            grant = self.read(tx, grant_ref)
            version = self.user_version(tx, row['user_id'])
            if row['state'] == 'pending':
                tx.update(ref, {'state': 'revoked'})
                if grant and grant['epoch'] == epoch and grant['version'] == version:
                    tx.update(grant_ref, {'remaining': grant['remaining'] + 1,
                                         'available_scope': f'{epoch}:{row["user_id"]}:{version}'})
            tx.delete(self.ref('pending_delivery', license_id))
        return self.tx(commit)

    def verify_key(self, key, client_id):
        now = time.time()
        def attempts(tx):
            buckets = [(self.ref('attempts', hashlib.sha256(client_id.encode()).hexdigest()), 600, 5),
                       (self.ref('attempts', 'global'), 60, 60)]
            rows = [self.read(tx, ref) for ref, _, _ in buckets]
            limited = False
            for (ref, seconds, maximum), row in zip(buckets, rows):
                current = row if row and now - row['start'] < seconds else {'start': now, 'count': 0}
                limited |= current['count'] >= maximum
                # Capped counters avoid attacker-controlled integer growth.
                tx.set(ref, {'start': current['start'], 'count': min(maximum + 1, current['count'] + 1), 'cleanup_at': now + 86400})
            return limited
        if self.tx(attempts):
            raise RateLimited('인증 시도가 너무 많습니다. 10분 후 다시 시도하세요.')
        def verify(tx):
            epoch = self.epoch(tx)
            mapping = self.read(tx, self.ref('key_hashes', self.digest(key)))
            row = self.read(tx, self.ref('licenses', mapping['license_id'])) if mapping else None
            self.valid(row, epoch, time.time())
            self.license_event(tx, 'login', row)
            return {'license_id': row['id'], **self.balance(row)}
        return self.tx(verify)

    def check(self, license_id):
        def check(tx):
            epoch = self.epoch(tx)
            row = self.read(tx, self.ref('licenses', license_id))
            self.valid(row, epoch, time.time())
            return self.balance(row)
        return self.tx(check)

    def consume(self, license_id, request_id):
        def commit(tx):
            epoch = self.epoch(tx)
            ref, receipt_ref = self.ref('licenses', license_id), self.ref('charges', request_id)
            row, receipt = self.read(tx, ref), self.read(tx, receipt_ref)
            if receipt:
                if (receipt['license_id'] != license_id or not row or row['epoch'] != epoch
                        or row['state'] != 'active' or row['expires'] is not None and time.time() >= row['expires']):
                    raise LicenseError('유효하지 않은 실행 요청입니다.')
                return self.balance(row)
            self.valid(row, epoch, time.time())
            if row['remaining'] is not None:
                row['remaining'] -= 1
                tx.update(ref, {'remaining': row['remaining']})
            tx.create(receipt_ref, {'license_id': license_id, 'created': time.time()})
            self.license_event(tx, 'used', row)
            return self.balance(row)
        return self.tx(commit)

    def revoke(self, actor, license_id):
        self.require_owner(actor)
        def commit(tx):
            ref = self.ref('licenses', license_id)
            row = self.read(tx, ref)
            if not row or row['state'] not in {'active', 'pending'}:
                return 0
            tx.update(ref, {'state': 'revoked'})
            tx.delete(self.ref('pending_delivery', license_id))
            self.license_event(tx, 'revoked', row)
            return 1
        return self.tx(commit)

    def revoke_grants(self, actor, user):
        self.require_owner(actor)
        def commit(tx):
            version = self.user_version(tx, user)
            tx.set(self.ref('users', user), {'version': version + 1})
            self.event(tx, 'grants_revoked', user_id=str(user), actor=str(actor))
        return self.tx(commit)

    def revoke_all(self, actor):
        self.require_owner(actor)
        def commit(tx):
            epoch = self.epoch(tx)
            tx.set(self.meta, {'value': epoch + 1})
            self.event(tx, 'all_revoked', actor=str(actor), scope='전체 키·미사용 발급권')
        self.tx(commit)
        return None  # An atomic generation change, not a misleading counted batch deletion.

    def accept_nonce(self, nonce, now):
        def commit(tx):
            ref = self.ref('admin_nonces', nonce)
            if self.read(tx, ref):
                raise LicenseError('이미 처리한 관리 요청입니다.')
            tx.create(ref, {'cleanup_at': now + 86400})
        return self.tx(commit)

    def set_webhook(self, url):
        self.ref('settings', 'log_webhook').set({'url': url}, timeout=8)

    def webhook(self):
        row = self.ref('settings', 'log_webhook').get(timeout=8).to_dict()
        return row['url'] if row else None

    def next_event(self):
        def claim(tx):
            query = self.root.collection('outbox').where(filter=FieldFilter('dispatch_at', '<=', time.time())).order_by('dispatch_at').limit(1)
            docs = list(query.stream(transaction=tx, timeout=8))
            if not docs:
                return None
            tx.update(docs[0].reference, {'dispatch_at': time.time() + 60})
            return docs[0].to_dict()
        return self.tx(claim)

    def finish_event(self, event_id, delivered, retry_after=30):
        batch = self.client.batch()
        if delivered:
            batch.update(self.ref('audit_events', event_id), {'sent': True})
            batch.delete(self.ref('outbox', event_id))
        else:
            batch.update(self.ref('outbox', event_id), {'dispatch_at': time.time() + retry_after})
        batch.commit(timeout=8)

    def ticket(self, guild, user, channel=None):
        ref = self.ref('tickets', f'{guild}:{user}')
        if channel is not None:
            ref.set({'channel': str(channel)}, timeout=8)
        row = ref.get(timeout=8).to_dict()
        return int(row['channel']) if row else None

    def maintenance(self):
        self.delivery_failed(older_than=time.time() - 600)
        for collection in ('attempts', 'admin_nonces'):
            docs = list(self.root.collection(collection).where(filter=FieldFilter('cleanup_at', '<=', time.time())).limit(100).stream(timeout=8))
            if docs:
                batch = self.client.batch()
                for doc in docs:
                    batch.delete(doc.reference)
                batch.commit(timeout=8)

    def close(self):
        self.client.close()
