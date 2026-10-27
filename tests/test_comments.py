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


async def deliver(commenter, identity):
    commenter.enqueue(identity)
    commenter.finish()
    await commenter.run()


def test_429_retries_only_rejected_comment_and_respects_wait():
    async def scenario():
        sender, commenter = make_commenter()
        sender.http.post.side_effect = [response(429, headers={"Retry-After": "2"}),
            response(201, {"data": {"attributes": {"id": 50, "wish_id": 7}}}),
            response(201, {"data": {"attributes": {"id": 51, "wish_id": 7}}})]
        sender.wait = AsyncMock(return_value=False)
        await deliver(commenter, 7)
        assert sender.http.post.await_count == 3 and sender.comments_sent == 2
        assert 1.5 < sender.wait.await_args.args[0] <= 2
        assert sender.comments_errors == 0
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [RuntimeError("private-token"), response(403), response(201, {})])
def test_uncertain_or_denied_comments_stop_without_resend(failure):
    async def scenario():
        sender, commenter = make_commenter()
        sender.http.post.side_effect = [failure]
        await deliver(commenter, 7)
        assert sender.http.post.await_count == 1 and commenter.failed
        assert not sender.stop_event.is_set() and sender.comments_sent == 0
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
        await deliver(commenter, 7)
        assert all(b-a >= .045 for a,b in zip(times, times[1:]))
        sender, commenter = make_commenter(count=3, delay=3600)
        first = asyncio.Event()
        async def start(*args, **kwargs):
            first.set()
            return response(201, {"data": {"attributes": {"id": 1, "wish_id": 7}}})
        sender.http.post.side_effect = start
        task = asyncio.create_task(deliver(commenter, 7))
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
        await deliver(commenter, 7)
        sender.http.post.assert_not_awaited()
        sender, commenter = make_commenter()
        started, finish = asyncio.Event(), asyncio.Event()
        async def post(*args, **kwargs):
            started.set()
            await finish.wait()
            return response(201, {"data": {"attributes": {"id": 50, "wish_id": 7}}})
        sender.http.post.side_effect = post
        task = asyncio.create_task(deliver(commenter, 7))
        await started.wait()
        sender.stop()
        finish.set()
        await asyncio.wait_for(task, 1)
        assert sender.comments_sent == 1 and sender.http.post.await_count == 1
        assert sender.active_batch["comments"] == {50}
    asyncio.run(scenario())


def test_every_page_is_covered_in_round_robin_order(monkeypatch):
    async def scenario():
        http, posts, comments, *_ = fixture(monkeypatch, can_post=False)
        original = http.get.side_effect
        async def get(url, **kwargs):
            if not url.endswith("/wishes"):
                return await original(url, **kwargs)
            cursor = kwargs["params"]["page_start"]
            ids = [100, 101] if not cursor else [101, 102, 103]
            return response(data={"data": [{"attributes": {"id": i, "wall_id": 22}} for i in ids],
                                  "meta": {"next": "page-2" if not cursor else None}})
        http.get.side_effect = get
        sender = Sender()
        await sender.queue_run(URL, "", "", 1, 0,
                               comments={"enabled": True, "body": "text", "count": 3, "delay": 0})
        await sender.worker
        assert [c["wish_id"] for c in comments] == [100, 101, 102, 103] * 3
        assert sender.comments_targets == 4 and sender.comments_total == 12
        assert not posts and sender.phase == "finished"
    asyncio.run(scenario())


@pytest.mark.parametrize("limited_kind", ["comments", "posts"])
def test_rate_limit_wait_is_isolated_and_other_worker_keeps_running(monkeypatch, limited_kind):
    async def scenario():
        http, posts, comments, _, browsers = fixture(monkeypatch)
        original = http.post.side_effect
        blocked, release, other_finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        first = True
        async def post(url, **kwargs):
            nonlocal first
            is_comment = url.endswith("/comments")
            if is_comment == (limited_kind == "comments") and first:
                first = False
                return response(429, headers={"Retry-After": "30"})
            result = await original(url, **kwargs)
            if (limited_kind == "comments" and len(posts) == 3 or
                    limited_kind == "posts" and len(comments) == 2):
                other_finished.set()
            return result
        http.post.side_effect = post
        sender = Sender()
        async def wait(seconds):
            blocked.set()
            await release.wait()
            return sender.stop_event.is_set()
        sender.wait = wait
        await sender.queue_run(URL, "title", "", 3, 0,
                               comments={"enabled": True, "body": "text", "count": 2, "delay": 0})
        try:
            await asyncio.wait_for(blocked.wait(), 1)
            await asyncio.wait_for(other_finished.wait(), 1)
            assert sender.running and not sender.stop_event.is_set()
            assert not browsers[0].closed
            if limited_kind == "comments":
                assert sender.commenter.phase == "backoff" and len(posts) == 3
            else:
                assert sender.commenter.backoff_until == 0 and len(comments) == 2
        finally:
            release.set()
            await asyncio.wait_for(sender.worker, 2)
        assert len(posts) == 3 and len(comments) == 8
        assert sender.phase == "finished" and browsers[0].closed
    asyncio.run(scenario())


