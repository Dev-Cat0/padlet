import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from server.app import app
import server.engine as engine
from server.engine import Sender, is_capture_url, make_payload, padlet_url


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("PANEL_TOKEN", "x" * 40)
    with TestClient(app) as client:
        yield client


def test_private_endpoints_fail_closed(client, monkeypatch):
    assert client.get('/healthz').status_code == 200
    for route in ['/api/state', '/api/browser/frame', '/api/browser/tabs']:
        assert client.get(route).status_code == 401
    for route in ['/api/start', '/api/stop', '/api/reset', '/api/browser/open', '/api/browser/action']:
        assert client.post(route, json={}).status_code == 401
    monkeypatch.delenv('PANEL_TOKEN')
    assert client.get('/api/state').status_code == 503


def test_state_hides_secrets_and_cors(client):
    response = client.get('/api/state', headers={'Authorization':'Bearer ' + 'x'*40,'Origin':'https://dev-cat0.github.io'})
    assert response.status_code == 200
    assert response.headers['access-control-allow-origin'] == 'https://dev-cat0.github.io'
    assert response.headers['cache-control'] == 'no-store'
    assert not {'headers','template','endpoint'} & response.json().keys()
    response = client.get('/api/state', headers={'Authorization':'Bearer ' + 'x'*40,'Origin':'https://evil.example'})
    assert 'access-control-allow-origin' not in response.headers


def test_start_validates_input_and_capture(client):
    auth = {'Authorization':'Bearer ' + 'x'*40}
    assert client.post('/api/start', headers=auth, json={'subject':'ok'}).status_code == 409
    for values in [{'subject':' '}, {'subject':'x','delay':-1}, {'subject':'x','limit':-1}, {'subject':'x','limit':1.5}]:
        assert client.post('/api/start', headers=auth, json=values).status_code == 422


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
