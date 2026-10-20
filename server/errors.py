"""Bounded, credential-free diagnostics for rejected Padlet requests."""
import re


def rejection_detail(response, secrets=()):
    # Never include a complete response, HTML error page, or echoed request.
    raw = getattr(response, "text", "")
    if not isinstance(raw, str) or len(raw) > 65536:
        return ""
    if re.search(r"InvalidAuthenticityToken|invalid csrf|csrf token|can't verify csrf", raw, re.I):
        return "익명 세션 인증이 만료되었거나 일치하지 않습니다. 게시판에 다시 연결하세요."
    try:
        data = response.json()
    except (ValueError, AttributeError):
        return ""
    messages = []

    def collect(value, depth=0):
        if depth > 4 or len(messages) >= 5:
            return
        if isinstance(value, str):
            messages.append(value)
        elif isinstance(value, list):
            for item in value[:5]:
                collect(item, depth + 1)
        elif isinstance(value, dict):
            # Padlet reasons and JSON:API errors both use these message keys.
            field = value.get("field")
            start = len(messages)
            for key in ("detail", "message", "title", "code"):
                if isinstance(value.get(key), str):
                    collect(value[key], depth + 1)
                    break
            if isinstance(field, str) and len(messages) > start:
                messages[start] = f"{field}: {messages[start]}"
            for key in ("reasons", "errors", "error"):
                if key in value:
                    collect(value[key], depth + 1)
            for key in ("subject", "body", "attachment", "custom_properties", "wall_section_id",
                        "wall_id", "author_id", "published", "base"):
                if key in value:
                    start = len(messages)
                    collect(value[key], depth + 1)
                    if len(messages) > start:
                        messages[start] = f"{key}: {messages[start]}"

    collect(data)
    detail = " · ".join(dict.fromkeys(messages))
    for secret in sorted((s for s in secrets if isinstance(s, str) and s), key=len, reverse=True):
        detail = detail.replace(secret, "[비공개]")
    detail = re.sub(r"(?i)(bearer\s+|(?:token|cookie|authorization|session)\s*[:=]\s*)[^\s,;]+",
                    r"\1[비공개]", detail)
    detail = re.sub(r"[A-Za-z0-9_+/=-]{40,}", "[비공개]", detail)
    detail = re.sub(r"<[^>]*>", "", detail)
    return " ".join(detail.split())[:500]


def rejection_message(response, secrets=()):
    status = response.status_code
    detail = rejection_detail(response, secrets)
    if status == 422:
        message = "Padlet이 게시 내용을 거부했습니다 (HTTP 422)."
        fallback = "게시판의 필수 항목·첨부 요구 조건을 확인하세요. 서버가 구체적인 사유를 제공하지 않았습니다."
    elif status in {401, 403}:
        message = f"Padlet 인증·권한 오류 (HTTP {status})."
        fallback = "익명 쓰기 권한을 확인하고 게시판에 다시 연결하세요."
    else:
        message = f"Padlet 요청 실패 (HTTP {status})."
        fallback = "게시판 권한·섹션 목록을 확인하고 다시 연결하세요."
    return f"{message} {detail or fallback} 전송을 중지했습니다."
