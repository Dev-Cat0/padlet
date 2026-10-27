import asyncio
import copy
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from server.app import RunRequest
from server.browser import BrowserHTTP, BrowserResponse
from server.comments import CommentOptions, CommentSender, read_posts
from server.engine import Sender

URL = "https://padlet.com/example/board"
STATE = {"canIWrite": True, "canIComment": True,
         "wall": {"id": 22, "hashid": "board"},
         "user": {"id": 31, "hashid": "guest", "registered_at": None}}


def response(status=200, data=None, headers=None):
    return BrowserResponse({"status": status, "text": json.dumps(data or {}), "headers": headers or {}})


def fixture(monkeypatch, allowed=True, can_post=True, wall_overrides=None):
    posts, comments, deleted, browsers = [], [], [], []
    state = copy.deepcopy(STATE)
    state["canIComment"] = allowed
    state["canIWrite"] = can_post
    state["isReadOnly"] = not can_post
    state["wall"].update(wall_overrides or {})

    async def get(url, **kwargs):
        if "/padlet-starting-state" in url:
            return response(data=state)
        assert url.endswith("/api/9/wishes")
        return response(data={"data": [{"id": "100", "attributes": {"id": 100, "wall_id": 22}}]})

    async def post(url, **kwargs):
        if url.endswith("/wishes"):
            posts.append(kwargs["json"])
            number = len(posts)
            return response(201, {"data": {"id": str(number), "attributes": {
                "id": number, "wall_id": 22, "hashid": f"post{number}"}}})
        assert url.endswith("/api/8/comments")
        attrs = kwargs["json"]["attributes"]
        comments.append(attrs)
        return response(201, {"data": {"attributes": {"id": 500 + len(comments), "wish_id": attrs["wish_id"]}}})

    async def delete(url, **kwargs):
        deleted.append(url)
        return response(204)

    http = SimpleNamespace(origin="https://padlet.com", get=AsyncMock(side_effect=get),
                           post=AsyncMock(side_effect=post), delete=AsyncMock(side_effect=delete), close=AsyncMock())

    class Browser:
        def __init__(self, sender):
            self.closed = False
            browsers.append(self)

        async def open(self, url):
            pass

        async def sending_page(self):
            return SimpleNamespace(url=URL)

        async def snapshot(self):
            return {"secret": "guest-cookie"}

        async def restore(self, snapshot):
            assert snapshot == {"secret": "guest-cookie"}
            return SimpleNamespace(url=URL)

        async def close(self):
            self.closed = True

    monkeypatch.setattr("server.engine.BrowserSession", Browser)
    monkeypatch.setattr("server.engine.BrowserHTTP", lambda *args: http)
    monkeypatch.setattr("server.engine.read_sections", AsyncMock(return_value=[]))
    monkeypatch.setattr("server.engine.read_fields", AsyncMock(return_value={}))
    return http, posts, comments, deleted, browsers


def test_existing_and_new_posts_receive_exact_count_and_delete_only_created_items(monkeypatch):
    async def scenario():
        http, posts, comments, deleted, browsers = fixture(monkeypatch)
        sender = Sender()
        await sender.queue_run(URL, "Title", "Body", 2, 0,
                               comments={"enabled": True, "body": "<hello>\nworld", "count": 2, "delay": 0})
        await sender.worker
        assert sender.phase == "finished" and sender.comments_sent == 6
        assert sender.comments_targets == 3 and len(posts) == 2
        assert sorted(c["wish_id"] for c in comments) == [1, 1, 2, 2, 100, 100]
        assert all(c["user_id"] == 31 and c["user_hashid"] == "guest" for c in comments)
        assert comments[0]["html_body"] == "<p>&lt;hello&gt;<br>world</p>"
        assert sender.status()["delete_pending"] == 8
        assert "guest-cookie" not in json.dumps(sender.status())
        assert all(b.closed for b in browsers)
        await sender.queue_delete()
        await sender.delete_worker
        assert sender.phase == "deleted" and sender.deleted == 8
        assert len(deleted) == 8 and len(set(deleted)) == 8
        assert all("/api/5/comments/" in url for url in deleted[:6])
        assert not any(url.endswith("/100") for url in deleted)
        assert not sender.deletion_batches
    asyncio.run(scenario())


