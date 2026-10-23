import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


def test_detached_bot_survives_launcher_and_stops_without_killing_pid(tmp_path):
    """Exercise the actual launcher using a stub agent with no Discord/network access."""
    root = Path(__file__).resolve().parents[1]
    package = tmp_path / 'keybot'
    package.mkdir()
    for name in ('__init__.py', 'control.py', 'instance.py'):
        shutil.copyfile(root / 'keybot' / name, package / name)
    (package / '__main__.py').write_text('''import json, os, time
from pathlib import Path
from .instance import single_instance
p=Path('.local/keybot');p.mkdir(parents=True,exist_ok=True)
with single_instance(p/'agent.lock'):
    while True:
        (p/'status.tmp').write_text(json.dumps({'pid':os.getpid(),'heartbeat':time.time(),'run_id':'stub','discord':True,'server':True}))
        try:(p/'status.tmp').replace(p/'status.json')
        except OSError:pass
        if (p/'stop.json').exists():break
        time.sleep(.1)
''', encoding='utf-8')
    env = {**os.environ, 'PYTHONUTF8': '1'}
    env.pop('PYTHONPATH', None)
    def call(action):
        return subprocess.run([sys.executable, '-m', 'keybot.control', action], cwd=tmp_path,
                              capture_output=True, text=True, encoding='utf-8', env=env, timeout=15)
    try:
        assert call('start').returncode == 0
        # The launcher exited, but its detached child continues advancing its heartbeat.
        first = json.loads((tmp_path / '.local/keybot/status.json').read_text())
        time.sleep(.3)
        later = json.loads((tmp_path / '.local/keybot/status.json').read_text())
        assert later['heartbeat'] > first['heartbeat']
        assert '실행 중' in call('status').stdout
        assert '이미 실행' in call('start').stdout
    finally:
        assert call('stop').returncode == 0
    assert '중지됨' in call('status').stdout
