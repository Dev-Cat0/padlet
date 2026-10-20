import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.fields import read_fields, validate_fields
from server.engine import Sender


def field(kind='text', required=True, **extra):
    return {'id': 'field-1', 'name': '추가 질문', 'type': kind, 'required': required,
            'supported': True, 'options': [], **extra}


def test_required_core_fields_and_body_only_board():
    with pytest.raises(ValueError, match='본문'):
        validate_fields({'required': {'body': True}}, 'title', '', '', {})
    subject, body, extra = validate_fields({'subject_enabled': False}, 'ignored', 'body', '', {})
    assert subject == '' and body == 'body'
    with pytest.raises(ValueError, match='하나 이상'):
        validate_fields({}, ' ', '', '', {})


@pytest.mark.parametrize('kind,value', [('text', '답변'), ('number', 0), ('date', '2026-10-20'),
    ('date', '2026-10-20T01:00:00Z'), ('email', 'a@example.com'), ('url', 'https://example.com'),
    ('phone_number', '010-0000-0000')])
def test_typed_custom_values_are_preserved(kind, value):
    _, _, extra = validate_fields({'custom': [field(kind)]}, '', '', '', {'field-1': value})
    assert extra['custom_properties'] == {'field-1': value}


@pytest.mark.parametrize('kind,value', [('number', '5'), ('number', float('nan')),
    ('number', float('inf')), ('number', True), ('date', 'yesterday'), ('date', '2026-02-30'),
    ('email', 'invalid'), ('url', 'javascript:alert(1)'), ('text', ' ')])
def test_invalid_custom_values_are_rejected(kind, value):
    with pytest.raises(ValueError, match='추가 질문'):
        validate_fields({'custom': [field(kind)]}, 'title', '', '', {'field-1': value})


def test_selection_uses_board_options_and_default_without_overriding_explicit_empty():
    fields = {'custom': [field('single_select', options=[{'id': 'a', 'default': True}])]}
    assert validate_fields(fields, 'title', '', '', {})[2]['custom_properties'] == {'field-1': 'a'}
    for value in ['other-board-option', None]:
        with pytest.raises(ValueError):
            validate_fields(fields, 'title', '', '', {'field-1': value})


def test_fields_from_previous_board_are_rejected_and_unsupported_required_is_named():
    with pytest.raises(ValueError, match='없는 추가 항목'):
        validate_fields({}, 'title', '', '', {'old-field': 'answer'})
    with pytest.raises(ValueError, match='Padlet에서 직접'):
        validate_fields({'custom': [field('user', supported=False)]}, 'title', '', '', {})


def test_attachment_requirement_and_protocol():
    for attachment in ['', 'javascript:alert(1)']:
        with pytest.raises(ValueError):
            validate_fields({'required': {'attachment': True}}, 'title', '', attachment, {})
    assert validate_fields({}, '', '', 'https://example.com/image.png', {})[2]['attachment'].endswith('.png')


def test_validation_happens_before_any_post_and_values_reach_every_section():
    async def scenario():
        sender = Sender()
        sender.ready = True
        sender.board = {'fields': {'required': {'body': True}, 'custom': [field('number')]},
                        'sections': [{'id': 1, 'title': 'A'}, {'id': 2, 'title': 'B'}]}
        sender.http = SimpleNamespace(post=AsyncMock(return_value=SimpleNamespace(status_code=201)))
        with pytest.raises(ValueError, match='본문'):
            await sender.start('title', '', 1, 0, custom_properties={'field-1': 0})
        sender.http.post.assert_not_awaited()
        assert not sender.running
        await sender.start('title', 'body', 1, 0, custom_properties={'field-1': 0})
        await sender.worker
        assert sender.sent == 2
        assert all(call.kwargs['json']['custom_properties'] == {'field-1': 0}
                   for call in sender.http.post.call_args_list)
    asyncio.run(scenario())


def reply(data):
    return SimpleNamespace(status_code=200, content=b'{}', json=lambda: {'data': data})


def test_field_discovery_is_get_only_and_uses_each_board_id():
    async def scenario():
        for wall_id in [22, 1234]:
            http = SimpleNamespace(get=AsyncMock(side_effect=[
                reply({'attributes': {'required_fields': {'body': True}}}),
                reply([{'attributes': {'id': 99, 'data_type': 'text', 'required': True,
                                       'name': 'Question', 'selection_options': None}}])]))
            fields = await read_fields(http, 'https://padlet.com', {}, wall_id)
            assert fields['required']['body'] and fields['custom'][0]['id'] == '99'
            assert all(f'/walls/{wall_id}/' in call.args[0] and call.kwargs['allow_redirects'] is False
                       for call in http.get.call_args_list)
    asyncio.run(scenario())


@pytest.mark.parametrize('core,custom', [([], []), ({'attributes': {}}, {}),
    ({'attributes': {'required_fields': 'invalid'}}, []), ({'attributes': {}}, [{'attributes': {}}])])
def test_malformed_field_settings_fail_with_clear_message(core, custom):
    async def scenario():
        http = SimpleNamespace(get=AsyncMock(side_effect=[reply(core), reply(custom)]))
        with pytest.raises(ValueError, match='입력 항목 형식'):
            await read_fields(http, 'https://padlet.com', {}, 22)
    asyncio.run(scenario())
