import asyncio
import hashlib
import hmac
import json
import secrets
import time
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from server.license_store import Store, OWNER_ID, LicenseError
from server.audit import event_message

SECRET = 'test-management-credential-' * 3


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    from server.app import key_access
    store = Store(tmp_path / 'server.sqlite3', 'test-server-pepper-' * 3)
    monkeypatch.setattr(key_access, 'store', store)
    monkeypatch.setenv('KEY_ADMIN_SECRET', SECRET)
    return store


def signed(action, payload, stamp=None, nonce=None):
    body = json.dumps(payload).encode()
    stamp, nonce = str(int(time.time()) if stamp is None else stamp), nonce or secrets.token_hex(16)
    path = '/internal/licenses/' + action
    message = ('POST\n' + path + '\n' + stamp + '\n' + nonce + '\n').encode() + body
    signature = hmac.new(SECRET.encode(), message, hashlib.sha256).hexdigest()
    return {'url': path, 'content': body, 'headers': {'Content-Type': 'application/json',
            'X-Key-Time': stamp, 'X-Key-Nonce': nonce, 'X-Key-Signature': signature}}


def issue(store, amount=1, kind='uses'):
    item = store.prepare(OWNER_ID, store.grant(OWNER_ID, OWNER_ID, kind, amount))
    store.delivered(item['id'])
    return item


@pytest.mark.parametrize('amount', [1, 2, 3])
def test_login_run_stop_repeat_without_bot(ledger, monkeypatch, amount):
    from server.app import app
    from server.engine import Sender
    launched = []

    async def job(sender, *args):
        launched.append(1)
        sender.opening, sender.running = False, True
        try:
            await sender.stop_event.wait()
        finally:
            sender.running = False
            sender.phase = 'stopped'

    monkeypatch.setattr(Sender, 'automatic_run', job)
    key = issue(ledger, amount)
    with TestClient(app) as client:
        # No Discord process, socket or mocked authorization response exists here.
        origin = {'Origin': 'https://dev-cat0.github.io'}
        login = client.post('/api/auth/login', headers=origin, json={'key': key['key']})
        assert login.status_code == 200 and set(login.json()) == {'token'}
        assert key['key'] not in login.text
        second = client.post('/api/auth/login', headers=origin, json={'key': key['key']}).json()['token']
        headers = {'Authorization': 'Bearer ' + login.json()['token']}
        other = {'Authorization': 'Bearer ' + second}
        for index in range(amount + 2):
            auth = other if index > amount else headers
            run_headers = {**auth, 'X-Request-ID': f'click-{index:020}'}
            response = client.post('/api/run', headers=run_headers, json={'url': 'https://padlet.com/example/board'})
            assert response.status_code == (200 if index < amount else 403)
            if index < amount:
                assert response.json()['access']['remaining'] == amount - index - 1
                assert client.post('/api/stop', headers=headers, json={}).status_code == 200
                for _ in range(50):
                    if not client.get('/api/state', headers=headers).json()['running']:
                        break
                    time.sleep(.01)
        assert len(launched) == amount
        for auth in (headers, other):
            state = client.get('/api/state', headers=auth).json()
            assert state['access']['remaining'] == 0 and not state['access']['can_run']
        # Replay of an already accepted request must not launch another job.
        replay = client.post('/api/run', headers={**headers, 'X-Request-ID': 'click-00000000000000000000'},
                             json={'url': 'https://padlet.com/example/board'})
        assert replay.status_code == 200 and len(launched) == amount
        assert client.post('/api/auth/login', headers=origin, json={'key': key['key']}).status_code == 403
    reloaded = Store(ledger.path, 'test-server-pepper-' * 3)
    with pytest.raises(LicenseError):
        reloaded.check(key['id'])
    with reloaded.db() as db:
        events = list(db.execute("SELECT * FROM audit_events WHERE kind='used'"))
        assert len(events) == amount
        assert key['key'] not in str([dict(e) for e in events])


def test_expiry_stops_job_and_existing_session_cannot_run(ledger, monkeypatch):
    from server.app import app, sessions
    from server.engine import Sender
    key = issue(ledger, kind='days')
    deadline = time.time() + .5
    with ledger.db() as db:
        db.execute('UPDATE licenses SET expires=? WHERE id=?', (deadline, key['id']))

    async def job(sender, *args):
        sender.opening, sender.running = False, True
        try:
            await sender.stop_event.wait()
        finally:
            sender.running = False
            sender.phase = 'stopped'

    monkeypatch.setattr(Sender, 'automatic_run', job)
    with TestClient(app) as client:
        token = sessions.create(license_id=key['id'])
        headers = {'Authorization': 'Bearer ' + token, 'X-Request-ID': 'time-key-run-000001'}
        data = {'url': 'https://padlet.com/example/board'}
        assert client.post('/api/run', headers=headers, json=data).status_code == 200
        for _ in range(100):
            state = client.get('/api/state', headers=headers).json()
            if state['access']['expired'] and not state['running']:
                break
            time.sleep(.01)
        assert state['access']['expired'] and not state['running'] and not state['access']['can_run']
        assert client.post('/api/run', headers={**headers, 'X-Request-ID': 'time-key-run-000002'}, json=data).status_code == 403


