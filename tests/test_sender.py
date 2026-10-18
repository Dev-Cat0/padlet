import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from server.app import app, sessions
import server.engine as engine
from server.engine import Sender, is_capture_url, make_payload, padlet_url


@pytest.fixture
def client():
    with TestClient(app) as client:
        yield client


def session_auth(client):
    response = client.post('/api/session', headers={'Origin':'https://dev-cat0.github.io'}, json={})
    assert response.status_code == 200
    return {'Authorization':'Bearer ' + response.json()['token']}


def test_private_endpoints_fail_closed(client):
    assert client.get('/healthz').status_code == 200
    for route in ['/api/state', '/api/browser/frame', '/api/browser/tabs']:
        assert client.get(route).status_code == 401
    for route in ['/api/start', '/api/stop', '/api/reset', '/api/browser/open', '/api/browser/action']:
        assert client.post(route, json={}).status_code == 401
    assert client.get('/api/state', headers={'Authorization':'Bearer unknown-session'}).status_code == 401


def test_state_hides_secrets_and_cors(client):
    auth = session_auth(client)
    response = client.get('/api/state', headers={**auth,'Origin':'https://dev-cat0.github.io'})
    assert response.status_code == 200
    assert response.headers['access-control-allow-origin'] == 'https://dev-cat0.github.io'
    assert response.headers['cache-control'] == 'no-store'
    assert not {'headers','template','endpoint'} & response.json().keys()
    response = client.get('/api/state', headers={**auth,'Origin':'https://evil.example'})
    assert 'access-control-allow-origin' not in response.headers


def test_start_validates_input_and_capture(client):
    auth = session_auth(client)
    assert client.post('/api/start', headers=auth, json={'subject':'ok'}).status_code == 409
    for values in [{'subject':' '}, {'subject':'x','delay':-1}, {'subject':'x','limit':-1}, {'subject':'x','limit':1.5}]:
        assert client.post('/api/start', headers=auth, json=values).status_code == 422


def test_automatic_sessions_are_isolated_and_resume(client):
    first, second = session_auth(client), session_auth(client)
    assert first != second
    first_token = first['Authorization'].split()[1]
    first_sender = sessions.items[first_token].sender
    first_sender.ready = True
    first_sender.template = {'subject':'private'}
    first_sender.log('Only the first session should see this')
    first_state = client.get('/api/state', headers=first).json()
    second_state = client.get('/api/state', headers=second).json()
    assert first_state['ready'] and len(first_state['logs']) == 1
    assert not second_state['ready'] and not second_state['logs']
    assert client.post('/api/reset', headers=second, json={}).status_code == 200
    assert client.get('/api/state', headers=first).json()['ready']


def test_session_creation_checks_origin_and_rate_limit(client):
    assert client.post('/api/session', headers={'Origin':'https://evil.example'}, json={}).status_code == 403
    for _ in range(20):
        session_auth(client)
    assert client.post('/api/session', headers={'Origin':'https://dev-cat0.github.io'}, json={}).status_code == 429


def test_session_cleanup_expires_idle_but_keeps_running():
    from server.sessions import Sessions
    async def scenario():
        registry = Sessions()
        idle, active = registry.create(), registry.create()
        registry.items[idle].last_seen -= 1900
        registry.items[active].last_seen -= 1900
        registry.items[active].sender.running = True
        await registry.collect()
        assert idle not in registry.items and active in registry.items
        await registry.shutdown()
    asyncio.run(scenario())


def test_only_one_remote_browser_at_a_time():
    from fastapi import HTTPException
    from server.sessions import Sessions
    async def scenario():
        registry = Sessions()
        first, second = registry.create(), registry.create()
        registry.items[first].sender.opening = True
        target = registry.items[second].sender
        target.open = AsyncMock()
        with pytest.raises(HTTPException) as error:
            await registry.open(target, 'https://padlet.com/dashboard')
        assert error.value.status_code == 429
        target.open.assert_not_awaited()
        registry.items[first].sender.opening = False
        await registry.open(target, 'https://padlet.com/dashboard')
        target.open.assert_awaited_once()
        await registry.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize('url', ['http://padlet.com/a','https://padlet.com.evil.org/a','https://localhost/a','https://padlet.com:123/a','https://user:password@padlet.com/a'])
