"""Read-only anonymous initialization using Padlet's public web client protocol."""
import re
import secrets
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit


def padlet_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if (parsed.scheme != "https" or parsed.hostname not in {"padlet.com", "www.padlet.com"}
            or parsed.username or parsed.password or parsed.port not in {None, 443}
            or not re.fullmatch(r"/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)?/?", parsed.path)
            or parsed.path.strip("/").split("/")[0] in {"api", "dashboard", "auth", "login"}):
        raise ValueError("https://padlet.com/사용자/게시판 형식의 게시판 주소를 입력하세요.")
    return urlunsplit(("https", parsed.hostname, parsed.path.rstrip("/"), "", ""))


class MetaParser(HTMLParser):
    token = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta" and attrs.get("name") == "csrf-token":
            self.token = attrs.get("content", "")


def check_response(response, stage):
    if response.status_code != 200:
        if response.headers.get("cf-mitigated") == "challenge":
            raise ValueError(f"{stage}: Padlet이 서버에 보안 확인을 요구합니다 (HTTP {response.status_code}). 현재 서버에서 직접 연결할 수 없습니다.")
        if response.status_code == 403:
            raise ValueError(f"{stage}: Padlet이 서버 요청을 거부했습니다 (HTTP 403). 익명 쓰기 허용과 별개로 서버 접근이 제한될 수 있습니다.")
        raise ValueError(f"{stage} 실패 (HTTP {response.status_code}). 익명 접근 권한을 확인하세요.")
    if len(response.content) > 3_000_000:
        raise ValueError("게시판 응답이 너무 큽니다.")


def build_template(state, require_write=True):
    wall, user = state.get("wall") or {}, state.get("user") or {}
    if wall.get("frozen") or require_write and (state.get("canIWrite") is not True or state.get("isReadOnly")):
        raise ValueError("이 게시판은 현재 익명 글쓰기를 허용하지 않습니다.")
    if user.get("registered_at") or user.get("username") or user.get("email"):
        raise ValueError("익명 방문자 세션을 확인할 수 없습니다.")
    if wall.get("is_archived") or wall.get("is_trashed"):
        raise ValueError("보관되었거나 삭제된 게시판입니다.")
    if not all(type(obj.get("id")) is int and obj["id"] > 0 and obj.get("hashid") for obj in (wall, user)):
        raise ValueError("게시판 응답 형식이 변경되었습니다. 연결 정보를 확인할 수 없습니다.")
    template = {
        "wall_id": wall["id"], "wall_hashid": wall["hashid"],
        "author_id": user["id"], "author_hashid": user["hashid"],
        "attachment": "", "attachment_caption": None, "width": 256,
        "published": not bool(wall.get("require_approval")),
    }
    section = (wall.get("wish_arrangement") or {}).get("default_section_id")
    if section:
        template["wall_section_id"] = section
    if wall.get("viz") == "free":
        template.update(left=16, top=16)
    return template


async def read_sections(session, origin, headers, wall_id):
    response = await session.get(origin + "/api/5/wall_sections",
                                 params={"wall_id": wall_id}, headers=headers, allow_redirects=False)
    check_response(response, "섹션 목록 조회")
    try:
        rows = response.json()["data"]
        if not isinstance(rows, list):
            raise TypeError()
        sections, seen = [], set()
        for row in rows:
            item = row["attributes"]
            section_id = item["id"]
            if type(section_id) is not int or section_id <= 0:
                raise TypeError()
            if item.get("wall_id", wall_id) != wall_id:
                raise ValueError("다른 게시판의 섹션이 반환되어 연결을 중단했습니다.")
            if section_id in seen:
                continue
            seen.add(section_id)
            sections.append({"id": section_id, "hashid": item.get("hashid"),
                             "title": str(item.get("title") or "기본 섹션")})
        return sections
    except (TypeError, KeyError, AttributeError):
        raise ValueError("섹션 목록을 확인할 수 없습니다. 게시판에 다시 연결하세요.") from None


async def prepare(session, value):
    """GET only: never create a test post to initialize a sender."""
    url = padlet_url(value)
    for _ in range(4):
        response = await session.get(url, allow_redirects=False)
        if response.status_code not in {301, 302, 303, 307, 308}:
            break
        url = padlet_url(urljoin(url, response.headers.get("location", "")))
    check_response(response, "게시판 페이지 조회")
    parser = MetaParser()
    parser.feed(response.text)
    if not parser.token:
        raise ValueError("익명 세션을 만들 수 없습니다. 로그인·보안 확인이 필요한 게시판은 지원하지 않습니다.")
    origin = "https://" + urlsplit(url).hostname
    headers = {
        "Accept": "application/json, application/vnd.api+json",
        "Content-Type": "application/json; charset=utf-8",
        "X-CSRF-Token": parser.token, "X-UID": secrets.token_hex(16),
        "Origin": origin, "Referer": url,
    }
    response = await session.get(origin + "/api/11/padlet-starting-state" + urlsplit(url).path,
                                 headers=headers, allow_redirects=False)
    check_response(response, "익명 권한 조회")
    try:
        state = response.json()
        if not isinstance(state, dict):
            raise TypeError()
        template = build_template(state)
    except (TypeError, KeyError, AttributeError):
        raise ValueError("게시판 응답 형식이 변경되었습니다. 연결 정보를 확인할 수 없습니다.") from None
    sections = await read_sections(session, origin, headers, template["wall_id"])
    from .fields import read_fields
    fields = await read_fields(session, origin, headers, template["wall_id"])
    return origin + "/api/9/wishes", headers, template, {
        "url": url, "title": str(state["wall"].get("title") or "익명 게시판"),
        "approval_required": not template["published"], "sections": sections, "fields": fields,
    }
