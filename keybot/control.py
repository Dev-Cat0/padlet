"""Detached Windows launcher and cooperative shutdown; no token in process arguments."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .instance import single_instance

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / '.local' / 'keybot'


def read_status():
    try:
        status = json.loads((DATA / 'status.json').read_text(encoding='utf-8'))
        return status if time.time() - status['heartbeat'] < 10 else {}
    except (OSError, ValueError, KeyError):
        return {}


def is_running():
    DATA.mkdir(parents=True, exist_ok=True)
    try:
        with single_instance(DATA / 'agent.lock'):
            return False
    except SystemExit:
        return True


def start():
    if is_running():
        print('봇이 이미 실행 중입니다. 중복 실행하지 않습니다.')
        return
    options = {'creationflags': subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
    process = subprocess.Popen([sys.executable, '-m', 'keybot'], cwd=ROOT,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, close_fds=True, **options)
    for _ in range(40):
        if process.poll() is not None:
            raise SystemExit('봇 시작 실패. .local/keybot/bot.log에서 오류를 확인하세요.')
        status = read_status()
        if status.get('pid') == process.pid and is_running():
            print('백그라운드 실행을 시작했습니다. 이제 CMD 창을 닫아도 됩니다.')
            print('연결 확인: status-keybot.cmd / 종료: stop-keybot.cmd')
            return
        time.sleep(.1)
    print('봇 연결 준비 중입니다. status-keybot.cmd로 상태를 확인하세요.')


def stop():
    if not is_running():
        print('봇이 실행 중이지 않습니다.')
        return
    status = read_status()
    if not status.get('run_id'):
        raise SystemExit('이전 버전의 봇은 실행 중인 CMD 창에서 Ctrl+C로 종료하세요.')
    (DATA / 'stop.json').write_text(json.dumps({'run_id': status['run_id']}), encoding='utf-8')
    for _ in range(100):
        if not is_running():
            print('봇을 종료했습니다. 발급 기록은 유지됩니다.')
            return
        time.sleep(.1)
    raise SystemExit('종료 요청을 보냈습니다. status-keybot.cmd로 확인하세요.')


def show_status():
    if not is_running():
        print('봇: 중지됨')
        return
    status = read_status()
    print('봇: 실행 중')
    print('Discord: ' + ('연결됨' if status.get('discord') else '연결 대기 중'))
    print('웹 인증 서버: ' + ('연결됨' if status.get('server') else '연결 대기 중'))
    print('로그: .local/keybot/bot.log')


if __name__ == '__main__':
    action = sys.argv[1] if len(sys.argv) > 1 else 'status'
    if action not in {'start', 'stop', 'status'}:
        raise SystemExit('사용법: python -m keybot.control start|stop|status')
    {'start': start, 'stop': stop, 'status': show_status}[action]()
