"""Real Chromium, mocked Padlet: no user interaction or warm-up posts."""
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.browser import BrowserSession
from server.engine import Sender


async def main():
    posts = []
    challenge = False
    overlap = False
    all_started = asyncio.Event()

    async def route(self, route):
        path = urlsplit(route.request.url).path
        if path == "/example/board":
            return await route.fulfill(content_type="text/html",
                headers={"Set-Cookie":"guest=fixture; Path=/; HttpOnly; Secure"},
                body='<meta name="csrf-token" content="fixture"><h1>Anonymous board</h1>')
        if path.startswith("/api/11/padlet-starting-state"):
            return await route.fulfill(json={"canIWrite":True,
                "wall":{"id":22,"hashid":"board","wish_arrangement":{"default_section_id":2}},
                "user":{"id":31,"hashid":"guest","registered_at":None}})
        if path == "/api/5/wall_sections":
            return await route.fulfill(json={"data":[{"attributes":{"id":i,"wall_id":22,"title":str(i)}} for i in [1,2,3]]})
        if path.endswith("/post-properties"):
            return await route.fulfill(json={"data":{"attributes":{"required_fields":{"body":True}}}})
        if path.endswith("/custom-post-properties"):
            return await route.fulfill(json={"data":[]})
        if path == "/api/9/wishes":
            posts.append(route.request.post_data_json)
            assert "guest=fixture" in (await route.request.all_headers()).get("cookie","")
            if overlap:
                if len(posts) == 18:
                    all_started.set()
                await asyncio.wait_for(all_started.wait(), 3)
            if challenge:
                return await route.fulfill(status=403, headers={"cf-mitigated":"challenge"},body="Verify")
            return await route.fulfill(status=201,json={"data":{"id":len(posts)}})
        await route.abort()

    sender = Sender()
    with patch.object(BrowserSession, "route", route):
        try:
            await sender.queue_run("https://padlet.com/example/board", "Title", "Body", 2, 0)
            await sender.worker
            assert sender.phase == "finished" and sender.sent == 6 and len(posts) == 6
            assert all(p["author_id"] == 31 for p in posts)
            assert sorted(p["wall_section_id"] for p in posts[:3]) == [1, 2, 3]
            assert sorted(p["wall_section_id"] for p in posts[3:]) == [1, 2, 3]
            assert sender.browser is None and sender.http is None
            overlap = True
            await sender.queue_run("https://padlet.com/example/board", "Title", "Body", 4, .00001)
            await sender.worker
            assert all_started.is_set() and sender.sent == 12 and sender.phase == "finished"
            assert all(sum(p["wall_section_id"] == i for p in posts[6:]) == 4 for i in [1, 2, 3])
            overlap = False
            challenge = True
            await sender.queue_run("https://padlet.com/example/board", "Title", "Body", 4, 0)
            await sender.worker
            assert sender.phase == "error" and sender.sent == 0 and 19 <= len(posts) <= 30
            assert sender.browser is None
            print(json.dumps({"automatic_preparation":True,"all_sections_each_round":True,
                "overlapping_rounds":True,"manual_steps":0,"challenge_stops":True,"real_posts":0}))
        finally:
            await sender.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