@pytest.mark.parametrize("failure_kind", ["comments", "posts"])
@pytest.mark.parametrize("failure", [response(403), RuntimeError("private-token"), response(201, {})])
def test_fatal_failure_only_stops_its_own_worker(monkeypatch, failure_kind, failure):
    async def scenario():
        http, posts, comments, *_ = fixture(monkeypatch)
        original = http.post.side_effect
        async def post(url, **kwargs):
            if url.endswith("/comments") == (failure_kind == "comments"):
                if isinstance(failure, Exception):
                    raise failure
                return failure
            return await original(url, **kwargs)
        http.post.side_effect = post
        sender = Sender()
        await sender.queue_run(URL, "title", "", 3, 0,
                               comments={"enabled": True, "body": "text", "count": 2, "delay": 0})
        await asyncio.wait_for(sender.worker, 2)
        assert not sender.stop_event.is_set()
        if failure_kind == "comments":
            assert len(posts) == 3 and sender.commenter.failed
        else:
            assert len(comments) == 2 and not sender.commenter.failed
        assert sender.phase == "error"
        assert "private-token" not in json.dumps(sender.status())
    asyncio.run(scenario())


def test_comment_listing_failure_does_not_prevent_posts(monkeypatch):
    async def scenario():
        http, posts, comments, *_ = fixture(monkeypatch)
        original = http.get.side_effect
        async def get(url, **kwargs):
            if url.endswith("/wishes"):
                return response(429)
            return await original(url, **kwargs)
        http.get.side_effect = get
        sender = Sender()
        await sender.queue_run(URL, "title", "", 3, 0,
                               comments={"enabled": True, "body": "text", "delay": 0})
        await asyncio.wait_for(sender.worker, 2)
        assert len(posts) == 3 and not comments
        assert sender.commenter.failed and not sender.stop_event.is_set()
    asyncio.run(scenario())


def test_post_preparation_failure_does_not_prevent_comments(monkeypatch):
    async def scenario():
        http, posts, comments, *_ = fixture(monkeypatch)
        monkeypatch.setattr("server.engine.read_fields", AsyncMock(side_effect=ValueError("invalid")))
        sender = Sender()
        await sender.queue_run(URL, "title", "", 3, 0,
                               comments={"enabled": True, "body": "text", "count": 2, "delay": 0})
        await sender.worker
        assert not posts and len(comments) == 2
        assert sender.posts_phase == "error" and sender.commenter.phase == "finished"
    asyncio.run(scenario())


def test_repeated_comment_rate_limits_eventually_resume_without_stopping_posts(monkeypatch):
    async def scenario():
        http, posts, comments, *_ = fixture(monkeypatch)
        original = http.post.side_effect
        attempts = 0
        async def post(url, **kwargs):
            nonlocal attempts
            if url.endswith("/comments"):
                attempts += 1
                if attempts <= 8:
                    return response(429, headers={"Retry-After": "1"})
            return await original(url, **kwargs)
        http.post.side_effect = post
        sender = Sender()
        sender.wait = AsyncMock(return_value=False)
        await sender.queue_run(URL, "title", "", 3, 0,
                               comments={"enabled": True, "body": "text", "delay": 0})
        await sender.worker
        assert len(posts) == 3 and not sender.commenter.failed and not sender.stop_event.is_set()
        assert len(comments) == 4 and attempts == 12 and sender.phase == "finished"
    asyncio.run(scenario())


def test_stop_interrupts_repeated_rate_limit_wait():
    async def scenario():
        sender, commenter = make_commenter()
        blocked = asyncio.Event()
        async def post(*args, **kwargs):
            blocked.set()
            return response(429, headers={"Retry-After": "3600"})
        sender.http.post.side_effect = post
        task = asyncio.create_task(deliver(commenter, 7))
        await blocked.wait()
        sender.stop()
        await asyncio.wait_for(task, 1)
        assert sender.comments_sent == 0 and sender.http.post.await_count == 1
        assert commenter.phase == "stopped"
    asyncio.run(scenario())


def test_deleted_post_is_skipped_without_losing_other_targets():
    async def scenario():
        sender, commenter = make_commenter(count=2)
        ids = []
        async def post(url, **kwargs):
            identity = kwargs["json"]["attributes"]["wish_id"]
            ids.append(identity)
            if identity == 7:
                return response(404)
            return response(201, {"data": {"attributes": {"id": len(ids), "wish_id": identity}}})
        sender.http.post.side_effect = post
        for identity in (7, 8, 9):
            commenter.enqueue(identity)
        commenter.finish()
        await commenter.run()
        assert ids == [7, 8, 9, 8, 9] and sender.comments_sent == 4
        assert not commenter.failed and not sender.stop_event.is_set()
    asyncio.run(scenario())


def test_comment_retry_after_http_date_is_respected(monkeypatch):
    from email.utils import formatdate
    async def scenario():
        sender, commenter = make_commenter(count=1)
        sender.http.post.side_effect = [response(429, headers={"Retry-After": formatdate(time.time()+120, usegmt=True)}),
            response(201, {"data": {"attributes": {"id": 50, "wish_id": 7}}})]
        sender.wait = AsyncMock(return_value=False)
        await deliver(commenter, 7)
        assert 118 < sender.wait.await_args.args[0] <= 120
        assert sender.comments_sent == 1
    asyncio.run(scenario())
