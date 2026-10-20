import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from server.app import app, sessions
from server.engine import Sender
from server.browser import BrowserResponse

URL = "https://padlet.com/example/board"
STATE = {"canIWrite": True, "user": {"id": 10, "hashid": "guest", "registered_at": None},
         "wall": {"id": 22, "hashid": "board", "title": "Fixture"}}


def setup(monkeypatch, state=None, fields=None, hold=None):
    browsers = []
    class Browser:
        def __init__(self, sender):
            self.capture_enabled = True
            self.closed = False
            browsers.append(self)
        async def open(self, url):
            if hold:
                await hold.wait()
        def active_page(self):
            return SimpleNamespace(url=URL)
        async def close(self):
            self.closed = True
    http = SimpleNamespace(origin="https://padlet.com", close=AsyncMock(),
        get=AsyncMock(return_value=SimpleNamespace(status_code=200, headers={}, content=b"{}",
            json=lambda: copy.deepcopy(state or STATE))),
        post=AsyncMock(return_value=BrowserResponse({"status":201,"headers":{},"text":'{"data":{"id":1}}'})))
    monkeypatch.setattr("server.engine.BrowserSession", Browser)
    monkeypatch.setattr("server.engine.BrowserHTTP", lambda *args: http)
    monkeypatch.setattr("server.engine.read_sections", AsyncMock(return_value=[
        {"id": 1, "hashid": "sec1", "title": "First"},
        {"id": 2, "hashid": "sec2", "title": "Second"}]))
    monkeypatch.setattr("server.engine.read_fields", AsyncMock(return_value=fields or {}))
    return http, browsers


def test_one_click_prepares_anonymous_and_sends_every_section_each_round(monkeypatch):
    async def scenario():
        http, browsers = setup(monkeypatch)
        sender = Sender()
        await sender.queue_run(URL, "Title", "Body", 3, 0)
        await sender.worker
        assert sender.sent == 6 and sender.rounds == 3 and sender.phase == "finished"
        assert sender.target_count == 2
        assert http.post.await_count == 6  # Three rounds across both sections, no sample POST.
        assert [call.kwargs["json"]["wall_section_id"] for call in http.post.call_args_list] == [1, 2] * 3
        for call in http.post.call_args_list:
            assert call.kwargs["json"]["author_id"] == 10
            assert call.kwargs["json"]["wall_section_hashid"] == "sec" + str(call.kwargs["json"]["wall_section_id"])
            assert call.kwargs["json"]["subject"] == "Title"
        assert not browsers[0].capture_enabled and browsers[0].closed
        assert sender.browser is None and sender.http is None and not sender.opening
        assert len(sender.board["sections"]) == 2
        await sender.queue_run(URL, "Next", "Body", 1, 0)
        await sender.worker
        assert len(browsers) == 2 and browsers[0] is not browsers[1]
    asyncio.run(scenario())


def test_automatic_starts_all_sections_together_every_point_three_seconds(monkeypatch):
    async def scenario():
        http, _ = setup(monkeypatch)
        sections = [{"id": i, "hashid": f"s{i}", "title": str(i)} for i in range(12)]
        monkeypatch.setattr("server.engine.read_sections", AsyncMock(return_value=sections))
        now, calls, gates = [0.0], [], [asyncio.Event() for _ in range(3)]
        monkeypatch.setattr("server.engine.time", SimpleNamespace(monotonic=lambda: now[0]))
        sender = Sender()
        async def wait(seconds):
            now[0] += seconds
            return False
        async def post(url, **kwargs):
            round_index = len(calls) // len(sections)
            calls.append((now[0], kwargs["json"]["wall_section_id"]))
            if len(calls) % len(sections) == 0:
                now[0] += .1  # Response time is part of the interval.
                gates[round_index].set()
            await asyncio.wait_for(gates[round_index].wait(), 1)
            return BrowserResponse({"status": 201, "headers": {}, "text": '{"data":{"id":1}}'})
        sender.wait, http.post = wait, post
        await sender.queue_run(URL, "Title", "Body", 3, .3)
        await sender.worker
        assert sender.phase == "finished" and sender.sent == 36
        for i, expected in enumerate([.3, .6, .9]):
            batch = calls[i * 12:(i + 1) * 12]
            assert [timestamp for timestamp, _ in batch] == pytest.approx([expected] * 12)
            assert {section for _, section in batch} == set(range(12))
    asyncio.run(scenario())


@pytest.mark.parametrize("user", [
    {"id":10,"hashid":"user","registered_at":"2026-01-01"},
    {"id":10,"hashid":"user"},
    {"id":10,"hashid":"user","registered_at":None,"username":"account"},
])
def test_automatic_bot_refuses_non_anonymous_identity(monkeypatch, user):
    async def scenario():
        http, browsers = setup(monkeypatch, {**STATE, "user": user})
        sender = Sender()
        await sender.queue_run(URL, "Title", "", 1, 0)
        await sender.worker
        assert sender.phase == "error" and not sender.running
        http.post.assert_not_awaited()
        assert browsers[0].closed
    asyncio.run(scenario())


def test_required_extra_fields_fail_before_post(monkeypatch):
    async def scenario():
        http, _ = setup(monkeypatch, fields={"required":{"attachment":True}})
        sender = Sender()
        await sender.queue_run(URL, "Title", "Body", 1, 0)
        await sender.worker
        http.post.assert_not_awaited()
        assert sender.phase == "error"
        assert "첨부" in sender.logs[-1]["message"]
    asyncio.run(scenario())


def test_stop_during_preparation_closes_without_post(monkeypatch):
    async def scenario():
        http, browsers = setup(monkeypatch, hold=asyncio.Event())
        sender = Sender()
        await sender.queue_run(URL, "Title", "", 1, 0)
        assert sender.opening
        sender.stop()
        sender.stop()  # Repeated stop must not interrupt resource cleanup.
        await sender.worker
        assert sender.phase == "stopped" and not sender.opening
        assert browsers[0].closed
        http.post.assert_not_awaited()
    asyncio.run(scenario())


def test_duplicate_job_and_other_session_cannot_start_parallel_browser(monkeypatch):
    async def scenario():
        from server.sessions import Sessions
        from fastapi import HTTPException
        setup(monkeypatch, hold=asyncio.Event())
        registry = Sessions()
        a, b = registry.create(), registry.create()
        first, second = registry.items[a].sender, registry.items[b].sender
        data = SimpleNamespace(url=URL, subject="Title", body="", limit=1, delay=0)
        await registry.run(first, data)
        with pytest.raises(ValueError):
            await registry.run(first, data)
        with pytest.raises(HTTPException):
            await registry.run(second, data)
        await registry.shutdown()
    asyncio.run(scenario())


def test_run_api_only_needs_five_fields_and_does_not_take_user_auth(monkeypatch):
    with TestClient(app) as client:
        assert client.post("/api/run",json={}).status_code == 401
        token = client.post("/api/session",headers={"Origin":"https://dev-cat0.github.io"},json={}).json()["token"]
        sender = sessions.items[token].sender
        sender.queue_run = AsyncMock()
        data = {"url":URL,"subject":"Title","body":"Body","limit":3,"delay":.5}
        result = client.post("/api/run",headers={"Authorization":"Bearer "+token},json=data)
        assert result.status_code == 200
        sender.queue_run.assert_awaited_once_with(URL,"Title","Body",3,.5)
        assert client.post("/api/run",headers={"Authorization":"Bearer "+token},json={**data,"limit":-1}).status_code == 422
