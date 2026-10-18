"""Local visible Chromium. Only successful Padlet captures go to the fixed server."""
import argparse
import asyncio
import json
import queue
import re
import threading
from urllib.parse import urlsplit

import httpx
from playwright.async_api import async_playwright

SERVER = "https://padlet-sender-panel.onrender.com"


def padlet_url(value):
    parsed = urlsplit(value)
    return (parsed.scheme == 'https' and parsed.hostname in {'padlet.com', 'www.padlet.com'}
            and not parsed.username and not parsed.password and parsed.port in {None, 443})


def capture_url(value):
    return padlet_url(value) and urlsplit(value).path.rstrip('/') == '/api/9/wishes'


class Helper:
    def __init__(self, code, report, stop, server=SERVER):
        self.code, self.report, self.stop, self.server = code, report, stop, server
        self.browser = self.context = self.playwright = self.open_task = None
        self.command_id = ''
        self.busy = False
        self.capture_tasks = set()

    async def request(self, path, body=None):
        response = await self.client.request('GET' if body is None else 'POST',
            self.server + path, json=body)
        response.raise_for_status()
        return response.json()

    async def close_browser(self):
        self.command_id = ''
        if self.open_task and not self.open_task.done():
            self.open_task.cancel()
            await asyncio.gather(self.open_task, return_exceptions=True)
        self.open_task = None
        if self.browser:
            await self.browser.close()
        self.browser = self.context = None
        self.busy = False

    async def capture(self, response, command_id):
        if (self.busy or command_id != self.command_id or response.request.method != 'POST'
                or not capture_url(response.url) or response.status not in {200, 201}):
            return
        self.busy = True
        try:
            template = json.loads(response.request.post_data or '')
            if not isinstance(template, dict) or not template:
                raise ValueError()
            payload = {'command_id': command_id, 'endpoint': response.url, 'status': response.status,
                       'headers': await response.request.all_headers(), 'template': template}
            if len(json.dumps(payload).encode()) > 65536:
                self.report('게시 요청이 너무 큽니다. 첨부파일 없이 짧은 글로 다시 캡처하세요.')
                return
            for attempt in range(3):
                try:
                    await self.request('/api/pc/capture', payload)
                    self.report('캡처 완료! 웹 패널에서 제목·본문을 입력하고 전송하세요.')
                    return
                except httpx.TransportError:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(1)
        except httpx.HTTPStatusError:
            self.report('캡처 연결이 만료됐습니다. 웹 패널에서 브라우저를 다시 여세요.')
        except Exception:
            self.report('캡처 업로드에 실패했습니다. 연결을 확인한 뒤 글을 다시 게시하세요.')
        finally:
            self.busy = False

    def observe(self, response, command_id):
        if response.request.method == 'POST' and capture_url(response.url):
            task = asyncio.create_task(self.capture(response, command_id))
            self.capture_tasks.add(task)
            task.add_done_callback(self.capture_tasks.discard)

    async def opened(self, command):
        try:
            if not padlet_url(command['url']):
                raise ValueError()
            self.browser = await self.playwright.chromium.launch(headless=False)
            self.context = await self.browser.new_context(locale='ko-KR', viewport=None)
            self.context.on('response', lambda response: self.observe(response, command['id']))
            page = await self.context.new_page()
            await page.goto(command['url'], wait_until='domcontentloaded', timeout=60000)
            await self.request('/api/pc/status', {'command_id':command['id'], 'phase':'opened'})
            self.report('열린 PC 브라우저에서 로그인하고 글을 한 번 게시하세요.')
        except asyncio.CancelledError:
            raise
        except Exception:
            self.report('브라우저 실행 실패. 인터넷 연결과 Chromium 설치를 확인하세요.')
            try:
                await self.request('/api/pc/status', {'command_id':command['id'], 'phase':'error'})
            except Exception:
                pass

    async def run(self):
        async with httpx.AsyncClient(timeout=20) as self.client:
            data = await self.request('/api/pc/connect', {'code':self.code})
            self.client.headers['Authorization'] = 'Bearer ' + data['token']
            self.report('연결됨. 웹 패널에서 「내 PC 브라우저 열기」를 누르세요.')
            self.playwright = await async_playwright().start()
            last_id, disconnected = '', False
            try:
                while not self.stop.is_set():
                    try:
                        result = await self.request('/api/pc/command')
                        if disconnected:
                            self.report('서버에 다시 연결됐습니다.')
                            disconnected = False
                        command = result.get('command')
                        if command and command['id'] != last_id:
                            await self.close_browser()
                            last_id = command['id']
                            if command['kind'] == 'open':
                                self.command_id = last_id
                                self.open_task = asyncio.create_task(self.opened(command))
                        if (self.command_id and self.open_task and self.open_task.done()
                                and self.browser and (not self.browser.is_connected() or not self.context.pages)):
                            await self.request('/api/pc/status', {'command_id':self.command_id, 'phase':'closed'})
                            await self.close_browser()
                    except httpx.HTTPStatusError as error:
                        if error.response.status_code == 401:
                            self.report('서버 세션이 만료됐습니다. 웹 패널에서 새 코드로 연결하세요.')
                            return
                        if not disconnected:
                            self.report('서버에 재연결 중입니다…')
                            disconnected = True
                    except httpx.TransportError:
                        if not disconnected:
                            self.report('서버에 재연결 중입니다…')
                            disconnected = True
                    await asyncio.sleep(1)
            finally:
                await self.close_browser()
                for task in list(self.capture_tasks):
                    task.cancel()
                await asyncio.gather(*self.capture_tasks, return_exceptions=True)
                await self.playwright.stop()
                try:
                    await self.request('/api/pc/disconnect', {})
                except Exception:
                    pass


