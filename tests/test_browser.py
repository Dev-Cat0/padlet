import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from server.app import app, sessions
from server.browser import BrowserHTTP, BrowserSession, BrowserResponse, capture_url, created_post
from server.engine import Sender
from server.fields import validate_fields
from server.sessions import Sessions

URL = "https://padlet.com/example/board"
ENDPOINT = "https://padlet.com/api/9/wishes"


def response(status=200, data=None, headers=None):
    return BrowserResponse({"status": status, "text": json.dumps(data or {}),
                            "headers": headers or {}})


def page():
    return SimpleNamespace(url=URL, is_closed=lambda: False, evaluate=AsyncMock())


@pytest.mark.parametrize("url", ["https://evil.example/api/9/wishes",
    "http://padlet.com/api/9/wishes", "https://padlet.com:444/api/9/wishes",
    "https://user:secret@padlet.com/api/9/wishes", "https://padlet.com/api/9/wishes/1"])
def test_only_padlet_create_request_can_be_captured(url):
    assert not capture_url(url)
    assert capture_url(ENDPOINT)


@pytest.mark.parametrize("data", [{}, {"data": []}, {"data": {"attributes": "bad"}},
    {"data": {"id": 5}, "errors": ["failed"]}, {"error": "challenge"}])
def test_http_success_alone_is_not_a_created_post(data):
    assert not created_post(data)
    assert created_post({"data": {"attributes": {"id": 5}}})


def test_transport_stays_in_captured_page_and_does_not_export_cookies():
    async def scenario():
        browser_page = page()
        browser_page.evaluate.return_value = {"status": 201, "text": '{"data":{"id":5}}', "headers": {}}
        transport = BrowserHTTP(browser_page, ENDPOINT)
        assert (await transport.post(ENDPOINT, json={"subject": "new"})).status_code == 201
        script, args = browser_page.evaluate.call_args.args
        assert "credentials:'same-origin'" in script and "redirect:'error'" in script
        assert "document.cookie" not in script and not hasattr(transport, "cookies")
        assert args["payload"] == {"subject": "new"} and "headers" not in args
        for target in ["https://evil.example/api/9/wishes", ENDPOINT + "/5"]:
            with pytest.raises(ValueError):
                await transport.post(target, json={})
        browser_page.url = "https://padlet.com/example/another"
        with pytest.raises(ValueError):
            await transport.post(ENDPOINT, json={})
        assert browser_page.evaluate.await_count == 1
    asyncio.run(scenario())


def test_automatic_browser_disables_live_feed_but_manual_browser_retains_it():
    async def scenario():
        browser = BrowserSession(Sender())
        browser.public_url = AsyncMock(return_value=True)
        route = SimpleNamespace(url="wss://padlet.com/live", close=AsyncMock(),
                                connect_to_server=lambda: None)
        browser.capture_enabled = False
        await browser.websocket(route)
        route.close.assert_awaited_once()
        browser.public_url.assert_not_awaited()
        browser.capture_enabled = True
        await browser.websocket(route)
        browser.public_url.assert_awaited_once()
    asyncio.run(scenario())


@pytest.mark.parametrize("resource_type", ["image", "media", "font", "stylesheet"])
def test_automatic_browser_skips_display_resources(resource_type):
    async def scenario():
        browser = BrowserSession(Sender())
        browser.capture_enabled = False
        browser.public_url = AsyncMock(return_value=True)
        route = SimpleNamespace(request=SimpleNamespace(resource_type=resource_type),
                                abort=AsyncMock(), continue_=AsyncMock())
        await browser.route(route)
        route.abort.assert_awaited_once()
        browser.public_url.assert_not_awaited()
    asyncio.run(scenario())


def test_capture_preserves_successful_payload_and_retains_browser(monkeypatch):
    async def scenario():
        sender = Sender()
        browser = BrowserSession(sender)
        sender.browser = browser
        browser_page = page()
        payload = {"wall_id": 12, "author_id": 30, "body": "<p><strong>rich</strong></p>",
                   "subject": "sample", "published": False, "custom_properties": {"photo": {"asset_id": 4}}}
        http = SimpleNamespace(origin="https://padlet.com", get=AsyncMock(return_value=response(data={
            "canIWrite": True, "wall": {"id": 12, "hashid": "board", "title": "Board", "require_approval": True},
            "user": {"id": 30, "hashid": "anon", "registered_at": None}})),
            close=AsyncMock())
        monkeypatch.setattr("server.engine.BrowserHTTP", lambda *args: http)
        monkeypatch.setattr("server.engine.read_sections", AsyncMock(return_value=[]))
        monkeypatch.setattr("server.engine.read_fields", AsyncMock(return_value={"custom": []}))
        await sender.capture_browser(browser, browser_page, ENDPOINT, payload)
        assert sender.ready and sender.browser is browser
        assert sender.template == payload and sender.template is not payload
        assert sender.board["draft"]["body"] == "rich"
        assert "author_id" not in str(sender.status()) and not sender.headers
        assert sender.sent == 0
        await sender.reset()
        assert sender.browser is None and not sender.ready
    asyncio.run(scenario())


