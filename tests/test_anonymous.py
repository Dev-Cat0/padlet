import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.anonymous import build_template, prepare, check_response
from server.engine import Sender
import server.engine as engine


URL = 'https://padlet.com/example/test-board'
STATE = {'canIWrite': True, 'user': {'id': 11, 'hashid': 'anonymous', 'registered_at': None},
         'wall': {'id': 22, 'hashid': 'board_example', 'title': 'Test board',
                  'require_approval': False, 'wish_arrangement': {'default_section_id': 33}}}


def response(status=200, text='', data=None, headers=None):
    return SimpleNamespace(status_code=status, text=text, content=text.encode(),
                           json=lambda: data, headers=headers or {})


def client(state=None):
    return SimpleNamespace(get=AsyncMock(side_effect=[
        response(text='<meta content="csrf-secret" name="csrf-token">'),
        response(data=deepcopy(STATE) if state is None else state)]),
        post=AsyncMock(), close=AsyncMock())


def test_prepare_uses_get_only_and_preserves_guest_identity():
    async def scenario():
        session = client()
        endpoint, headers, template, board = await prepare(session, URL)
        assert endpoint == 'https://padlet.com/api/9/wishes'
        assert headers['X-CSRF-Token'] == 'csrf-secret'
        assert template['author_id'] == 11 and template['wall_section_id'] == 33
        assert template['wall_id'] == 22 and template['published']
        assert board['url'] == URL
        assert session.get.await_count == 2
        assert session.get.call_args.args[0] == 'https://padlet.com/api/11/padlet-starting-state/example/test-board'
        assert all(call.kwargs['allow_redirects'] is False for call in session.get.call_args_list)
        session.post.assert_not_awaited()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', [
    {'canIWrite': False}, {'canIWrite': 1}, {'isReadOnly': True},
    {'wall': {**STATE['wall'], 'frozen': True}},
    {'wall': {**STATE['wall'], 'is_archived': True}},
    {'user': {**STATE['user'], 'registered_at': '2026-01-01'}},
    {'user': {}},
])
def test_prepare_rejects_non_anonymous_or_read_only_state(change):
    with pytest.raises(ValueError):
        build_template({**deepcopy(STATE), **change})


def test_moderated_board_does_not_publish_directly():
    state = deepcopy(STATE)
    state['wall']['require_approval'] = True
    assert build_template(state)['published'] is False


def test_security_challenge_is_reported_without_retry():
    with pytest.raises(ValueError, match='보안 확인'):
        check_response(response(403, headers={'cf-mitigated': 'challenge'}), '게시판 페이지 조회')


@pytest.mark.parametrize('location', ['https://evil.example/board', 'http://padlet.com/a/b',
                                      'https://padlet.com/api/9/wishes', 'https://padlet.com/a/%2e%2e'])
def test_redirect_never_leaves_allowed_board_routes(location):
    async def scenario():
        session = client()
        session.get.side_effect = [response(302, headers={'location': location})]
        with pytest.raises(ValueError):
            await prepare(session, URL)
        assert session.get.await_count == 1
        session.post.assert_not_awaited()
    asyncio.run(scenario())


@pytest.mark.parametrize('first', [response(403), response(text='<html>Login required</html>')])
def test_challenge_or_missing_csrf_stops(first):
    async def scenario():
        session = client()
        session.get.side_effect = [first]
        with pytest.raises(ValueError):
            await prepare(session, URL)
        assert session.get.await_count == 1
        session.post.assert_not_awaited()
    asyncio.run(scenario())


def test_sessions_use_separate_http_clients_and_hide_tokens(monkeypatch):
    async def scenario():
        first, second = Sender(), Sender()
        http1, http2 = client(), client()
        queue = iter([http1, http2])
        monkeypatch.setattr(engine, 'AsyncSession', lambda **kwargs: next(queue))
        await first.connect(URL)
        await second.connect(URL)
        assert first.http is http1 and second.http is http2
        assert first.ready and second.ready
        assert 'csrf-secret' not in str(first.status())
        assert 'anonymous' not in str(first.status())
        await first.reset()
        http1.close.assert_awaited_once()
        assert not first.ready and first.template == {} and first.headers == {}
        assert second.ready
        await second.shutdown()
    asyncio.run(scenario())


def test_failed_reconnect_clears_previous_board(monkeypatch):
    async def scenario():
        sender = Sender()
        good, bad = client(), client({**STATE, 'canIWrite': False})
        queue = iter([good, bad])
        monkeypatch.setattr(engine, 'AsyncSession', lambda **kwargs: next(queue))
        await sender.connect(URL)
        with pytest.raises(ValueError):
            await sender.connect(URL)
        assert not sender.ready and not sender.opening and sender.http is None
        assert sender.board is None and not sender.headers
        good.close.assert_awaited_once()
        bad.close.assert_awaited_once()
    asyncio.run(scenario())


def test_connect_blocks_start_reset_and_second_connect(monkeypatch):
    async def scenario():
        sender = Sender()
        gate = asyncio.Event()
        http = client()
        async def pending(*args):
            await gate.wait()
            return 'https://padlet.com/api/9/wishes', {}, {}, {'url': URL, 'approval_required': False}
        monkeypatch.setattr(engine, 'AsyncSession', lambda **kwargs: http)
        monkeypatch.setattr(engine, 'prepare', pending)
        task = asyncio.create_task(sender.connect(URL))
        await asyncio.sleep(0)
        for action in (lambda: sender.start('x', '', 1, 0), sender.reset, lambda: sender.connect(URL)):
            with pytest.raises(ValueError):
                await action()
        gate.set()
        await task
        await sender.shutdown()
    asyncio.run(scenario())