def test_comment_permission_denial_falls_back_to_posts(monkeypatch):
    async def scenario():
        http, posts, comments, *_ = fixture(monkeypatch, allowed=False)
        sender = Sender()
        await sender.queue_run(URL, "Title", "", 1, 0, comments={"enabled": True, "body": "text"})
        await sender.worker
        assert sender.phase == "finished" and sender.execution_mode == "posts"
        assert len(posts) == 1 and not comments
        assert any("게시글만" in log["message"] for log in sender.logs)
    asyncio.run(scenario())


def test_post_permission_denial_runs_only_comments_without_post_fields(monkeypatch):
    async def scenario():
        http, posts, comments, *_ = fixture(monkeypatch, can_post=False)
        fields = AsyncMock(side_effect=AssertionError("Post-only fields must not be fetched"))
        monkeypatch.setattr("server.engine.read_fields", fields)
        sender = Sender()
        await sender.queue_run(URL, "", "", 50, 0, comments={"enabled": True,"body": "text","count": 2,"delay": 0})
        await sender.worker
        assert sender.phase == "finished" and sender.execution_mode == "comments"
        assert not posts and len(comments) == 2
        assert sender.comments_total == sender.comments_sent == 2
        fields.assert_not_awaited()
    asyncio.run(scenario())


@pytest.mark.parametrize("allowed,enabled,wall", [(False,True,{}),(True,False,{}),
    (True,True,{"frozen":True}),(True,True,{"is_archived":True}),(True,True,{"is_trashed":True})])
def test_no_permitted_action_or_frozen_board_sends_nothing(monkeypatch,allowed,enabled,wall):
    async def scenario():
        http, *_ = fixture(monkeypatch,allowed=allowed,can_post=False,wall_overrides=wall)
        sender = Sender()
        await sender.queue_run(URL,"","",1,0,comments={"enabled":enabled,"body":"text"})
        await sender.worker
        assert sender.phase == "error"
        http.post.assert_not_awaited()
    asyncio.run(scenario())


def test_disabled_comments_preserve_post_only_behavior(monkeypatch):
    async def scenario():
        http, posts, comments, *_ = fixture(monkeypatch, allowed=False)
        sender = Sender()
        await sender.queue_run(URL, "Title", "", 2, 0)
        await sender.worker
        assert len(posts) == 2 and not comments
        assert http.get.await_count == 1 and sender.phase == "finished"
    asyncio.run(scenario())


@pytest.mark.parametrize("data", [{"body": " "}, {"body": "ok", "count": 0},
                                  {"body": "ok", "count": 1001}, {"body": "ok", "delay": -1},
                                  {"body": "ok", "delay": float("nan")}])
def test_bad_comment_options_fail_before_job_acceptance(data):
    with pytest.raises(ValidationError):
        RunRequest(url=URL, comments={"enabled": True, **data})


def test_pagination_deduplicates_and_rejects_other_board_or_repeated_cursor():
    async def scenario():
        row = {"id": "1", "attributes": {"id": 1, "wall_id": 22}}
        http = SimpleNamespace(origin="https://padlet.com", get=AsyncMock(side_effect=[
            response(data={"data": [row], "meta": {"next": "cursor-2"}}),
            response(data={"data": [row, {"id": "2", "attributes": {"wall_id": 22}}]})]))
        template = {"wall_id": 22, "wall_hashid": "board"}
        assert await read_posts(http, template, lambda: False) == [1, 2]
        assert http.get.await_args.kwargs["params"]["page_start"] == "cursor-2"
        http.get = AsyncMock(return_value=response(data={"data": [row], "meta": {"next": "same"}}))
        with pytest.raises(ValueError, match="반복"):
            await read_posts(http, template, lambda: False)
        row["attributes"]["wall_id"] = 99
        http.get = AsyncMock(return_value=response(data={"data": [row]}))
        with pytest.raises(ValueError, match="게시판"):
            await read_posts(http, template, lambda: False)
    asyncio.run(scenario())