def test_expiry_guard_prevents_post_without_timer():
    from server.engine import Sender
    async def scenario():
        sender = Sender()
        sender.license_expires = time.time() - 1
        sender.http = AsyncMock()
        assert await sender.send_section(None, '', '', asyncio.Semaphore(1), asyncio.Event()) == ('pending', 0)
        sender.http.post.assert_not_called()
    asyncio.run(scenario())


def test_signed_admin_owner_binding_replay_and_old_bot_rejected(ledger):
    from server.app import app
    with TestClient(app) as client:
        payload = {'actor': OWNER_ID, 'user': 111, 'kind': 'uses', 'amount': 1}
        assert client.post('/internal/licenses/grant', json=payload).status_code == 401
        assert client.post(**signed('grant', payload, stamp=1)).status_code == 401
        request = signed('grant', payload)
        result = client.post(**request)
        assert result.status_code == 200
        assert client.post(**request).status_code == 409
        assert client.post(**signed('grant', {**payload, 'actor': 111})).status_code == 403
        grant_id = result.json()['id']
        assert client.post(**signed('prepare', {'actor': 222, 'user': 111, 'grant_id': grant_id})).status_code == 403
        item = client.post(**signed('prepare', {'actor': 111, 'user': 111, 'grant_id': grant_id})).json()
        assert client.post(**signed('delivered', {'actor': 222, 'license_id': item['id']})).status_code == 403
        assert client.post(**signed('delivered', {'actor': 111, 'license_id': item['id']})).status_code == 200
        assert client.post(**signed('revoke_all', {'actor': 111})).status_code == 403
        assert client.post(**signed('revoke_all', {'actor': OWNER_ID})).json()['count'] == 1
        with pytest.raises(LicenseError):
            ledger.check(item['id'])
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect('/internal/key-agent', headers={'Authorization': 'Bearer old-secret'}):
                pass


def test_audit_outbox_durable_and_human_readable(ledger):
    count_key = issue(ledger, 2)
    time_key = issue(ledger, kind='days')
    forever = issue(ledger, kind='forever')
    for item in (count_key, time_key, forever):
        ledger.consume(item['id'], secrets.token_hex(16))
    with ledger.db() as db:
        events = [dict(r) for r in db.execute("SELECT * FROM audit_events WHERE kind='used' ORDER BY id")]
    messages = [event_message(e) for e in events]
    assert '남은 실행: 1회' in messages[0]['content']
    assert '시간' in messages[1]['content'] and '분' in messages[1]['content'] and '초' in messages[1]['content']
    assert '영구' in messages[2]['content']
    assert all(m['allowed_mentions'] == {'parse': []} for m in messages)
    assert all(item['key'] not in str(messages) for item in (count_key, time_key, forever))
    pending = ledger.next_event()
    ledger.finish_event(pending['id'], False, retry_after=0)
    reloaded = Store(ledger.path, 'test-server-pepper-' * 3)
    assert reloaded.next_event()['id'] == pending['id']
    ledger.finish_event(pending['id'], True)
    assert reloaded.next_event()['id'] != pending['id']


def test_local_log_relay_requires_owner_and_shares_delivery_lease(ledger):
    from server.app import app
    key = issue(ledger)
    with TestClient(app) as client:
        assert client.post('/internal/licenses/audit_next', json={'actor': OWNER_ID}).status_code == 401
        assert client.post(**signed('audit_next', {'actor': 222})).status_code == 403
        response = client.post(**signed('audit_next', {'actor': OWNER_ID}))
        assert response.status_code == 200 and key['key'] not in response.text
        event = response.json()['event']
        other = ledger.next_event()
        assert str(other['id']) != event['id']
        assert client.post(**signed('audit_finish', {'actor': 222, 'event_id': event['id'], 'delivered': True})).status_code == 403
        assert client.post(**signed('audit_finish', {'actor': OWNER_ID, 'event_id': event['id'], 'delivered': True})).status_code == 200
        with ledger.db() as db:
            assert db.execute('SELECT sent FROM audit_events WHERE id=?', (event['id'],)).fetchone()[0] == 1
