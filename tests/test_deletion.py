import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from server.app import app, sessions
from server.browser import BrowserHTTP, BrowserResponse, deletion_hashid
from server.engine import Sender
from server.sessions import Sessions

URL = "https://padlet.com/example/board"
ENDPOINT = "https://padlet.com/api/9/wishes"


def response(status=204, data=None, headers=None):
    return BrowserResponse({"status": status, "headers": headers or {}, "text": json.dumps(data or {})})


def fixture(monkeypatch):
    restored, deletions, browsers = [], [], []
    http = SimpleNamespace(origin="https://padlet.com", close=AsyncMock())
    http.get = AsyncMock(return_value=response(200, {
        "canIWrite": True, "wall": {"id": 22, "hashid": "board"},
        "user": {"id": 31, "hashid": "guest", "registered_at": None}}))
    counter = 0

    async def post(*args, **kwargs):
        nonlocal counter
        counter += 1
        return response(201, {"data": {"id": counter, "attributes": {"hashid": f"post{counter}"}}})

    async def delete(url, **kwargs):
        deletions.append((restored[-1], url))
        return response()

    http.post, http.delete = AsyncMock(side_effect=post), AsyncMock(side_effect=delete)

    class Browser:
        def __init__(self, sender):
            self.identity = len(browsers) + 1
            self.closed = False
            browsers.append(self)

        async def open(self, url):
            pass

        async def sending_page(self):
            return SimpleNamespace(url=URL)

        async def snapshot(self):
            return {"identity": self.identity, "secret": "private"}

        async def restore(self, snapshot):
            self.identity = snapshot["identity"]
            restored.append(snapshot["identity"])
            return SimpleNamespace(url=URL)

        async def close(self):
            self.closed = True

    monkeypatch.setattr("server.engine.BrowserSession", Browser)
    monkeypatch.setattr("server.engine.BrowserHTTP", lambda *args: http)
    monkeypatch.setattr("server.engine.read_sections", AsyncMock(return_value=[]))
    monkeypatch.setattr("server.engine.read_fields", AsyncMock(return_value={}))
    return http, restored, deletions, browsers


def test_all_runs_keep_separate_identities_and_delete_only_their_returned_hashids(monkeypatch):
    async def scenario():
        http, restored, deletions, browsers = fixture(monkeypatch)
        sender = Sender()
        for _ in range(2):
            await sender.queue_run(URL, "Title", "Body", 2, 0)
            await sender.worker
        assert sender.status()["delete_pending"] == 4
        assert all(b.closed for b in browsers)
        assert "private" not in json.dumps(sender.status())
        await sender.queue_delete()
        task = sender.delete_worker
        await sender.queue_delete()
        assert sender.delete_worker is task
        with pytest.raises(ValueError):
            await sender.queue_run(URL, "Title", "", 1, 0)
        await task
        assert restored == [1, 2]
        assert set(deletions) == {(1, ENDPOINT + "/post1"), (1, ENDPOINT + "/post2"),
                                  (2, ENDPOINT + "/post3"), (2, ENDPOINT + "/post4")}
        assert sender.deleted == 4 and sender.phase == "deleted"
        assert not sender.deletion_batches and sender.browser is None
        await sender.queue_delete()
        await sender.delete_worker
        assert http.delete.await_count == 4
    asyncio.run(scenario())


def test_emergency_waits_for_inflight_creation_before_deleting(monkeypatch):
    async def scenario():
        http, _, deletions, _ = fixture(monkeypatch)
        started, finish = asyncio.Event(), asyncio.Event()
        async def post(*args, **kwargs):
            started.set()
            await finish.wait()
            return response(201, {"data": {"id": 1, "attributes": {"hashid": "latePost"}}})
        http.post.side_effect = post
        sender = Sender()
        await sender.queue_run(URL, "Title", "", 1, 0)
        await started.wait()
        await sender.queue_delete()
        await asyncio.sleep(0)
        assert sender.stop_event.is_set() and sender.deleting
        http.delete.assert_not_awaited()
        finish.set()
        await sender.delete_worker
        assert deletions == [(1, ENDPOINT + "/latePost")]
        assert sender.deleted == sender.sent == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [
    response(403), response(200, {"errors": ["denied"]}),
    response(200, headers={"cf-mitigated": "challenge"}), TimeoutError(),
])
def test_failed_deletions_are_retained_for_retry(monkeypatch, failure):
    async def scenario():
        http, _, _, _ = fixture(monkeypatch)
        sender = Sender()
        await sender.queue_run(URL, "Title", "", 1, 0)
        await sender.worker
        http.delete.side_effect = [failure, response()]
        await sender.queue_delete()
        await sender.delete_worker
        assert sender.phase == "delete_incomplete" and sender.deleted == 0
        assert sender.status()["delete_pending"] == 1
        await sender.queue_delete()
        await sender.delete_worker
        assert sender.phase == "deleted" and sender.deleted == 1
    asyncio.run(scenario())