def main():
    import tkinter as tk
    from tkinter import ttk
    parser = argparse.ArgumentParser()
    parser.add_argument('--pair-code', default='')
    args = parser.parse_args()
    root = tk.Tk()
    root.title('Padlet Sender · PC 브라우저 도우미')
    root.geometry('510x290')
    root.resizable(False, False)
    frame = ttk.Frame(root, padding=24)
    frame.pack(fill='both', expand=True)
    ttk.Label(frame, text='내 PC 브라우저 연결', font=('맑은 고딕', 17, 'bold')).pack(anchor='w')
    ttk.Label(frame, text='웹 패널의 10자리 연결 코드를 입력하세요.').pack(anchor='w', pady=(16,8))
    code = tk.StringVar(value=args.pair_code)
    entry = ttk.Entry(frame, textvariable=code, font=('Consolas',18))
    entry.pack(fill='x')
    messages, stop = queue.Queue(), threading.Event()
    worker = None
    status = tk.StringVar(value='브라우저는 이 PC에서 실행됩니다. 창을 열어 두세요.')

    def run(value):
        try:
            asyncio.run(Helper(value, messages.put, stop).run())
        except httpx.HTTPStatusError:
            messages.put('연결 코드가 만료됐거나 틀립니다. 새 코드로 다시 연결하세요.')
        except Exception:
            messages.put('연결 실패. 인터넷 연결과 도우미 설치 상태를 확인하세요.')
        finally:
            messages.put(None)

    def connect():
        nonlocal worker
        value = code.get().strip().upper().replace('-', '')
        if not re.fullmatch('[0-9A-F]{10}', value):
            status.set('웹 패널에 표시된 10자리 연결 코드를 입력하세요.')
            return
        stop.clear()
        button.config(state='disabled')
        entry.config(state='disabled')
        status.set('Render 서버에 연결 중…')
        worker = threading.Thread(target=run, args=(value,), daemon=True)
        worker.start()

    button = ttk.Button(frame, text='연결', command=connect)
    button.pack(fill='x', pady=14)
    ttk.Label(frame, textvariable=status, wraplength=460).pack(anchor='w')

    def update():
        try:
            while True:
                message = messages.get_nowait()
                if message is None:
                    button.config(state='normal')
                    entry.config(state='normal')
                else:
                    status.set(message)
        except queue.Empty:
            pass
        root.after(150, update)

    def close():
        stop.set()
        status.set('브라우저와 연결을 종료하는 중…')
        if worker and worker.is_alive():
            root.after(100, close)
        else:
            root.destroy()

    root.protocol('WM_DELETE_WINDOW', close)
    root.after(150, update)
    if args.pair_code:
        root.after(200, connect)
    entry.focus()
    root.mainloop()


if __name__ == '__main__':
    main()
