"""Anonymous comments using the same guest context as the post sender."""
import asyncio
import html
import math
import time

from pydantic import BaseModel, Field, model_validator

from .anonymous import check_response

MAX_COMMENT_POSTS = 10000


class CommentOptions(BaseModel):
    enabled: bool = False
    body: str = Field(default="", max_length=2000)
    count: int = Field(default=1, ge=1, le=1000)
    delay: float = Field(default=1, ge=0, le=86400, allow_inf_nan=False)

    @model_validator(mode="after")
    def require_text(self):
        if self.enabled and not self.body.strip():
            raise ValueError("댓글 내용을 입력하세요.")
        return self


def positive_id(value):
    if type(value) is int and 0 < value < 10**20:
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit() and 0 < len(value) <= 20:
        number = int(value)
        return number if number > 0 else None
    return None


def post_id(data, wall_id):
    resource = data.get("data") if isinstance(data, dict) else None
    attrs = resource.get("attributes") if isinstance(resource, dict) else None
    if not isinstance(attrs, dict) or data.get("errors") or data.get("error"):
        return None
    if attrs.get("wall_id") is not None and positive_id(attrs["wall_id"]) != wall_id:
        return None
    return positive_id(attrs.get("id")) or positive_id(resource.get("id"))


async def read_posts(http, template, stopped):
    """Snapshot existing targets before posting; never follow response-supplied URLs."""
    posts, seen, cursors = [], set(), set()
    cursor = ""
    for _ in range(200):
        if stopped():
            return []
        response = await http.get(http.origin + "/api/9/wishes", params={
            "wall_hashid": template["wall_hashid"], "page_start": cursor})
        check_response(response, "댓글 대상 게시글 조회")
        data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise ValueError("댓글 대상 게시글 목록 형식이 변경되었습니다.")
        for row in data["data"]:
            identity = post_id({"data": row}, template["wall_id"])
            if identity is None:
                raise ValueError("댓글 대상 게시글의 번호 또는 게시판을 확인하지 못했습니다.")
            if identity not in seen:
                seen.add(identity)
                posts.append(identity)
            if len(posts) > MAX_COMMENT_POSTS:
                raise ValueError("댓글 대상은 한 실행에 최대 10,000개 게시글까지 지원합니다.")
        meta = data.get("meta") or {}
        if not isinstance(meta, dict):
            raise ValueError("댓글 대상 게시글의 페이지 정보를 확인하지 못했습니다.")
        cursor = meta.get("next")
        if cursor is None or cursor == "":
            return posts
        if not isinstance(cursor, (str, int)) or isinstance(cursor, bool) or len(str(cursor)) > 2000:
            raise ValueError("댓글 대상 게시글의 페이지 정보를 확인하지 못했습니다.")
        cursor = str(cursor)
        if cursor in cursors:
            raise ValueError("게시글 목록의 페이지가 반복되어 중단했습니다.")
        cursors.add(cursor)
    raise ValueError("댓글 대상 게시글의 페이지 수가 지원 범위를 초과했습니다.")


class CommentSender:
    def __init__(self, sender, options):
        self.sender, self.options = sender, options
        self.lock = asyncio.Lock()
        self.seen = set()
        self.next_start = self.backoff_until = 0.0
        self.failed = False

    def stopped(self):
        if self.sender.license_expires is not None and time.time() >= self.sender.license_expires:
            self.sender.stop_event.set()
        return self.sender.stop_event.is_set()

    async def ready(self):
        remaining = self.backoff_until - time.monotonic()
        if remaining > 0:
            await self.sender.wait(remaining)
        return not self.stopped()

    def fail(self, message, ambiguous=False):
        self.failed = True
        self.sender.comments_errors += 1
        self.sender.errors += 1
        if ambiguous:
            self.sender.untracked += 1
        self.sender.log(message, "error")
        self.sender.stop_event.set()

    async def send(self, identity):
        async with self.lock:
            if self.stopped():
                return
            if identity is None:
                self.fail("새 게시글 번호를 확인하지 못해 댓글 전송을 중단했습니다.")
                return
            if identity in self.seen:
                return
            if len(self.seen) >= MAX_COMMENT_POSTS:
                self.fail("댓글 대상 10,000개 게시글에 도달하여 실행을 중단했습니다.")
                return
            self.seen.add(identity)
            self.sender.comments_targets += 1
            body = "<p>" + html.escape(self.options.body).replace("\n", "<br>") + "</p>"
            payload = {"attributes": {"wish_id": identity, "html_body": body,
                "user_id": self.sender.template["author_id"],
                "user_hashid": self.sender.template["author_hashid"]}}
            for _ in range(self.options.count):
                retries = 0
                while not self.stopped():
                    remaining = max(self.next_start, self.backoff_until) - time.monotonic()
                    if remaining > 0 and await self.sender.wait(remaining):
                        return
                    if self.stopped():
                        return
                    self.sender.phase = "commenting"
                    self.next_start = time.monotonic() + self.options.delay
                    try:
                        response = await self.sender.http.post(self.sender.http.origin + "/api/8/comments",
                                                               json=payload, timeout=10)
                    except Exception:
                        self.fail("댓글 응답이 끊겨 중단했습니다. 중복 방지를 위해 자동 재전송하지 않습니다.", True)
                        return
                    challenge = response.headers.get("cf-mitigated") == "challenge"
                    if response.status_code == 429 and not challenge:
                        retries += 1
                        if retries > 5:
                            self.fail("댓글 요청 제한이 반복되어 중단했습니다. 나중에 다시 실행하세요.")
                            return
                        try:
                            delay = float(response.headers.get("Retry-After") or 30)
                            delay = max(1, delay) if math.isfinite(delay) else 30
                        except (ValueError, TypeError):
                            delay = 30
                        self.backoff_until = time.monotonic() + delay
                        self.sender.phase = "backoff"
                        self.sender.log(f"댓글 요청 제한 · {delay:g}초 대기", "warning")
                        continue
                    if response.status_code not in {200, 201} or challenge:
                        self.fail("익명 댓글 권한 또는 보안 확인이 필요하여 중단했습니다." if
                                  challenge or response.status_code in {401, 403} else
                                  f"댓글 작성 실패 (HTTP {response.status_code}). 실행을 중단했습니다.")
                        return
                    try:
                        data = response.json()
                        attrs = data["data"]["attributes"]
                        comment_id = positive_id(attrs.get("id")) or positive_id(data["data"].get("id"))
                        valid = comment_id and positive_id(attrs.get("wish_id")) == identity and not data.get("errors") and not data.get("error")
                    except (ValueError, KeyError, TypeError, AttributeError):
                        valid = False
                    if not valid:
                        self.fail("댓글 성공 응답을 확인하지 못해 중단했습니다. 마지막 댓글 작성 여부를 확인하세요.", True)
                        return
                    self.sender.deletion_batch().setdefault("comments", set()).add(comment_id)
                    self.sender.comments_sent += 1
                    self.sender.log(f"댓글 작성 성공 #{self.sender.comments_sent}", "success")
                    self.backoff_until = 0
                    break