def test_rate_limit_prevents_immediate_retry(monkeypatch):
    async def scenario():
        http, _, _, _ = fixture(monkeypatch)
        sender = Sender()
        await sender.queue_run(URL, "Title", "", 1, 0)
        await sender.worker
        http.delete.side_effect = None
        http.delete.return_value = response(429, headers={"Retry-After": "60"})
        await sender.queue_delete()
        await sender.delete_worker
        assert sender.status()["delete_retry_after"] > 0
        with pytest.raises(ValueError, match="요청 제한"):
            await sender.queue_delete()
        assert http.delete.await_count == 1 and sender.status()["delete_pending"] == 1
    asyncio.run(scenario())


def test_missing_hashids_are_reported_and_never_guessed(monkeypatch):
    async def scenario():
        http, _, _, _ = fixture(monkeypatch)
        http.post.side_effect = None
        http.post.return_value = response(201, {"data": {"id": 99}})
        sender = Sender()
        await sender.queue_run(URL, "Title", "", 1, 0)
        await sender.worker
        await sender.queue_delete()
        await sender.delete_worker
        assert sender.untracked == 1 and sender.phase == "delete_incomplete"
        http.delete.assert_not_awaited()
    asyncio.run(scenario())


def test_unknown_creation_does_not_claim_everything_was_deleted(monkeypatch):
    async def scenario():
        http, _, _, _ = fixture(monkeypatch)
        http.post.side_effect = TimeoutError()
        sender = Sender()
        await sender.queue_run(URL, "Title", "", 1, 0)
        await sender.worker
        await sender.queue_delete()
        await sender.delete_worker
        assert sender.untracked == 1 and sender.phase == "delete_incomplete"
        http.delete.assert_not_awaited()
    asyncio.run(scenario())


def test_partial_deletion_retry_skips_confirmed_posts(monkeypatch):
    async def scenario():
        http, _, _, _ = fixture(monkeypatch)
        sender = Sender()
        await sender.queue_run(URL, "Title", "", 2, 0)
        await sender.worker
        http.delete.side_effect = [response(), response(403), response(404)]
        await sender.queue_delete()
        await sender.delete_worker
        assert sender.deleted == 1 and sender.status()["delete_pending"] == 1
        first = http.delete.call_args_list[0].args[0]
        await sender.queue_delete()
        await sender.delete_worker
        assert sender.deleted == 2 and sender.phase == "deleted"
        assert http.delete.call_args_list[2].args[0] != first
    asyncio.run(scenario())


def test_emergency_during_preparation_closes_without_creating(monkeypatch):
    async def scenario():
        http, _, _, browsers = fixture(monkeypatch)
        import server.engine as engine
        gate = asyncio.Event()
        async def hold(*args):
            await gate.wait()
        monkeypatch.setattr(engine.BrowserSession, "open", hold)
        sender = Sender()
        await sender.queue_run(URL, "Title", "", 1, 0)
        await sender.queue_delete()
        await sender.delete_worker
        assert sender.phase == "deleted" and browsers[0].closed
        http.post.assert_not_awaited()
        http.delete.assert_not_awaited()
    asyncio.run(scenario())


def test_delete_transport_rejects_other_resources_and_unsafe_ids():
    async def scenario():
        page = SimpleNamespace(url=URL, is_closed=lambda: False, evaluate=AsyncMock(
            return_value={"status": 204, "text": "", "headers": {}}))
        http = BrowserHTTP(page, ENDPOINT)
        for url in [ENDPOINT, ENDPOINT + "/../walls", ENDPOINT + "/post1?all=true",
                    "https://evil.example/api/9/wishes/post1", "https://padlet.com/api/9/walls/1"]:
            with pytest.raises(ValueError):
                await http.delete(url)
        await http.delete(ENDPOINT + "/post1")
        assert page.evaluate.await_count == 1
        assert page.evaluate.call_args.args[1]["method"] == "DELETE"
        assert deletion_hashid({"data": {"id": 1, "attributes": {"hashid": "../all"}}}) is None
    asyncio.run(scenario())


def test_deletion_reserves_browser_and_survives_idle_collection():
    async def scenario():
        registry = Sessions()
        first, second = registry.create(), registry.create()
        a, b = registry.items[first].sender, registry.items[second].sender
        a.deleting = True
        registry.items[first].last_seen -= 1900
        await registry.collect()
        assert first in registry.items
        with pytest.raises(HTTPException):
            await registry.delete(b)
        with pytest.raises(HTTPException):
            await registry.run(b, SimpleNamespace())
        await registry.shutdown()
    asyncio.run(scenario())


def test_delete_api_requires_auth_and_uses_only_callers_history(licensed_session):
    with TestClient(app) as client:
        assert client.post("/api/emergency-delete", json={}).status_code == 401
        tokens = [licensed_session() for _ in range(2)]
        a, b = [sessions.items[token].sender for token in tokens]
        a.queue_delete, b.queue_delete = AsyncMock(), AsyncMock()
        result = client.post("/api/emergency-delete", headers={"Authorization": "Bearer " + tokens[0]},
                             json={"posts": ["someone-elses-post"]})
        assert result.status_code == 200
        a.queue_delete.assert_awaited_once_with()
        b.queue_delete.assert_not_awaited()
