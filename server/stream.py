"""Chromium compositor frames and ordered input over one WebSocket."""
import asyncio
import base64
import json
import time
from contextlib import suppress

from fastapi import WebSocketDisconnect
from pydantic import ValidationError


async def browser_stream(socket, sender, action_model):
    previous = sender.stream_socket
    sender.stream_socket = socket
    if previous and previous is not socket:
        with suppress(Exception):
            await previous.close(code=4001)
    send_lock = asyncio.Lock()
    replies = set()

    async def send_json(data):
        async with send_lock:
            await socket.send_json(data)

    def reply(data):
        task = asyncio.create_task(send_json(data))
        replies.add(task)
        def finished(done):
            replies.discard(done)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(finished)

    async def inputs():
        while True:
            raw = await socket.receive_text()
            if len(raw) > 65536 or len(replies) >= 64:
                await socket.close(code=1009)
                return
            try:
                message = json.loads(raw)
                if not isinstance(message, dict):
                    raise ValueError()
                if message.get('type') == 'ping':
                    reply({'type':'pong'})
                    continue
                action = action_model.model_validate(message.get('action', {}))
                await sender.control(action)
                reply({'type':'ack', 'id':message.get('id')})
            except (ValueError, ValidationError):
                await send_json({'type':'error', 'message':'입력 형식을 확인하세요.'})
            except Exception:
                await send_json({'type':'error', 'message':'브라우저 조작 실패. 화면을 다시 확인하세요.'})

    async def frames():
        while True:
            try:
                page = sender.active_page()
            except ValueError:
                return
            cdp = await sender.context.new_cdp_session(page)
            changed = asyncio.Event()
            latest = None
            pending = set()

            def on_frame(frame):
                nonlocal latest
                latest = frame['data']
                pending.add(frame['sessionId'])
                changed.set()

            cdp.on('Page.screencastFrame', on_frame)
            try:
                await cdp.send('Page.startScreencast', {
                    'format':'jpeg', 'quality':40, 'maxWidth':880, 'maxHeight':576,
                    'everyNthFrame':1})
                await page.bring_to_front()
                await send_json({'type':'ready'})
                last_sent = 0
                while not page.is_closed() and sender.page is page:
                    try:
                        await asyncio.wait_for(changed.wait(), timeout=.25)
                    except TimeoutError:
                        continue
                    # Keep only the newest frame; never build a stale image backlog.
                    # Leave CPU headroom for page scripts and input on small instances.
                    await asyncio.sleep(max(0, .25 - (time.monotonic() - last_sent)))
                    changed.clear()
                    image, acks = latest, list(pending)
                    pending.clear()
                    async with send_lock:
                        await socket.send_bytes(base64.b64decode(image))
                    last_sent = time.monotonic()
                    for frame_id in acks:
                        await cdp.send('Page.screencastFrameAck', {'sessionId':frame_id})
            finally:
                cdp.remove_listener('Page.screencastFrame', on_frame)
                with suppress(Exception):
                    await cdp.send('Page.stopScreencast')
                with suppress(Exception):
                    await cdp.detach()

    tasks = [asyncio.create_task(inputs()), asyncio.create_task(frames())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except (WebSocketDisconnect, RuntimeError):
        pass
    except Exception:
        with suppress(Exception):
            await send_json({'type':'fallback'})
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for task in list(replies):
            task.cancel()
        await asyncio.gather(*replies, return_exceptions=True)
        if sender.stream_socket is socket:
            sender.stream_socket = None
        with suppress(Exception):
            await socket.close()