def test_capture_rejects_different_board(monkeypatch):
    async def scenario():
        sender = Sender()
        sender.browser = BrowserSession(sender)
        http = SimpleNamespace(origin="https://padlet.com", get=AsyncMock(return_value=response(data={
            "canIWrite": True, "wall": {"id": 99, "hashid": "board"},
            "user": {"id": 30, "hashid": "anon", "registered_at": None}})))
        monkeypatch.setattr("server.engine.BrowserHTTP", lambda *args: http)
        with pytest.raises(ValueError, match="게시판"):
            await sender.capture_browser(sender.browser, page(), ENDPOINT, {"wall_id": 12, "author_id": 30})
        assert not sender.ready and not sender.opening
        assert sender.http is None
    asyncio.run(scenario())


def test_manual_capture_ignores_failure_and_non_creation():
    async def scenario():
        sender = Sender()
        browser = BrowserSession(sender)
        sender.capture_browser = AsyncMock()
        request = SimpleNamespace(post_data='{"wall_id":12}', frame=SimpleNamespace(page=page()))
        for result in [SimpleNamespace(status=403), SimpleNamespace(status=200, request=request,
            json=AsyncMock(return_value={"errors": ["bad"]}))]:
            await browser.capture(result)
        sender.capture_browser.assert_not_awaited()
        result = SimpleNamespace(status=201, request=request, url=ENDPOINT,
            json=AsyncMock(return_value={"data": {"id": 5}}))
        await browser.capture(result)
        sender.capture_browser.assert_awaited_once()
    asyncio.run(scenario())


@pytest.mark.parametrize("user", [
    {"id": 30, "hashid": "account", "registered_at": "2026-01-01"},
    {"id": 30, "hashid": "account", "username": "user"},
])
def test_legacy_browser_also_refuses_logged_in_capture(monkeypatch, user):
    async def scenario():
        sender = Sender()
        sender.browser = BrowserSession(sender)
        http = SimpleNamespace(origin="https://padlet.com", get=AsyncMock(return_value=response(data={
            "canIWrite": True, "wall": {"id": 12, "hashid": "board"}, "user": user})))
        monkeypatch.setattr("server.engine.BrowserHTTP", lambda *args: http)
        with pytest.raises(ValueError, match="익명"):
            await sender.capture_browser(sender.browser, page(), ENDPOINT, {"wall_id": 12, "author_id": 30})
        assert not sender.ready
    asyncio.run(scenario())


@pytest.mark.parametrize("result", [
    response(403, headers={"cf-mitigated": "challenge"}),
    response(429, headers={"cf-mitigated": "challenge"}),
    response(200, headers={"cf-mitigated": "challenge"}),
    response(200, {"errors": ["bad"]})])
def test_challenge_or_unconfirmed_success_stops_browser_sender(result):
    async def scenario():
        sender = Sender()
        sender.browser = BrowserSession(sender)
        sender.http = SimpleNamespace(post=AsyncMock(return_value=result), close=AsyncMock())
        sender.ready = True
        await sender.start("Title", "Body", 3, 0)
        await sender.worker
        assert not sender.ready and sender.sent == 0 and sender.errors == 1
        sender.http.post.assert_awaited_once()
        await sender.shutdown()
    asyncio.run(scenario())


def test_captured_required_complex_property_is_preserved():
    fields = {"custom": [{"id": "photo", "name": "Photo", "type": "image",
                         "supported": False, "required": True}]}
    original = {"photo": {"asset_id": 42}}
    _, _, values = validate_fields(fields, "new", "", "", {}, original)
    assert values["custom_properties"] == original
    with pytest.raises(ValueError):
        validate_fields(fields, "new", "", "", {"photo": "replacement"}, original)
    with pytest.raises(ValueError):
        validate_fields(fields, "new", "", "", {})


def test_only_one_browser_can_occupy_server_and_idle_browser_expires():
    async def scenario():
        registry = Sessions()
        a, b = registry.create(), registry.create()
        first, second = registry.items[a].sender, registry.items[b].sender
        first.browser = BrowserSession(first)
        second.open = AsyncMock()
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as error:
            await registry.open(second, URL)
        assert error.value.status_code == 429
        second.open.assert_not_awaited()
        first.last_activity -= 1900
        await registry.collect()
        assert first.browser is None
        await registry.open(second, URL)
        second.open.assert_awaited_once()
        await registry.shutdown()
    asyncio.run(scenario())


def test_browser_routes_require_auth_and_open_uses_capture(monkeypatch):
    with TestClient(app) as client:
        for path in ["/api/browser/frame", "/api/browser/tabs"]:
            assert client.get(path).status_code == 401
        for path in ["/api/browser/open", "/api/browser/action"]:
            assert client.post(path, json={}).status_code == 401
        token = client.post("/api/session", headers={"Origin": "https://dev-cat0.github.io"}, json={}).json()["token"]
        headers = {"Authorization": "Bearer " + token}
        sender = sessions.items[token].sender
        sender.open = AsyncMock()
        sender.connect = AsyncMock()
        for path in ["/api/browser/open", "/api/board/connect"]:
            assert client.post(path, headers=headers, json={"url": URL}).status_code == 200
        assert sender.open.await_count == 2
        sender.connect.assert_not_awaited()
        assert client.post("/api/browser/action", headers=headers,
                           json={"kind": "key", "key": "Control+l"}).status_code == 422
        assert client.get("/api/browser/frame", headers=headers).status_code == 409
