"""Bounded short-interval sending with delayed responses; no external requests."""
import asyncio
from collections import Counter
from types import SimpleNamespace

import pytest

from server.engine import Sender


def sender_for(sections=3):
    sender = Sender()
    sender.ready = True
    sender.endpoint = "https://padlet.com/api/9/wishes"
    sender.template = {"wall_id": 22}
    sender.board = {"sections": [{"id": i, "title": str(i)} for i in range(sections)]}
    return sender


def reply(status=201, retry=0):
    return SimpleNamespace(status_code=status, headers={"Retry-After": str(retry)})


@pytest.mark.parametrize("delay", [0, .00001])
def test_refills_before_slow_first_round_finishes_and_keeps_exact_count(delay):
    async def scenario():
        sender = sender_for()
        calls, active, peak = [], 0, 0
        full, slow, release, refilled = (asyncio.Event() for _ in range(4))
        async def post(url, **kwargs):
            nonlocal active, peak
            index = len(calls)
            calls.append(kwargs["json"])
            active += 1
            peak = max(peak, active)
            if len(calls) == 12:
                full.set()
            if len(calls) == 15:
                refilled.set()
            await (slow if index < 3 else release).wait()
            active -= 1
            return reply()
        sender.http = SimpleNamespace(post=post)
        await sender.start("Title", "Body", 7, delay)
        try:
            await asyncio.wait_for(full.wait(), 1)
            await asyncio.sleep(.02)
            assert len(calls) == 12  # No unbounded task queue while responses are pending.
            release.set()
            await asyncio.wait_for(refilled.wait(), 1)
            assert not slow.is_set()  # Refill does not wait for the oldest round.
            slow.set()
            await asyncio.wait_for(sender.worker, 1)
            assert sender.phase == "finished" and sender.rounds == 7 and sender.sent == 21
            assert Counter(p["wall_section_id"] for p in calls) == {0: 7, 1: 7, 2: 7}
            assert len({p["cid"] for p in calls}) == 21
            assert peak == 12
        finally:
            sender.stop()
            slow.set()
            release.set()
            await sender.worker
    asyncio.run(scenario())


def test_stop_drains_started_posts_without_launching_next_round():
    async def scenario():
        sender = sender_for(2)
        full, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def post(url, **kwargs):
            calls.append(kwargs["json"])
            if len(calls) == 8:
                full.set()
            await release.wait()
            return reply()
        sender.http = SimpleNamespace(post=post)
        await sender.start("Title", "", 0, 0)
        await asyncio.wait_for(full.wait(), 1)
        sender.stop()
        release.set()
        await asyncio.wait_for(sender.worker, 1)
        assert len(calls) == sender.sent == 8
        assert sender.rounds == 4 and sender.phase == "stopped"
    asyncio.run(scenario())


def test_429_pauses_all_rounds_and_only_retries_rejected_post():
    async def scenario():
        sender = sender_for(2)
        calls, waits = [], []
        full, release, cooling, resume = (asyncio.Event() for _ in range(4))
        async def post(url, **kwargs):
            index = len(calls)
            calls.append(kwargs["json"]["wall_section_id"])
            if len(calls) == 8:
                full.set()
            await release.wait()
            return reply(429, 12) if index == 0 else reply()
        async def wait(seconds):
            if seconds >= 3:
                waits.append(seconds)
                cooling.set()
                await resume.wait()
                return False
            return await Sender.wait(sender, seconds)
        sender.wait = wait
        sender.http = SimpleNamespace(post=post)
        await sender.start("Title", "", 6, .00001)
        await asyncio.wait_for(full.wait(), 1)
        release.set()
        await asyncio.wait_for(cooling.wait(), 1)
        assert sender.phase == "backoff" and sender.sent == 7
        await asyncio.sleep(.02)
        assert len(calls) == 8
        resume.set()
        await asyncio.wait_for(sender.worker, 1)
        assert waits == [12]
        assert Counter(calls) == {0: 7, 1: 6}
        assert sender.sent == 12 and sender.rounds == 6 and sender.phase == "finished"
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["403", "network"])
def test_error_stops_all_new_rounds_without_retrying_uncertain_post(failure):
    async def scenario():
        sender = sender_for(2)
        calls, full, release = [], asyncio.Event(), asyncio.Event()
        async def post(url, **kwargs):
            index = len(calls)
            calls.append(kwargs["json"])
            if len(calls) == 8:
                full.set()
            await release.wait()
            if index == 0:
                if failure == "network":
                    raise TimeoutError()
                return reply(403)
            return reply()
        sender.http = SimpleNamespace(post=post)
        await sender.start("Title", "", 100, 0)
        await asyncio.wait_for(full.wait(), 1)
        release.set()
        await asyncio.wait_for(sender.worker, 1)
        assert sender.phase == "error" and len(calls) == 8 and sender.sent == 7
        assert sender.rounds == 3 and sender.errors == 1
    asyncio.run(scenario())


def test_large_section_count_bounds_overlapping_rounds():
    async def scenario():
        sender = sender_for(20)
        calls, full, release = [], asyncio.Event(), asyncio.Event()
        async def post(url, **kwargs):
            calls.append(kwargs["json"])
            if len(calls) == 20:
                full.set()
            await release.wait()
            return reply()
        sender.http = SimpleNamespace(post=post)
        await sender.start("Title", "", 2, 0)
        await asyncio.wait_for(full.wait(), 1)
        await asyncio.sleep(.02)
        assert len(calls) == 20
        release.set()
        await asyncio.wait_for(sender.worker, 1)
        assert sender.sent == 40 and sender.rounds == 2
    asyncio.run(scenario())
