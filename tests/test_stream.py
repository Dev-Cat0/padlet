import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from server.app import app, BrowserAction, sessions
from server.engine import Sender
from server.stream import browser_stream


def test_slow_screenshot_never_blocks_input():
    async def scenario():
        sender = Sender()
        started, release = asyncio.Event(), asyncio.Event()
        async def screenshot(**kwargs):
            started.set()
            await release.wait()
            return b'image'
        page = SimpleNamespace(is_closed=lambda:False, screenshot=screenshot,
                               keyboard=SimpleNamespace(insert_text=AsyncMock()))
        sender.page = page
        snapshot = asyncio.create_task(sender.screenshot())
        await started.wait()
        await asyncio.wait_for(sender.control(BrowserAction(kind='text',text='test')), .2)
        page.keyboard.insert_text.assert_awaited_once_with('test')
        release.set()
        await snapshot
    asyncio.run(scenario())


def test_dns_checks_coalesce_and_still_block_private_hosts(monkeypatch):
    calls = []
    def lookup(host, port):
        calls.append(host)
        return [(2,1,6,'',('127.0.0.1' if host == 'private' else '1.1.1.1',443))]
    monkeypatch.setattr('server.engine.socket.getaddrinfo',lookup)
    async def scenario():
        sender = Sender()
        assert all(await asyncio.gather(*(sender.public_host('public') for _ in range(10))))
        assert await sender.public_host('public')
        assert not await sender.public_host('private')
        assert calls.count('public') == 1
    asyncio.run(scenario())


def test_websocket_checks_origin_and_session(monkeypatch):
    import importlib
    module = importlib.import_module('server.app')
    async def fake_stream(socket, sender, model):
        await socket.send_json({'type':'test', 'ready':sender.ready})
        await socket.close()
    monkeypatch.setattr(module,'browser_stream',fake_stream)
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect('/api/browser/stream', headers={'Origin':'https://evil.example'}):
                pass
        with client.websocket_connect('/api/browser/stream', headers={'Origin':'https://dev-cat0.github.io'}) as ws:
            ws.send_json({'token':'invalid'})
            with pytest.raises(WebSocketDisconnect): ws.receive_json()
        token = client.post('/api/session',headers={'Origin':'https://dev-cat0.github.io'}).json()['token']
        sessions.items[token].sender.ready = True
        with client.websocket_connect('/api/browser/stream', headers={'Origin':'https://dev-cat0.github.io'}) as ws:
            ws.send_json({'token':token})
            assert ws.receive_json() == {'type':'test','ready':True}


def test_stream_input_does_not_wait_for_slow_frame_delivery():
    async def scenario():
        incoming, sent = asyncio.Queue(), []
        frame_started, release_frame, inputs_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
        class Socket:
            async def receive_text(self): return await incoming.get()
            async def send_json(self, data): sent.append(data)
            async def send_bytes(self, data):
                assert data == b'jpeg'
                frame_started.set()
                await release_frame.wait()
            async def close(self, **kwargs): pass
        class CDP:
            def on(self, event, handler): self.handler = handler
            def remove_listener(self, *args): pass
            async def detach(self): pass
            async def send(self, command, params=None):
                if command == 'Page.startScreencast':
                    self.handler({'data':base64.b64encode(b'jpeg').decode(),'sessionId':1})
        page = SimpleNamespace(is_closed=lambda:False,bring_to_front=AsyncMock())
        actions = []
        async def control(action):
            actions.append(action.text)
            if len(actions) == 3: inputs_done.set()
        sender = SimpleNamespace(stream_socket=None,page=page,active_page=lambda:page,
                                 context=SimpleNamespace(new_cdp_session=AsyncMock(return_value=CDP())),control=control)
        task = asyncio.create_task(browser_stream(Socket(),sender,BrowserAction))
        await asyncio.wait_for(frame_started.wait(),1)
        for i in range(3):
            await incoming.put(json.dumps({'id':i,'action':{'kind':'text','text':str(i)}}))
        await asyncio.wait_for(inputs_done.wait(),.5)
        assert actions == ['0','1','2']
        release_frame.set()
        await asyncio.sleep(.05)
        assert [item['id'] for item in sent if item['type'] == 'ack'] == [0,1,2]
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    asyncio.run(scenario())
