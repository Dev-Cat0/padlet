"""One outbound WSS connection; no local listener or inbound port required."""
import asyncio
import logging

import aiohttp

from .store import LicenseError, RateLimited

log = logging.getLogger(__name__)


class Bridge:
    def __init__(self, url, secret, store):
        if not url.startswith("https://") or len(secret) < 32:
            raise ValueError("HTTPS SERVER_URL and a 32+ character KEY_AGENT_SECRET are required")
        self.url = "wss://" + url.removeprefix("https://").rstrip("/") + "/internal/key-agent"
        self.secret, self.store = secret, store
        self.socket = None

    async def run(self):
        delay = 2
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as client:
            while True:
                try:
                    async with client.ws_connect(self.url, headers={"Authorization": "Bearer " + self.secret},
                                                 heartbeat=25, max_msg_size=8192) as ws:
                        self.socket, delay = ws, 2
                        log.info("Web authorization bridge connected")
                        async for packet in ws:
                            if packet.type != aiohttp.WSMsgType.TEXT:
                                continue
                            msg = packet.json()
                            if not isinstance(msg, dict):
                                continue
                            rpc_id = msg.get("id")
                            action, license_id = msg.get("action"), msg.get("license_id")
                            if action not in {"check", "consume", "verify_key"} or not isinstance(license_id, str):
                                continue
                            try:
                                if action == "verify_key":
                                    key, client_id = msg.get("key"), msg.get("client_id")
                                    if not isinstance(key, str) or len(key) != 5 or not isinstance(client_id, str) or len(client_id) != 64:
                                        raise LicenseError("Invalid request")
                                    result = await asyncio.to_thread(self.store.verify_key, key, client_id)
                                    msg.clear()
                                elif action == "consume":
                                    request_id = msg.get("request_id", "")
                                    if not isinstance(request_id, str) or not 16 <= len(request_id) <= 160:
                                        raise LicenseError("Invalid request")
                                    result = await asyncio.to_thread(self.store.consume, license_id, request_id)
                                else:
                                    result = await asyncio.to_thread(self.store.check, license_id)
                                await ws.send_json({"id": rpc_id, "ok": True, **result})
                            except LicenseError as error:
                                await ws.send_json({"id": rpc_id, "ok": False, "limited": isinstance(error, RateLimited)})
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    # Never log exception text, headers, keys, interaction payloads, or tokens.
                    log.warning("Web bridge unavailable (%s); reconnecting", type(error).__name__)
                finally:
                    self.socket = None
                await asyncio.sleep(delay)
                delay = min(60, delay * 2)
