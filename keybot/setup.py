"""Interactive setup: secrets never appear in command arguments or version control."""
import getpass
import json
import os
import secrets
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1] / ".local" / "keybot"
    root.mkdir(parents=True, exist_ok=True)
    path = root / "config.json"
    config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
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
    config.setdefault("KEY_HASH_SECRET", secrets.token_urlsafe(48))
    config.setdefault("KEY_AGENT_SECRET", secrets.token_urlsafe(48))
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(config, indent=2), encoding="utf-8")
    os.chmod(temp, 0o600)
    temp.replace(path)
    # Operator-only handoff file: keep the shared credential out of terminal history.
    (root / "server-secret.txt").write_text("KEY_AGENT_SECRET=" + config["KEY_AGENT_SECRET"] + "\n", encoding="utf-8")
    print("설정 저장 완료. .local/keybot/server-secret.txt의 값을 기존 웹 서버 환경 변수에 등록하세요.")
    print("그 다음 python -m keybot 으로 실행하세요. .local/keybot 폴더를 비공개로 백업하세요.")


if __name__ == "__main__":
    main()
