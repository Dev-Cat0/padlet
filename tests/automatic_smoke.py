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
    board_pages = []
    light_pages = []
    deleted = []

    async def route(self, route):
        path = urlsplit(route.request.url).path
        if route.request.method == "DELETE":
            assert path.startswith("/api/9/wishes/post")
            headers = await route.request.all_headers()
            assert "guest=fixture" in headers.get("cookie", "")
            assert headers["x-csrf-token"] == "fixture" and headers["x-uid"] == "fixture-uid"
            deleted.append(path)
            return await route.fulfill(status=204, body="")
        if path == "/example/board":
            board_pages.append(route.request.frame.page)
            return await route.fulfill(content_type="text/html",
                headers={"Set-Cookie":"guest=fixture; Path=/; HttpOnly; Secure"},
                body="""<meta name="csrf-token" content="fixture"><h1>Large board</h1>
                    <script>
                    window.ww = {uid: 'fixture-uid'};
                    for (let i=0; i<20000; i++) {
                        const post=document.createElement('article');
                        post.textContent='Existing post '+i;
                        document.body.append(post);
                    }
                    setInterval(() => document.body.dataset.tick=performance.now(), 10);
                    </script>""")
        if path.startswith("/api/11/padlet-starting-state"):
            current = route.request.frame.page
            assert all(page.is_closed() for page in board_pages)
            assert len(current.context.pages) == 1
            assert await current.locator("article, script").count() == 0
            light_pages.append(current)
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
            post_id = len(posts)
            headers = await route.request.all_headers()
            assert "guest=fixture" in headers.get("cookie","")
            assert headers["x-csrf-token"] == "fixture" and headers["x-uid"] == "fixture-uid"
            assert route.request.frame.page is light_pages[-1]
            assert board_pages[-1].is_closed()
            if overlap:
                if len(posts) == 18:
                    all_started.set()
                await asyncio.wait_for(all_started.wait(), 3)
            if challenge:
                return await route.fulfill(status=403, headers={"cf-mitigated":"challenge"},body="Verify")
            return await route.fulfill(status=201,json={"data":{"id":post_id,
                "attributes":{"hashid":f"post{post_id}"}}})
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
            assert len(board_pages) == len(light_pages) == 3  # No second board load.
            assert all(page.is_closed() for page in light_pages)
            assert sender.status()["delete_pending"] == 18
            await sender.queue_delete()
            await sender.delete_worker
            assert sender.phase == "deleted" and sender.deleted == 18
            assert set(deleted) == {f"/api/9/wishes/post{i}" for i in range(1, 19)}
            assert len(deleted) == 18 and len(board_pages) == 3
            assert sender.browser is None and not sender.deletion_batches
            print(json.dumps({"automatic_preparation":True,"all_sections_each_round":True,
                "overlapping_rounds":True,"large_board_retired":True,"session_preserved":True,
                "emergency_deletion_restores_identity":True,
                "manual_steps":0,"challenge_stops":True,"real_posts":0,"real_deletions":0}))
        finally:
            await sender.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
