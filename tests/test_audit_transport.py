import io
import logging
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
