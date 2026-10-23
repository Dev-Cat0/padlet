"""Interactive setup: secrets never appear in command arguments or version control."""
import getpass
import json
import os
import secrets
import sys
from pathlib import Path


def load_config():
    root = Path(__file__).resolve().parents[1] / ".local" / "keybot"
    root.mkdir(parents=True, exist_ok=True)
    path = root / "config.json"
    config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    connection = root / 'connection.json'
    if connection.exists():
        provided = json.loads(connection.read_text(encoding='utf-8'))
        for name in ('SERVER_URL', 'KEY_ADMIN_SECRET'):
            if provided.get(name):
                config[name] = provided[name]
    return root, path, config


def save_config(path, config):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(config, indent=2), encoding='utf-8')
    os.chmod(temp, 0o600)
    temp.replace(path)


def main():
    root, path, config = load_config()
    if '--connection-only' in sys.argv:
        save_config(path, config)
        return
    print("노출된 토큰은 Discord Developer Portal에서 Reset Token 후 새 토큰을 사용하세요.")
    token = getpass.getpass("새 봇 토큰 (기존 값을 유지하려면 Enter): ").strip()
    guild = input("Discord 서버 ID: ").strip()
    if token:
        config["DISCORD_BOT_TOKEN"] = token
    if guild:
        config["DISCORD_GUILD_ID"] = str(int(guild))
    if not config.get("DISCORD_BOT_TOKEN") or not config.get("DISCORD_GUILD_ID"):
        raise SystemExit("봇 토큰과 서버 ID가 필요합니다.")
    config.setdefault("SERVER_URL", "https://padlet-sender-panel.onrender.com")
    if not config.get('KEY_ADMIN_SECRET'):
        config['KEY_ADMIN_SECRET'] = getpass.getpass('서버 관리 연결 비밀값 (관리자 배포 파일에서 제공): ').strip()
    if len(config['KEY_ADMIN_SECRET']) < 32:
        raise SystemExit('새 봇 업데이트 파일의 서버 연결 설정이 필요합니다.')
    save_config(path, config)
    print("설정 저장 완료. start-keybot.cmd로 백그라운드 실행하세요.")


if __name__ == "__main__":
    main()
