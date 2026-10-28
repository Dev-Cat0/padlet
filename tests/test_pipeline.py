"""Bounded sending with delayed responses; no external requests."""
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
        budget = 32 if delay == 0 else 30
        calls, active, peak = [], 0, 0
        full, slow, release, refilled = (asyncio.Event() for _ in range(4))
        async def post(url, **kwargs):
            nonlocal active, peak
            index = len(calls)
            calls.append(kwargs["json"])
            active += 1
            peak = max(peak, active)
            if len(calls) == budget:
                full.set()
            if len(calls) == 33:
                refilled.set()
            await (slow if index < 3 else release).wait()
            active -= 1
            return reply()
        sender.http = SimpleNamespace(post=post)
        await sender.start("Title", "Body", 13, delay)
        try:
            await asyncio.wait_for(full.wait(), 1)
            await asyncio.sleep(.02)
            assert len(calls) == budget  # No unbounded task queue while responses are pending.
            release.set()
            await asyncio.wait_for(refilled.wait(), 1)
            assert not slow.is_set()  # Refill does not wait for the oldest round.
            slow.set()
            await asyncio.wait_for(sender.worker, 1)
            assert sender.phase == "finished" and sender.rounds == 13 and sender.sent == 39
            assert Counter(p["wall_section_id"] for p in calls) == {0: 13, 1: 13, 2: 13}
            assert len({p["cid"] for p in calls}) == 39
            assert peak == budget
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
            if len(calls) == 32:
                full.set()
            await release.wait()
            return reply()
        sender.http = SimpleNamespace(post=post)
        await sender.start("Title", "", 0, 0)
        await asyncio.wait_for(full.wait(), 1)
        sender.stop()
        release.set()
        await asyncio.wait_for(sender.worker, 1)
        assert len(calls) == sender.sent == 32
        assert sender.rounds == 16 and sender.phase == "stopped"
    asyncio.run(scenario())


def test_429_pauses_all_rounds_and_only_retries_rejected_post():
    async def scenario():
        sender = sender_for(2)
        calls, waits = [], []
        full, release, cooling, resume = (asyncio.Event() for _ in range(4))
        async def post(url, **kwargs):
            index = len(calls)
            calls.append(kwargs["json"]["wall_section_id"])
            if len(calls) == 32:
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
        await sender.start("Title", "", 18, .00001)
        await asyncio.wait_for(full.wait(), 1)
        release.set()
        await asyncio.wait_for(cooling.wait(), 1)
        assert sender.phase == "backoff" and sender.sent == 31
        await asyncio.sleep(.02)
        assert len(calls) == 32
        resume.set()
        await asyncio.wait_for(sender.worker, 1)
        assert waits == [12]
        assert Counter(calls) == {0: 19, 1: 18}
        assert sender.sent == 36 and sender.rounds == 18 and sender.phase == "finished"
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["403", "network"])
def test_error_stops_all_new_rounds_without_retrying_uncertain_post(failure):
    async def scenario():
        sender = sender_for(2)
        calls, full, release = [], asyncio.Event(), asyncio.Event()
        async def post(url, **kwargs):
            index = len(calls)
            calls.append(kwargs["json"])
            if len(calls) == 32:
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
        assert sender.phase == "error" and len(calls) == 32 and sender.sent == 31
        assert sender.rounds == 15 and sender.errors == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("delay", [.05, .3])
def test_normal_interval_launches_next_round_while_response_is_pending(delay):
    async def scenario():
        sender = sender_for(1)
        release, second = asyncio.Event(), asyncio.Event()
        starts = []
        async def post(url, **kwargs):
            starts.append(asyncio.get_running_loop().time())
            if len(starts) == 2:
                second.set()
            await release.wait()
            return reply()
        sender.http = SimpleNamespace(post=post)
        await sender.start("Title", "", 2, delay)
        try:
            await asyncio.wait_for(second.wait(), 2)
            assert sender.sent == 0
            assert starts[1] - starts[0] >= delay * .9
        finally:
            release.set()
            await sender.worker
        assert sender.sent == sender.rounds == 2
        assert sender.phase == "finished"
    asyncio.run(scenario())


def test_more_than_32_sections_still_caps_actual_requests_and_stops_queued_posts():
    async def scenario():
        sender = sender_for(40)
        full, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def post(url, **kwargs):
            calls.append(kwargs["json"])
            if len(calls) == 32:
                full.set()
            await release.wait()
            return reply()
        sender.http = SimpleNamespace(post=post)
        await sender.start("Title", "", 2, 0)
        try:
            await asyncio.wait_for(full.wait(), 1)
            await asyncio.sleep(.02)
            assert len(calls) == 32
        finally:
            sender.stop()
            release.set()
            await sender.worker
        assert len(calls) == sender.sent == 32
        assert sender.rounds == 0 and sender.phase == "stopped"
    asyncio.run(scenario())


def test_error_during_long_interval_is_processed_immediately():
    async def scenario():
        sender = sender_for(1)
        waits = 0
        async def wait(seconds):
            nonlocal waits
            waits += 1
            if waits == 1:
                return False  # Skip the initial hour before the first request.
            return await Sender.wait(sender, seconds)
        async def post(url, **kwargs):
            await asyncio.sleep(.01)
            return reply(403)
        sender.wait = wait
        sender.http = SimpleNamespace(post=post)
        await sender.start("Title", "", 2, 3600)
        await asyncio.wait_for(sender.worker, 1)
        assert sender.phase == "error" and sender.errors == 1
        assert waits >= 2 and sender.sent == 0
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
        assert len(calls) == 32
        release.set()
        await asyncio.wait_for(sender.worker, 1)
        assert sender.sent == 40 and sender.rounds == 2
    asyncio.run(scenario())
