import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from server.app import app, sessions, RunRequest
from server.browser import BrowserHTTP
from server.engine import Sender
from test_automatic import setup, STATE, URL


def test_legacy_comment_options_cannot_start_comment_worker(monkeypatch, licensed_session):
    with TestClient(app) as client:
        token = licensed_session()
        sender = sessions.items[token].sender
        sender.queue_run = AsyncMock()
        result = client.post('/api/run', headers={'Authorization':'Bearer '+token,
            'X-Request-ID':'removed-comments-0001'}, json={'url':URL,
            'comments':{'enabled':True,'body':'legacy client','count':1000,'delay':0}})
        assert result.status_code == 200
        assert 'comments' not in sender.queue_run.await_args.kwargs
        assert 'comments' not in RunRequest.model_fields
        assert client.get('/healthz').json()['features']['anonymous_comments'] is False


def test_comments_only_permission_does_not_produce_posts(monkeypatch):
    async def scenario():
        state = dict(STATE, canIWrite=False, canIComment=True, isReadOnly=True)
        http, _ = setup(monkeypatch, state=state)
        sender = Sender()
        await sender.queue_run(URL, 'title', '', 1, 0)
        await sender.worker
        assert sender.phase == 'error'
        http.post.assert_not_awaited()
    asyncio.run(scenario())


def test_transport_has_no_comment_create_or_delete_endpoint():
    async def scenario():
        page = SimpleNamespace(url=URL, is_closed=lambda:False, evaluate=AsyncMock())
        transport = BrowserHTTP(page, 'https://padlet.com/api/9/wishes')
        for method, path in [('POST','/api/8/comments'),('DELETE','/api/5/comments/123')]:
            with pytest.raises(ValueError):
                await transport.request(method, transport.origin+path)
        page.evaluate.assert_not_awaited()
    asyncio.run(scenario())
