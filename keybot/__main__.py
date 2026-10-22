import asyncio
import json
import logging
from contextlib import suppress
from pathlib import Path

from .bot import Bot
from .bridge import Bridge
from .store import Store
from .instance import single_instance

ROOT = Path(__file__).resolve().parents[1]


async def run(config, path):
    store = Store(path.parent / "licenses.sqlite3", config["KEY_HASH_SECRET"])
    store.delivery_failed()
    bridge = Bridge(config["SERVER_URL"], config["KEY_AGENT_SECRET"], store)
    bot = Bot(store, bridge, int(config["DISCORD_GUILD_ID"]))
    task = asyncio.create_task(bridge.run())
    try:
        async with bot:
            await bot.start(config["DISCORD_BOT_TOKEN"])
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def main():
    path = ROOT / ".local" / "keybot" / "config.json"
    if not path.exists():
        raise SystemExit("먼저 python -m keybot.setup 을 실행하세요.")
    config = json.loads(path.read_text(encoding="utf-8"))
    with single_instance(path.parent / "agent.lock"):
        await run(config, path)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # Do not dump Discord HTTP responses (DMs and interactions can contain keys).
    logging.getLogger("discord").setLevel(logging.CRITICAL)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as error:
        raise SystemExit(f"봇 시작 실패 ({type(error).__name__}). 토큰·서버 ID·설정을 확인하세요.") from None