def test_target_rejects_other_hosts(url):
    with pytest.raises(ValueError):
        padlet_url(url)


def test_capture_endpoint_exact_match():
    assert is_capture_url('https://padlet.com/api/9/wishes')
    assert not is_capture_url('https://evil.org/api/9/wishes')
    assert not is_capture_url('https://padlet.com/anything?api/9/wishes')


def test_payload_escapes_text_preserves_template():
    template = {'wall_id':1,'nested':{'x':2},'subject':'original'}
    result = make_payload(template, 'New', '<script>alert(1)</script>\nNext')
    assert result['wall_id'] == 1
    assert '&lt;script&gt;' in result['body'] and '<br>Next' in result['body']
    result['nested']['x'] = 3
    assert template['nested']['x'] == 2 and template['subject'] == 'original'
    assert result['created_at'].endswith('Z')


class FakeSession:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = []
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception): raise outcome
        return SimpleNamespace(status_code=outcome, headers={'Retry-After':'12'})


def test_finite_send_and_rate_limit(monkeypatch):
    async def scenario():
        sender = Sender()
        session = FakeSession([201,429,201])
        monkeypatch.setattr(engine, 'AsyncSession', lambda **kwargs: session)
        sender.wait = AsyncMock(return_value=False)
        sender.ready = True
        sender.endpoint = 'https://padlet.com/api/9/wishes'
        await sender.start('Title', 'Body', 2, .3)
        with pytest.raises(ValueError): await sender.start('Duplicate', '', 1, .3)
        await sender.worker
        assert sender.sent == 2 and sender.errors == 1
        assert sender.phase == 'finished' and not sender.running
        assert len(session.calls) == 3
        assert sender.wait.call_args_list[-1].args[0] >= 12
        assert session.calls[0][1]['allow_redirects'] is False
    asyncio.run(scenario())


def test_stop_unlimited_during_wait(monkeypatch):
    async def scenario():
        sender = Sender()
        session = FakeSession([])
        monkeypatch.setattr(engine, 'AsyncSession', lambda **kwargs: session)
        sender.ready = True
        await sender.start('Title', '', 0, 300)
        await asyncio.sleep(.01)
        sender.stop()
        await asyncio.wait_for(sender.worker, 1)
        assert not sender.running and sender.phase == 'stopped'
        assert not session.calls
    asyncio.run(scenario())


@pytest.mark.parametrize('result', [401,403,500,TimeoutError()])
def test_error_stops_without_unsafe_retry(monkeypatch, result):
    async def scenario():
        sender = Sender()
        session = FakeSession([result])
        monkeypatch.setattr(engine, 'AsyncSession', lambda **kwargs: session)
        sender.wait = AsyncMock(return_value=False)
        sender.ready = True
        await sender.start('Title', '', 0, .3)
        await sender.worker
        assert not sender.running and sender.phase == 'error'
        assert sender.errors == 1 and len(session.calls) == 1
    asyncio.run(scenario())


def test_capture_success_filters_transport_and_preserves_cookie():
    async def scenario():
        sender = Sender()
        sender.close_browser = AsyncMock()
        request = SimpleNamespace(method='POST',post_data='{"subject":"test","wall_id":123}',
            all_headers=AsyncMock(return_value={'cookie':'private','host':'padlet.com','content-length':'12'}))
        response = SimpleNamespace(request=request,status=201,url='https://padlet.com/api/9/wishes')
        await sender.capture(response, sender.generation)
        assert sender.ready and sender.headers == {'cookie':'private'}
        assert 'private' not in str(sender.status())
        assert sender.template['wall_id'] == 123
        sender.close_browser.assert_awaited_once()
    asyncio.run(scenario())
