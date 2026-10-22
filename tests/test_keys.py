import asyncio
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from keybot.store import KST, OWNER_ID, LicenseError, Store, expiry
from server.key_access import KeyAccess


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / 'licenses.sqlite3', 'test-only-pepper-' * 3)


def issue(store, user=111, kind='uses', amount=1):
    grant = store.grant(OWNER_ID, user, kind, amount)
    key = store.prepare(user, grant)
    store.delivered(key['id'])
    return key


def test_owner_only_grants_and_bound_recipient(store):
    with pytest.raises(LicenseError):
        store.grant(123, 111, 'uses', 1)
    grant = store.grant(OWNER_ID, 111, 'uses', 3)
    with pytest.raises(LicenseError):
        store.prepare(222, grant)
    key = store.prepare(111, grant)
    assert len(key['key']) == 5 and key['key'].isalnum()
    assert any(c.isdigit() for c in key['key']) and any(c.isalpha() for c in key['key'])
    with pytest.raises(LicenseError):
        store.verify(111, key['key'])  # pending DM is not usable
    store.delivered(key['id'])
    with pytest.raises(LicenseError):
        store.verify(222, key['key'])
    assert store.verify(111, key['key'].lower())['license_id'] == key['id']
    with store.db() as db:
        assert key['key'] not in str([tuple(r) for r in db.execute('SELECT * FROM licenses')])


def test_delivery_failure_and_restart_restore_exactly_one_grant(store):
    grant = store.grant(OWNER_ID, 111, 'days', 3)
    key = store.prepare(111, grant)
    assert not store.available(111)
    store.delivery_failed()
    store.delivery_failed(key['id'])
    assert store.available(111)[0]['remaining'] == 1
    with pytest.raises(LicenseError):
        store.verify(111, key['key'])


def test_concurrent_issue_and_consume_cannot_overspend(store):
    grant = store.grant(OWNER_ID, 111, 'uses', 1)
    def prepare(_):
        try:
            return store.prepare(111, grant)
        except LicenseError:
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        keys = [v for v in pool.map(prepare, range(16)) if v]
    assert len(keys) == 1
    key = keys[0]
    store.delivered(key['id'])
    def consume(i):
        try:
            store.consume(key['id'], f'request-{i:020}')
            return True
        except LicenseError:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(consume, range(16))) == 1
    with store.db() as db:
        assert db.execute('SELECT remaining FROM licenses').fetchone()[0] == 0


def test_charge_retry_is_idempotent_and_survives_restart(store):
    key = issue(store)
    request = secrets.token_urlsafe(32)
    store.consume(key['id'], request)
    reloaded = Store(store.path, 'test-only-pepper-' * 3)
    reloaded.consume(key['id'], request)
    with pytest.raises(LicenseError):
        reloaded.consume(key['id'], secrets.token_urlsafe(32))
    second = issue(store, user=222)
    with pytest.raises(LicenseError):
        reloaded.consume(second['id'], request)


def test_time_kst_boundaries_month_year_and_expiration(store, monkeypatch):
    start = datetime(2028, 1, 31, 23, 5, tzinfo=KST).timestamp()
    assert datetime.fromtimestamp(expiry('month', 1, start), KST) == datetime(2028, 2, 29, 23, 5, tzinfo=KST)
    leap = datetime(2028, 2, 29, 9, 0, tzinfo=KST).timestamp()
    assert datetime.fromtimestamp(expiry('year', 1, leap), KST) == datetime(2029, 2, 28, 9, 0, tzinfo=KST)
    assert expiry('days', 7, start) - start == 7 * 86400
    assert expiry('forever', 1, start) is None
    monkeypatch.setattr('keybot.store.time.time', lambda: start)
    key = issue(store, kind='days', amount=1)
    monkeypatch.setattr('keybot.store.time.time', lambda: start + 86400)
    with pytest.raises(LicenseError):
        store.consume(key['id'], 'a' * 32)


def test_revocation_and_guess_limit(store):
    key = issue(store, kind='forever')
    for _ in range(5):
        with pytest.raises(LicenseError):
            store.verify(111, '00000')
    with pytest.raises(LicenseError, match='10분'):
        store.verify(111, key['key'])
    with pytest.raises(LicenseError):
        store.revoke(222, key['id'])
    assert store.revoke(OWNER_ID, key['id']) == 1
    with pytest.raises(LicenseError):
        store.check(key['id'])