def make_commenter(count=2, delay=0):
    sender = Sender()
    sender.endpoint = "https://padlet.com/api/9/wishes"
    sender.template = {"author_id": 31, "author_hashid": "guest"}
    sender.http = SimpleNamespace(origin="https://padlet.com", post=AsyncMock())
    commenter = CommentSender(sender, CommentOptions(enabled=True, body="text", count=count, delay=delay))
    return sender, commenter


def test_429_retries_only_rejected_comment_and_respects_wait():
    async def scenario():
        sender, commenter = make_commenter()
        sender.http.post.side_effect = [response(429, headers={"Retry-After": "2"}),
            response(201, {"data": {"attributes": {"id": 50, "wish_id": 7}}}),
            response(201, {"data": {"attributes": {"id": 51, "wish_id": 7}}})]
        sender.wait = AsyncMock(return_value=False)
        await commenter.send(7)
        assert sender.http.post.await_count == 3 and sender.comments_sent == 2
        assert 1.5 < sender.wait.await_args.args[0] <= 2
        assert sender.comments_errors == 0
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [RuntimeError("private-token"), response(403), response(201, {})])
def test_uncertain_or_denied_comments_stop_without_resend(failure):
    async def scenario():
        sender, commenter = make_commenter()
        sender.http.post.side_effect = [failure]
        await commenter.send(7)
        assert sender.http.post.await_count == 1 and commenter.failed
        assert sender.stop_event.is_set() and sender.comments_sent == 0
        assert "private-token" not in json.dumps(sender.status())
    asyncio.run(scenario())


def test_comment_interval_and_stop_interrupt_wait():
    async def scenario():
        sender, commenter = make_commenter(count=3, delay=.05)
        times = []
        async def post(*args, **kwargs):
            times.append(time.monotonic())
            return response(201, {"data": {"attributes": {"id": len(times), "wish_id": 7}}})
        sender.http.post.side_effect = post
        await commenter.send(7)
        assert all(b-a >= .045 for a,b in zip(times, times[1:]))
        sender, commenter = make_commenter(count=3, delay=3600)
        first = asyncio.Event()
        async def start(*args, **kwargs):
            first.set()
            return response(201, {"data": {"attributes": {"id": 1, "wish_id": 7}}})
        sender.http.post.side_effect = start
        task = asyncio.create_task(commenter.send(7))
        await first.wait()
        sender.stop()
        await asyncio.wait_for(task, 1)
        assert sender.http.post.await_count == 1
    asyncio.run(scenario())


def test_comment_transport_allows_only_exact_create_and_numeric_delete_endpoints():
    async def scenario():
        page = SimpleNamespace(url=URL, is_closed=lambda: False, evaluate=AsyncMock(return_value={
            "status": 201, "text": "{}", "headers": {}}))
        http = BrowserHTTP(page, "https://padlet.com/api/9/wishes")
        await http.post(http.origin + "/api/8/comments", json={"attributes": {}})
        await http.delete(http.origin + "/api/5/comments/123")
        for method, url in [("POST", http.origin + "/api/8/comments/123"),
                            ("DELETE", http.origin + "/api/5/comments/not-an-id"),
                            ("POST", "https://evil.example/api/8/comments")]:
            with pytest.raises(ValueError):
                await http.request(method, url)
        assert page.evaluate.await_count == 2
    asyncio.run(scenario())


def test_expiry_and_stop_keep_inflight_comment_for_deletion():
    async def scenario():
        sender, commenter = make_commenter()
        sender.license_expires = time.time() - 1
        await commenter.send(7)
        sender.http.post.assert_not_awaited()
        sender, commenter = make_commenter()
        started, finish = asyncio.Event(), asyncio.Event()
        async def post(*args, **kwargs):
            started.set()
            await finish.wait()
            return response(201, {"data": {"attributes": {"id": 50, "wish_id": 7}}})
        sender.http.post.side_effect = post
        task = asyncio.create_task(commenter.send(7))
        await started.wait()
        sender.stop()
        finish.set()
        await asyncio.wait_for(task, 1)
        assert sender.comments_sent == 1 and sender.http.post.await_count == 1
        assert sender.active_batch["comments"] == {50}
    asyncio.run(scenario())
