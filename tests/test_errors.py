import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.engine import Sender
from server.errors import rejection_detail, rejection_message


def response(data=None, text=None, status=422):
    text = json.dumps(data) if text is None else text
    return SimpleNamespace(status_code=status, text=text, headers={}, json=lambda: json.loads(text))


@pytest.mark.parametrize('data', [
    {'errors': [{'detail': 'Body is required'}]},
    {'error': {'message': 'Body is required'}},
    {'message': 'Body is required'},
    {'reasons': [{'field': 'body', 'message': 'Body is required'}]},
    {'errors': {'body': ['Body is required']}},
])
def test_validation_reasons_are_visible(data):
    message = rejection_message(response(data))
    assert 'HTTP 422' in message and 'Body is required' in message


def test_diagnostics_do_not_expose_headers_cookies_or_echoed_request():
    data = {'errors': [{'detail': 'Bad token: short-secret; cookie: private-cookie'}],
            'request': {'subject': 'private post', 'headers': {'secret': 'hidden'}}}
    detail = rejection_detail(response(data), ['short-secret', 'private-cookie'])
    assert 'short-secret' not in detail and 'private-cookie' not in detail
    assert 'private post' not in detail and 'hidden' not in detail


@pytest.mark.parametrize('text', ['<html>Private error page</html>', '', 'not JSON', 'x' * 65537],
                         ids=['html', 'empty', 'non-json', 'oversize'])
def test_unstructured_errors_have_safe_fallback(text):
    assert rejection_detail(response(text=text)) == ''
    assert '구체적인 사유' in rejection_message(response(text=text))


def test_csrf_failure_is_distinguished_from_missing_fields():
    message = rejection_message(response(text='<html>ActionController::InvalidAuthenticityToken</html>'))
    assert '인증이 만료' in message and '<html>' not in message


def test_diagnostic_length_and_number_of_errors_are_bounded():
    assert len(rejection_detail(response({'errors': [{'detail': 'invalid value ' * 200}] * 20}))) <= 500


def test_422_stops_once_and_keeps_actionable_reason():
    async def scenario():
        sender = Sender()
        sender.ready = True
        sender.http = SimpleNamespace(post=AsyncMock(return_value=response(
            {'errors': [{'detail': 'A required field is missing'}]})))
        await sender.start('Title', '', 50, 0)
        await sender.worker
        assert sender.phase == 'error' and not sender.running
        assert sender.errors == 1 and sender.sent == 0
        sender.http.post.assert_awaited_once()
        assert 'A required field is missing' in sender.logs[-1]['message']
    asyncio.run(scenario())
