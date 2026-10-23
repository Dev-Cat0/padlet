"""Real SDK/transaction tests against the isolated Firebase emulator, never production."""
import asyncio
import json
import os
import secrets
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import httpx
from fastapi.testclient import TestClient
from google.auth.credentials import AnonymousCredentials
from google.cloud import firestore

from server.firestore_store import FirestoreStore
from server.license_store import OWNER_ID, LicenseError

pytestmark = pytest.mark.skipif(not os.getenv('FIRESTORE_EMULATOR_HOST'), reason='Firestore emulator required')


@pytest.fixture
def ledger():
    assert os.environ['FIRESTORE_EMULATOR_HOST'] in {'127.0.0.1:8088', 'localhost:8088'}
    client = firestore.Client(project='demo-padlet-key-tests', credentials=AnonymousCredentials())
    store = FirestoreStore(client, 'emulator-only-hash-secret-' * 3, 'test_' + secrets.token_hex(8))
    yield store
    store.close()


def issue(store, amount=1, kind='uses'):
    key = store.prepare(OWNER_ID, store.grant(OWNER_ID, OWNER_ID, kind, amount))
    store.delivered(key['id'], OWNER_ID)
    return key


def test_browser_cannot_read_or_write_database(ledger):
    key = issue(ledger)
    base = 'http://127.0.0.1:8088/v1/projects/demo-padlet-key-tests/databases/(default)/documents/'
    url = base + ledger.ref('licenses', key['id']).path
    assert httpx.get(url).status_code == 403
    assert httpx.patch(url, json={'fields': {'remaining': {'integerValue': '999'}}}).status_code == 403
    assert ledger.check(key['id'])['remaining'] == 1


def test_simultaneous_issuance_and_run_cannot_overspend(ledger):
    grant = ledger.grant(OWNER_ID, OWNER_ID, 'uses', 1)
    def prepare(_):
        try:
            return ledger.prepare(OWNER_ID, grant)
        except LicenseError:
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        issued = [v for v in pool.map(prepare, range(8)) if v]
    assert len(issued) == 1
    key = issued[0]
    ledger.delivered(key['id'])
    def consume(_):
        try:
            ledger.consume(key['id'], secrets.token_hex(16))
            return True
        except LicenseError:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(consume, range(8))) == 1
    row = ledger.ref('licenses', key['id']).get().to_dict()
    assert row['remaining'] == 0 and key['key'] not in json.dumps(row)
    used = [s.to_dict() for s in ledger.root.collection('audit_events').stream() if s.to_dict()['kind'] == 'used']
    assert len(used) == 1 and json.loads(used[0]['payload'])['remaining'] == 0


def test_receipt_retry_and_reopened_client_keep_balance(ledger):
    key = issue(ledger)
    receipt = secrets.token_hex(16)
    ledger.consume(key['id'], receipt)
    client = firestore.Client(project='demo-padlet-key-tests', credentials=AnonymousCredentials())
    reopened = FirestoreStore(client, 'emulator-only-hash-secret-' * 3, ledger.root.parent.id)
    try:
        assert reopened.consume(key['id'], receipt)['remaining'] == 0
        with pytest.raises(LicenseError):
            reopened.consume(key['id'], secrets.token_hex(16))
        with pytest.raises(LicenseError):
            reopened.verify_key(key['key'], 'client')
    finally:
        reopened.close()


def test_global_revocation_invalidates_all_previous_generations(ledger):
    key = issue(ledger, kind='forever')
    pending = ledger.prepare(OWNER_ID, ledger.grant(OWNER_ID, OWNER_ID, 'uses', 2))
    untouched = ledger.grant(OWNER_ID, OWNER_ID, 'days', 3)
    ledger.revoke_all(OWNER_ID)
    with pytest.raises(LicenseError):
        ledger.check(key['id'])
    with pytest.raises(LicenseError):
        ledger.verify_key(key['key'], 'client')
    with pytest.raises(LicenseError):
        ledger.delivered(pending['id'])
    with pytest.raises(LicenseError):
        ledger.prepare(OWNER_ID, untouched)
    ledger.delivery_failed(pending['id'])
    assert ledger.available(OWNER_ID) == []
    assert ledger.check(issue(ledger)['id'])['remaining'] == 1


def test_delivery_refund_recalled_grants_and_nonce_replay(ledger):
    key = ledger.prepare(OWNER_ID, ledger.grant(OWNER_ID, OWNER_ID, 'uses', 1))
    ledger.delivery_failed(key['id'])
    ledger.delivery_failed(key['id'])
    assert ledger.available(OWNER_ID)[0]['remaining'] == 1
    another = ledger.prepare(OWNER_ID, ledger.available(OWNER_ID)[0]['id'])
    ledger.revoke_grants(OWNER_ID, OWNER_ID)
    ledger.delivery_failed(another['id'])
    assert not ledger.available(OWNER_ID)
    nonce = secrets.token_hex(16)
    ledger.accept_nonce(nonce, time.time())
    with pytest.raises(LicenseError):
        ledger.accept_nonce(nonce, time.time())


def test_time_expiry_and_audit_queue_survive_bot_absence(ledger):
    key = issue(ledger, kind='days')
    ledger.consume(key['id'], secrets.token_hex(16))
    ledger.ref('licenses', key['id']).update({'expires': time.time() - 1})
    with pytest.raises(LicenseError):
        ledger.consume(key['id'], secrets.token_hex(16))
    event = ledger.next_event()
    ledger.finish_event(event['id'], False, 0)
    while True:
        next_item = ledger.next_event()
        assert next_item
        ledger.finish_event(next_item['id'], True)
        if next_item['id'] == event['id']:
            break
    assert ledger.ref('audit_events', event['id']).get().to_dict()['sent'] is True
    assert not ledger.ref('outbox', event['id']).get().exists


def test_render_api_consumes_firestore_without_local_discord(ledger, monkeypatch):
    from server.app import app, key_access
    from server.engine import Sender
    monkeypatch.setattr(key_access, 'store', ledger)
    runs = []
    async def job(sender, *args):
        runs.append(1)
        sender.opening, sender.running = False, True
        try:
            await sender.stop_event.wait()
        finally:
            sender.running = False
            sender.phase = 'stopped'
    monkeypatch.setattr(Sender, 'automatic_run', job)
    key = issue(ledger)
    with TestClient(app) as client:
        response = client.post('/api/auth/login', headers={'Origin': 'https://dev-cat0.github.io'}, json={'key': key['key']})
        assert response.status_code == 200 and set(response.json()) == {'token'}
        headers = {'Authorization': 'Bearer ' + response.json()['token'], 'X-Request-ID': 'firestore-run-000001'}
        data = {'url': 'https://padlet.com/example/board'}
        first = client.post('/api/run', headers=headers, json=data)
        assert first.status_code == 200 and first.json()['access']['remaining'] == 0
        assert client.post('/api/stop', headers=headers, json={}).status_code == 200
        for _ in range(50):
            if not client.get('/api/state', headers=headers).json()['running']:
                break
            time.sleep(.01)
        assert client.post('/api/run', headers={**headers, 'X-Request-ID': 'firestore-run-000002'}, json=data).status_code == 403
        assert len(runs) == 1