def test_login_requires_agent_and_limits_requests():
    access = KeyAccess()
    with pytest.raises(HTTPException) as error:
        access.login_limit('peer')
    assert error.value.status_code == 503
    access.socket = object()
    for _ in range(5):
        assert len(access.login_limit('peer')) == 64
    with pytest.raises(HTTPException) as error:
        access.login_limit('peer')
    assert error.value.status_code == 429


def test_real_websocket_key_login_returns_only_random_session(monkeypatch, store):
    from server.app import app, sessions
    key = issue(store, amount=2)
    secret = 'test-agent-secret-' * 3
    monkeypatch.setenv('KEY_AGENT_SECRET', secret)
    origin = {'Origin': 'https://dev-cat0.github.io'}
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect('/internal/key-agent'):
                pass
        assert client.post('/api/session', headers=origin, json={}).status_code == 403
        assert client.post('/api/auth/login', headers=origin, json={'key': key['key']}).status_code == 503
        assert client.post('/api/auth/login', headers={'Origin':'https://evil.example'}, json={'key':key['key']}).status_code == 403
        malformed = client.post('/api/auth/login', headers=origin, json={'key': {'secret': key['key']}})
        assert malformed.status_code == 422 and key['key'] not in malformed.text
        with client.websocket_connect('/internal/key-agent', headers={'Authorization': 'Bearer ' + secret}) as ws:
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(client.post, '/api/auth/login', headers=origin, json={'key':key['key']})
                message = ws.receive_json()
                assert message['action'] == 'verify_key' and message['key'] == key['key']
                result = store.verify_key(message['key'], message['client_id'])
                ws.send_json({'id':message['id'], 'ok':True, **result})
                response = pending.result(timeout=5)
                assert response.status_code == 200
                assert set(response.json()) == {'token'}
                token = response.json()['token']
                assert len(token) >= 40 and key['key'] not in response.text
                assert sessions.items[token].license_id == key['id']
                state = client.get('/api/state',headers={'Authorization':'Bearer '+token})
                assert state.status_code == 200 and key['key'] not in state.text
                with store.db() as db:
                    assert db.execute('SELECT remaining FROM licenses').fetchone()[0] == 2


def test_run_charges_only_after_validation_and_deduplicates(monkeypatch, store):
    from server.app import app, sessions, key_access
    from server.engine import Sender
    key = issue(store, amount=2)
    async def rpc(action, license_id, request_id=''):
        return {'ok': True, **store.consume(license_id, request_id)}
    monkeypatch.setattr(key_access, 'rpc', rpc)
    monkeypatch.setattr(Sender, 'automatic_run', AsyncMock())
    with TestClient(app) as client:
        token = sessions.create(license_id=key['id'])
        headers = {'Authorization': 'Bearer ' + token, 'X-Request-ID': 'one-request-00000001'}
        data = {'url': 'https://padlet.com/example/board', 'subject': 'Hi', 'body': '', 'limit': 1, 'delay': 0}
        assert client.post('/api/run', headers=headers, json={**data, 'url': 'https://evil.example/board'}).status_code == 409
        with store.db() as db:
            assert db.execute('SELECT remaining FROM licenses').fetchone()[0] == 2
        assert client.post('/api/run', headers=headers, json=data).status_code == 200
        assert client.post('/api/run', headers=headers, json=data).status_code == 200
        assert client.post('/api/run', headers=headers, json={**data, 'body': 'changed'}).status_code == 409
        assert client.post('/api/run', headers={**headers, 'X-Request-ID': 'two-request-00000002'}, json=data).status_code == 409
        with store.db() as db:
            assert db.execute('SELECT remaining FROM licenses').fetchone()[0] == 1


def test_exhausted_or_offline_cannot_start_but_can_stop_and_delete(monkeypatch):
    from server.app import app, sessions, key_access
    with TestClient(app) as client:
        token = sessions.create(license_id='opaque-license')
        headers = {'Authorization': 'Bearer ' + token, 'X-Request-ID': 'request-0000000001'}
        sender = sessions.items[token].sender
        sender.queue_delete = AsyncMock()
        data = {'url': 'https://padlet.com/example/board'}
        assert client.post('/api/run', headers=headers, json=data).status_code == 503
        monkeypatch.setattr(key_access, 'rpc', AsyncMock(side_effect=HTTPException(403, 'Exhausted')))
        assert client.post('/api/run', headers=headers, json=data).status_code == 403
        assert client.post('/api/stop', headers=headers, json={}).status_code == 200
        assert client.post('/api/emergency-delete', headers=headers, json={}).status_code == 200
        assert not sender.opening


