"""Durable Discord webhook outbox, independent of the local bot process."""
import asyncio
import json
import logging
import math
import time
from datetime import datetime
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .license_store import KST

LOG_CHANNEL_ID = 1562751204935602258


def event_message(event):
    data = json.loads(event['payload'])
    title = {'grant': '발급권 지급', 'issued': '키 발급 완료', 'login': '키 인증', 'used': '실행 1회 사용',
             'revoked': '키 폐기', 'grants_revoked': '발급권 회수', 'all_revoked': '전체 키 폐기'}.get(event['kind'], event['kind'])
    lines = [f'**{title}** · 기록 #{event["id"]}',
             datetime.fromtimestamp(event['created'], KST).strftime('%Y-%m-%d %H:%M:%S (UTC+9)')]
    if data.get('user_id'):
        lines.append(f'지급 대상: <@{data["user_id"]}> (ID: {data["user_id"]})')
    if data.get('id'):
        lines.append(f'발급 번호: `{data["id"]}`')
    kind, amount = data.get('kind'), data.get('amount')
    if kind:
        lines.append('종류: ' + {'uses': f'실행 {amount}회', 'days': f'{amount}일', 'month': '1달', 'year': '1년', 'forever': '영구'}[kind])
    if 'quantity' in data:
        lines.append(f'발급권 수량: {data["quantity"]}개')
    if 'remaining' in data:
        if data['remaining'] is not None:
            lines.append(f'남은 실행: {data["remaining"]}회')
        elif data['expires'] is not None:
            seconds = max(0, math.ceil(data['expires'] - event['created']))
            hours, seconds = divmod(seconds, 3600)
            minutes, seconds = divmod(seconds, 60)
            lines.append(f'기록 시점 잔여 시간: {hours}시간 {minutes}분 {seconds}초')
            lines.append('만료: ' + datetime.fromtimestamp(data['expires'], KST).strftime('%Y-%m-%d %H:%M:%S (UTC+9)'))
        else:
            lines.append('잔여 기간: 영구')
    if 'count' in data:
        lines.append(f'폐기한 키: {data["count"]}개')
    if data.get('scope'):
        lines.append('폐기 범위: ' + data['scope'])
    return {'content': '\n'.join(lines), 'allowed_mentions': {'parse': []}}


def send_event(url, event):
    payload = json.dumps(event_message(event)).encode('utf-8')
    request = Request(url + '?wait=true', data=payload,
                      headers={'Content-Type': 'application/json',
                               'User-Agent': 'DiscordBot (https://dev-cat0.github.io/padlet/, 2.0)'})
    try:
        with urlopen(request, timeout=10) as response:
            return 200 <= response.status < 300, 30
    except HTTPError as error:
        try:
            details = json.loads(error.read(4096))
        except (ValueError, OSError):
            details = {}
        if not isinstance(details, dict):
            details = {}
        # Never log the exception string: it may include the secret webhook URL.
        logging.getLogger(__name__).warning('Discord audit HTTP %s (API code %s)', error.code, details.get('code', 'unknown'))
        if error.code == 429:
            try:
                return False, min(3600, max(1, float(details['retry_after'])))
            except (ValueError, KeyError, TypeError):
                pass
        return False, 60
    except Exception as error:
        logging.getLogger(__name__).warning('Discord audit transport failure (%s)', type(error).__name__)
        return False, 30


async def worker(access):
    maintenance_at = 0
    while True:
        access.audit_signal.clear()
        delay = 60
        try:
            if access.store:
                store = access.store
                url = await asyncio.to_thread(store.webhook)
                if url:
                    event = await asyncio.to_thread(store.next_event)
                    if event:
                        delivered, retry = await asyncio.to_thread(send_event, url, event)
                        await asyncio.to_thread(store.finish_event, event['id'], delivered, retry)
                        delay = .5 if delivered else retry
                if time.monotonic() >= maintenance_at:
                    await asyncio.to_thread(store.maintenance)
                    maintenance_at = time.monotonic() + 600
        except Exception as error:
            logging.getLogger(__name__).warning('Audit queue unavailable (%s)', type(error).__name__)
        try:
            await asyncio.wait_for(access.audit_signal.wait(), timeout=delay)
        except TimeoutError:
            pass
