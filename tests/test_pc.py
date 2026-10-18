import asyncio
import importlib.util
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from server.app import app, sessions


@pytest.fixture
def client():
    with TestClient(app) as client:
        yield client


def panel(client):
    token = client.post('/api/session', headers={'Origin':'https://dev-cat0.github.io'}).json()['token']
    return {'Authorization':'Bearer ' + token}, sessions.items[token].sender


def pair(client, auth):
    code = client.post('/api/pc/pair', headers=auth, json={}).json()['code']
    token = client.post('/api/pc/connect', json={'code':code}).json()['token']
    return {'Authorization':'Bearer ' + token}


def open_browser(client, auth, helper):
    result = client.post('/api/pc/open', headers=auth, json={'url':'https://padlet.com/dashboard'})
    assert result.status_code == 200
    return client.get('/api/pc/command', headers=helper).json()['command']


def payload(command):
    return {'command_id':command['id'], 'endpoint':'https://padlet.com/api/9/wishes', 'status':201,
            'headers':{'Cookie':'private-cookie', 'Content-Length':'20', 'Host':'padlet.com'},
            'template':{'wall_id':123, 'subject':'sample', 'body':'sample'}}


def test_pairing_is_expiring_single_use_and_separate_from_panel_token(client):
    auth, sender = panel(client)
    code = client.post('/api/pc/pair', headers=auth, json={}).json()['code']
    sender.pc.expires -= 301
    assert client.post('/api/pc/connect', json={'code':code}).status_code == 401
    helper = pair(client, auth)
    assert client.post('/api/pc/connect', json={'code':code}).status_code == 401
    assert client.get('/api/pc/command', headers=auth).status_code == 401
    assert client.get('/api/state', headers=helper).status_code == 401
    assert client.get('/api/state', headers=auth).json()['pc_connected']
    assert 'token' not in client.get('/api/state', headers=auth).json()


def test_pc_capture_is_isolated_validated_and_idempotent(client):
    auth, sender = panel(client)
    other, other_sender = panel(client)
    helper, other_helper = pair(client, auth), pair(client, other)
    command = open_browser(client, auth, helper)
    assert client.get('/api/pc/command', headers=other_helper).json()['command'] is None
    data = payload(command)
    assert client.post('/api/pc/capture', headers=other_helper, json=data).status_code == 409
    assert client.post('/api/pc/capture', headers=helper, json={**data,'endpoint':'https://evil.example/api/9/wishes'}).status_code == 422
    assert client.post('/api/pc/capture', headers=helper, json={**data,'status':403}).status_code == 422
    assert client.post('/api/pc/capture', headers=helper, json=data).status_code == 200
    assert client.post('/api/pc/capture', headers=helper, json=data).status_code == 200
    assert sender.ready and sender.headers == {'cookie':'private-cookie'}
    assert not other_sender.ready
    assert client.get('/api/pc/command', headers=helper).json()['command']['kind'] == 'close'
    state = client.get('/api/state', headers=auth)
    assert 'private-cookie' not in state.text and 'wall_id' not in state.text
    client.post('/api/pc/disconnect', headers=helper, json={})
    assert sender.ready  # PC can close after capture; Render still sends.


def test_reset_cancels_pending_pc_open_and_rejects_late_capture(client):
    auth, sender = panel(client)
    helper = pair(client, auth)
    command = open_browser(client, auth, helper)
    assert sender.opening
    assert client.post('/api/reset', headers=auth, json={}).status_code == 200
    assert not sender.opening and not sender.pc.active
    assert client.post('/api/pc/capture', headers=helper, json=payload(command)).status_code == 409
    assert client.get('/api/pc/command', headers=helper).json()['command']['kind'] == 'close'


def test_disconnected_pc_cannot_open_or_keep_capture_pending(client):
    auth, sender = panel(client)
    assert client.post('/api/pc/open', headers=auth, json={'url':'https://padlet.com/dashboard'}).status_code == 409
    helper = pair(client, auth)
    open_browser(client, auth, helper)
    sender.pc.last_seen -= 31
    asyncio.run(sessions.collect())
    assert not sender.opening and not sender.pc.active
    assert sender.phase == 'error'


def test_helper_captures_successful_posts_only_without_extra_posting():
    spec = importlib.util.spec_from_file_location('pc_helper', Path(__file__).parents[1] / 'pc-helper/helper.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    async def scenario():
        helper = module.Helper('code', lambda _:None, threading.Event())
        helper.command_id = 'a' * 32
        helper.request = AsyncMock(return_value={'ok':True})
        response = SimpleNamespace(url='https://padlet.com/api/9/wishes', status=403,
            request=SimpleNamespace(method='POST', post_data=json.dumps({'wall_id':123}),
                                    all_headers=AsyncMock(return_value={'cookie':'secret'})))
        await helper.capture(response, helper.command_id)
        helper.request.assert_not_awaited()
        response.status = 201
        await helper.capture(response, helper.command_id)
        helper.request.assert_awaited_once()
        path, data = helper.request.call_args.args
        assert path == '/api/pc/capture' and data['headers']['cookie'] == 'secret'
        assert data['template']['wall_id'] == 123
    asyncio.run(scenario())
