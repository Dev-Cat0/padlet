import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import server.engine as engine
from server.anonymous import read_sections
from server.engine import Sender


def sender_for(count=3):
    sender = Sender()
    sender.ready = True
    sender.endpoint = 'https://padlet.com/api/9/wishes'
    sender.template = {'wall_id': 22, 'wall_section_id': 999, 'wall_section_hashid': 'stale'}
    sender.board = {'sections': [{'id': i, 'hashid': f'sec_{i}', 'title': f'Section {i}'}
                                 for i in range(1, count + 1)]}
    sender.wait = AsyncMock(return_value=False)
    return sender


def reply(status=201):
    return SimpleNamespace(status_code=status, headers={'Retry-After': '12'})


def test_every_section_per_round_without_mutating_template():
    async def scenario():
        sender = sender_for()
        calls, active = [], set()
        gate = asyncio.Event()
        async def post(url, **kwargs):
            payload = kwargs['json']
            calls.append(payload)
            active.add(payload['wall_section_id'])
            if len(active) == 3:
                gate.set()
            await asyncio.wait_for(gate.wait(), 1)
            return reply()
        sender.http = SimpleNamespace(post=post)
        await sender.start('Title', 'Body', 2, .3)
        await sender.worker
        assert sender.sent == 6 and sender.rounds == 2 and sender.target_count == 3
        assert sender.phase == 'finished'
        assert [call['wall_section_id'] for call in calls] == [1, 2, 3, 1, 2, 3]
        assert all(call['wall_section_hashid'] == f"sec_{call['wall_section_id']}" for call in calls)
        assert len({call['cid'] for call in calls}) == 6
        assert sender.template['wall_section_id'] == 999
    asyncio.run(scenario())


def test_limited_section_retries_without_reposting_successes():
    async def scenario():
        sender = sender_for()
        calls = []
        async def post(url, **kwargs):
            section_id = kwargs['json']['wall_section_id']
            calls.append(section_id)
            await asyncio.sleep(0)
            return reply(429 if section_id == 2 and calls.count(2) == 1 else 201)
        sender.http = SimpleNamespace(post=post)
        await sender.start('Title', '', 1, .3)
        await sender.worker
        assert calls == [1, 2, 3, 2]
        assert sender.sent == 3 and sender.errors == 1 and sender.rounds == 1
        assert sender.wait.call_args_list[-1].args[0] == 12
    asyncio.run(scenario())


def test_stop_waits_for_inflight_and_skips_queued_sections():
    async def scenario():
        sender = sender_for(12)
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def post(url, **kwargs):
            calls.append(kwargs['json']['wall_section_id'])
            if len(calls) == 8:
                entered.set()
            await release.wait()
            return reply()
        sender.http = SimpleNamespace(post=post)
        await sender.start('Title', '', 0, .3)
        await asyncio.wait_for(entered.wait(), 1)
        sender.stop()
        release.set()
        await sender.worker
        assert len(calls) == 8 and sender.sent == 8
        assert sender.rounds == 0 and sender.phase == 'stopped'
    asyncio.run(scenario())


def test_failed_post_is_not_retried_and_rounds_do_not_continue():
    async def scenario():
        sender = sender_for()
        calls = []
        async def post(url, **kwargs):
            section_id = kwargs['json']['wall_section_id']
            calls.append(section_id)
            await asyncio.sleep(0)
            if section_id == 2:
                raise TimeoutError()
            return reply()
        sender.http = SimpleNamespace(post=post)
        await sender.start('Title', '', 4, .3)
        await sender.worker
        assert calls == [1, 2, 3]
        assert sender.sent == 2 and sender.errors == 1
        assert sender.rounds == 0 and sender.phase == 'error'
    asyncio.run(scenario())


def test_interval_counts_response_time_without_jitter_or_overlap(monkeypatch):
    async def scenario():
        now = [0.0]
        monkeypatch.setattr(engine, 'time', SimpleNamespace(monotonic=lambda: now[0]))
        sender = sender_for(1)
        starts = []
        async def wait(seconds):
            now[0] += seconds
            return False
        async def post(url, **kwargs):
            starts.append(now[0])
            now[0] += .1
            return reply()
        sender.wait = wait
        sender.http = SimpleNamespace(post=post)
        await sender.start('Title', '', 3, .3)
        await sender.worker
        assert starts == pytest.approx([.3, .6, .9])
        assert sender.rounds == 3
    asyncio.run(scenario())


def test_board_without_sections_still_sends_once_per_round():
    async def scenario():
        sender = sender_for(0)
        sender.template = {'wall_id': 55}
        sender.http = SimpleNamespace(post=AsyncMock(return_value=reply()))
        await sender.start('Title', '', 2, 0)
        await sender.worker
        assert sender.sent == sender.rounds == 2 and sender.target_count == 1
        assert all('wall_section_id' not in call.kwargs['json'] for call in sender.http.post.call_args_list)
    asyncio.run(scenario())


@pytest.mark.parametrize('wall_id,section_ids', [(22, [11, 12]), (88, [91, 92, 93]), (99, [])])
def test_section_discovery_uses_each_boards_id(wall_id, section_ids):
    async def scenario():
        session = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(
            status_code=200, headers={}, content=b'{}', json=lambda: {
                'data': [{'attributes': {'id': i, 'wall_id': wall_id, 'title': f'Section {i}'}}
                         for i in section_ids]})))
        sections = await read_sections(session, 'https://padlet.com', {}, wall_id)
        assert [section['id'] for section in sections] == section_ids
        assert session.get.call_args.kwargs['params'] == {'wall_id': wall_id}
    asyncio.run(scenario())


@pytest.mark.parametrize('data', [None, {}, {'data': {}}, {'data': [{'attributes': {'id': -1}}]},
                                 {'data': [{'attributes': {'id': 12, 'wall_id': 999}}]}])
def test_invalid_section_list_never_silently_uses_only_default(data):
    async def scenario():
        session = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(
            status_code=200, headers={}, content=b'{}', json=lambda: data)))
        with pytest.raises(ValueError):
            await read_sections(session, 'https://padlet.com', {}, 22)
    asyncio.run(scenario())
