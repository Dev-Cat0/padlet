import io
import logging
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest
from urllib.error import HTTPError

from server import audit


EVENT = {'id': 'test-event', 'kind': 'all_revoked', 'created': 1, 'payload': '{}'}
SECRET_URL = 'https://discord.com/api/webhooks/123/private-test-secret'


def test_rate_limit_is_retried_without_leaking_webhook(monkeypatch, caplog):
    def limited(request, timeout):
        assert request.get_header('User-agent').startswith('DiscordBot (')
        raise HTTPError(request.full_url, 429, 'rate limited', {}, io.BytesIO(b'{"retry_after":2.5,"code":20028}'))
    monkeypatch.setattr(audit, 'urlopen', limited)
    with caplog.at_level(logging.WARNING):
        assert audit.send_event(SECRET_URL, EVENT) == (False, 2.5)
    assert 'HTTP 429' in caplog.text and '20028' in caplog.text
    assert 'private-test-secret' not in caplog.text


def test_non_json_discord_error_stays_queued(monkeypatch, caplog):
    def blocked(request, timeout):
        raise HTTPError(request.full_url, 403, 'blocked', {}, io.BytesIO(b'<html>Blocked</html>'))
    monkeypatch.setattr(audit, 'urlopen', blocked)
    assert audit.send_event(SECRET_URL, EVENT) == (False, 60)
    assert 'HTTP 403' in caplog.text and 'private-test-secret' not in caplog.text


def test_network_failure_does_not_log_exception_url(monkeypatch, caplog):
    def failed(*args, **kwargs):
        raise TimeoutError(SECRET_URL)
    monkeypatch.setattr(audit, 'urlopen', failed)
    assert audit.send_event(SECRET_URL, EVENT) == (False, 30)
    assert 'TimeoutError' in caplog.text and 'private-test-secret' not in caplog.text


def test_header_only_rate_limit_honors_retry_after(monkeypatch):
    def limited(request, timeout):
        raise HTTPError(request.full_url, 429, 'rate limited', {'Retry-After': '120'}, io.BytesIO(b'<html>limited</html>'))
    monkeypatch.setattr(audit, 'urlopen', limited)
    assert audit.send_event(SECRET_URL, EVENT) == (False, 120)


def test_bot_relay_acknowledges_only_after_success():
    from keybot.audit import relay_once
    async def scenario():
        store = SimpleNamespace(call=Mock(side_effect=[{'event': {'id':'abc', 'content':'audit'}}, {'ok':True}]))
        channel = SimpleNamespace(send=AsyncMock())
        assert await relay_once(store, channel)
        assert channel.send.await_args.args == ('audit',)
        assert not channel.send.await_args.kwargs['allowed_mentions'].everyone
        store.call.assert_called_with('audit_finish', event_id='abc', delivered=True)
    asyncio.run(scenario())


def test_failed_local_delivery_stays_pending():
    from keybot.audit import relay_once
    async def scenario():
        store = SimpleNamespace(call=Mock(side_effect=[{'event': {'id':'abc', 'content':'audit'}}, {'ok':True}]))
        with pytest.raises(RuntimeError):
            await relay_once(store, SimpleNamespace(send=AsyncMock(side_effect=RuntimeError('offline'))))
        store.call.assert_called_with('audit_finish', event_id='abc', delivered=False)
    asyncio.run(scenario())