def test_expiry_stops_running_job():
    from server.sessions import Sessions
    async def scenario():
        sessions = Sessions()
        token = sessions.create(license_id='opaque-license')
        sender = sessions.items[token].sender
        sender.stop = MagicMock()
        sessions.schedule_expiry(sender, time.time() - 1)
        await asyncio.sleep(.02)
        sender.stop.assert_called_once()
        await sessions.shutdown()
    asyncio.run(scenario())


def test_discord_commands_panels_and_permissions(store):
    from keybot.bot import Bot, IssuePanel, TicketPanel, ticket_overwrites
    async def scenario():
        bot = Bot(store, SimpleNamespace(), 123)
        for panel in (IssuePanel(bot), TicketPanel(bot)):
            assert panel.is_persistent()
        commands = {c.name: c for c in bot.tree.get_commands()}
        for name, command in commands.items():
            with pytest.raises(LicenseError):
                await command.checks[0](SimpleNamespace(user=SimpleNamespace(id=222), guild_id=123))
            assert await command.checks[0](SimpleNamespace(user=SimpleNamespace(id=OWNER_ID), guild_id=123))
        role, owner, user, service = object(), object(), object(), object()
        perms = ticket_overwrites(SimpleNamespace(default_role=role), owner, user, service)
        assert set(perms) == {role, owner, user, service}
        assert perms[role].view_channel is False
        assert all(perms[member].view_channel for member in (owner, user, service))
        await bot.close()
    asyncio.run(scenario())


def test_dm_failure_returns_grant_without_disclosing_key(store):
    from keybot.bot import Bot
    async def scenario():
        bot = Bot(store, SimpleNamespace(), 123)
        grant = store.grant(OWNER_ID, 111, 'uses', 2)
        interaction = SimpleNamespace(followup=SimpleNamespace(send=AsyncMock()),
                                      response=SimpleNamespace(is_done=lambda: True))
        recipient = SimpleNamespace(id=111, send=AsyncMock(side_effect=RuntimeError('DM disabled')))
        await bot.deliver(interaction, recipient, grant)
        assert store.available(111)[0]['remaining'] == 1
        key = recipient.send.await_args.args[0].split('`')[1]
        assert key not in str(interaction.followup.send.await_args_list)
        await bot.close()
    asyncio.run(scenario())


def test_recalled_grant_is_not_restored_by_late_dm_failure(store):
    grant = store.grant(OWNER_ID, 111, 'uses', 1)
    key = store.prepare(111, grant)
    store.revoke_grants(OWNER_ID, 111)
    store.delivery_failed(key['id'])
    assert store.available(111) == []


def test_one_agent_process_per_database(tmp_path):
    from keybot.instance import single_instance
    path = tmp_path / 'agent.lock'
    with single_instance(path):
        with pytest.raises(SystemExit):
            with single_instance(path):
                pass
    with single_instance(path):
        pass


def test_actual_server_bridge_charge_round_trip(monkeypatch, store):
    from server.app import app, sessions
    from server.engine import Sender
    key = issue(store)
    secret = 'bridge-test-secret-' * 3
    monkeypatch.setenv('KEY_AGENT_SECRET', secret)
    monkeypatch.setattr(Sender, 'automatic_run', AsyncMock())
    with TestClient(app) as client:
        token = sessions.create(license_id=key['id'])
        with client.websocket_connect('/internal/key-agent', headers={'Authorization': 'Bearer ' + secret}) as ws:
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(client.post, '/api/run',
                                      headers={'Authorization': 'Bearer ' + token, 'X-Request-ID': 'bridge-request-00000001'},
                                      json={'url': 'https://padlet.com/example/board'})
                message = ws.receive_json()
                assert message['action'] == 'consume'
                assert key['key'] not in str(message)
                result = store.consume(message['license_id'], message['request_id'])
                ws.send_json({'id': message['id'], 'ok': True, **result})
                response = pending.result(timeout=5)
                assert response.status_code == 200
                assert key['key'] not in response.text
    with pytest.raises(LicenseError):
        store.check(key['id'])
