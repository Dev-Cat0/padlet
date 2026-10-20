"""Anonymous launcher: all requests are local fixtures."""
import json
from pathlib import Path
from urllib.parse import urlsplit, unquote
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1366, "height": 1000})
        errors, urls = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        def route(route):
            parsed = urlsplit(route.request.url)
            urls.append(route.request.url)
            assert parsed.hostname == "127.0.0.1"
            name = "index.html" if parsed.path == "/" else parsed.path.lstrip("/")
            assert name in {"index.html", "app.js", "anonymous.js", "style.css"}
            mime = "text/html" if name.endswith("html") else "text/css" if name.endswith("css") else "application/javascript"
            route.fulfill(content_type=mime, body=(ROOT / "web" / name).read_text(encoding="utf-8"))
        page.route("**/*", route)
        page.goto("http://127.0.0.1:4173/")
        page.wait_for_function("() => document.getElementById('bookmark').hasAttribute('href')")
        href = page.locator("#bookmark").get_attribute("href")
        assert href.startswith("javascript:") and "anonymousState" in unquote(href)
        assert page.locator("#code").input_value() == href
        page.locator("#bookmark").click()
        assert "시크릿" in page.locator("#notice").inner_text()
        assert page.locator("#padlet-anonymous-sender").count() == 0
        if (ROOT / ".local").is_dir():
            page.screenshot(path=str(ROOT / ".local/anonymous-launcher.png"), full_page=True)
        page.set_viewport_size({"width":390,"height":844})
        assert page.evaluate("document.documentElement.scrollWidth") <= 390
        assert not errors, errors
        assert not any("onrender" in url or "/api/" in url for url in urls)
        print(json.dumps({"launcher":"passed","bookmarklet_bytes":len(href),"server_requests":0}))
        browser.close()


if __name__ == "__main__":
    main()
