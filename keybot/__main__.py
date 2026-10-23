import asyncio
import json
import logging
import os
import secrets
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .bot import Bot
from .client import RemoteStore
from .instance import single_instance
from .setup import load_config

ROOT = Path(__file__).resolve().parents[1]


async def run(config, path):
    store = RemoteStore(config["SERVER_URL"], config["KEY_ADMIN_SECRET"])
    bot = Bot(store, None, int(config["DISCORD_GUILD_ID"]))
    run_id = secrets.token_hex(16)

    async def monitor():
        while True:
            status = {"pid": os.getpid(), "run_id": run_id, "heartbeat": time.time(),
                      "discord": bot.is_ready(), "server": store.connected}
            temporary = path.parent / 'status.tmp'
            try:
                temporary.write_text(json.dumps(status), encoding='utf-8')
                temporary.replace(path.parent / 'status.json')
            except OSError:
                pass  # A Windows reader can briefly hold the status file; retry next heartbeat.
            try:
                stop = json.loads((path.parent / 'stop.json').read_text(encoding='utf-8'))
                if stop.get('run_id') == run_id:
                    return
            except (OSError, ValueError):
                pass
            await asyncio.sleep(1)

    try:
        async with bot:
            login = asyncio.create_task(bot.start(config["DISCORD_BOT_TOKEN"]))
            watcher = asyncio.create_task(monitor())
            try:
                done, _ = await asyncio.wait({login, watcher}, return_when=asyncio.FIRST_COMPLETED)
                for finished in done:
                    finished.result()
            finally:
                login.cancel()
                watcher.cancel()
                await asyncio.gather(login, watcher, return_exceptions=True)
    finally:
        logging.getLogger(__name__).info('로컬 봇 종료. 사이트 키 검증은 서버에서 계속됩니다.')


async def main():
    path = ROOT / ".local" / "keybot" / "config.json"
    if not path.exists():
        raise SystemExit("먼저 python -m keybot.setup 을 실행하세요.")
    _, _, config = load_config()
    with single_instance(path.parent / "agent.lock"):
        await run(config, path)


if __name__ == "__main__":
    data = ROOT / '.local' / 'keybot'
    data.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[RotatingFileHandler(data / 'bot.log', maxBytes=512_000,
                                                      backupCount=2, encoding='utf-8')])
    # Do not dump Discord HTTP responses (DMs and interactions can contain keys).
    logging.getLogger("discord").setLevel(logging.CRITICAL)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as error:
        logging.getLogger(__name__).error("봇 시작 실패 (%s). 토큰·서버 ID·설정을 확인하세요.", type(error).__name__)
        raise SystemExit(f"봇 시작 실패 ({type(error).__name__}). 토큰·서버 ID·설정을 확인하세요.") from None
