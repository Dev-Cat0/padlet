"""Read and validate each board's own post fields."""
import math
from datetime import datetime
from urllib.parse import urlsplit

EDITABLE_TYPES = {"text", "number", "date", "email", "url", "phone_number", "single_select"}


async def read_fields(session, origin, headers, wall_id):
    from .anonymous import check_response
    values = []
    for path in ("post-properties", "custom-post-properties"):
        response = await session.get(f"{origin}/api/1/walls/{wall_id}/{path}",
                                     headers=headers, allow_redirects=False)
        check_response(response, "게시판 입력 항목 조회")
        try:
            values.append(response.json()["data"])
        except (ValueError, TypeError, KeyError):
            raise ValueError("게시판 입력 항목을 읽을 수 없습니다. 다시 연결하세요.") from None
    try:
        core = values[0][0]["attributes"] if isinstance(values[0], list) else values[0]["attributes"]
        if not isinstance(values[1], list):
            raise TypeError()
        custom = [row["attributes"] for row in values[1]]
        if not isinstance(core, dict) or any(not isinstance(row, dict) or not row.get("id") for row in custom):
            raise TypeError()
        if not isinstance(core.get("required_fields") or {}, dict):
            raise TypeError()
        return {"subject_enabled": core.get("subject_enabled", True),
                "body_enabled": core.get("body_enabled", True),
                "attachment_enabled": core.get("attachment_settings", True) is not False,
                "required": core.get("required_fields") or {},
                "custom": [{"id": str(row["id"]), "name": str(row.get("name") or "추가 항목"),
                            "type": row.get("data_type"), "required": bool(row.get("required")),
                            "supported": row.get("data_type") in EDITABLE_TYPES,
                            "show_time": bool(row.get("show_time")),
                            "options": [{"id": str(option["id"]), "name": str(option.get("name") or ""),
                                         "default": bool(option.get("default"))}
                                        for option in row.get("selection_options") or []]}
                           for row in custom]}
    except (TypeError, KeyError, IndexError, AttributeError):
        raise ValueError("게시판 입력 항목 형식이 변경되었습니다. 다시 연결하세요.") from None


def validate_fields(fields, subject, body, attachment, custom):
    fields = fields or {}
    required = fields.get("required", {})
    subject = subject.strip() if fields.get("subject_enabled", True) else ""
    body = body if fields.get("body_enabled", True) else ""
    attachment = attachment.strip() if fields.get("attachment_enabled", True) else ""
    for key, value, name in (("subject", subject, "제목"), ("body", body, "본문"),
                             ("attachment", attachment, "첨부 링크")):
        if required.get(key) and not value.strip():
            raise ValueError(f"이 게시판에서는 {name} 입력이 필수입니다.")
    if attachment and not valid_url(attachment):
        raise ValueError("첨부 링크는 http 또는 https 주소를 입력하세요.")
    definitions = {row["id"]: row for row in fields.get("custom", [])}
    if custom.keys() - definitions.keys():
        raise ValueError("게시판에 없는 추가 항목입니다. 다시 연결하세요.")
    normalized = {}
    for key, field in definitions.items():
        value = custom.get(key)
        if key not in custom and field["type"] == "single_select":
            value = next((option["id"] for option in field["options"] if option["default"]), None)
        if not field["supported"]:
            if field["required"]:
                raise ValueError(f"‘{field['name']}’은 Padlet에서 직접 입력해야 하는 필수 항목입니다.")
            if value is not None:
                raise ValueError(f"‘{field['name']}’은 이 패널에서 지원하지 않는 항목입니다.")
            continue
        if value is None or isinstance(value, str) and not value.strip():
            if field["required"]:
                raise ValueError(f"‘{field['name']}’ 항목을 입력하세요.")
            continue
        kind = field["type"]
        valid = isinstance(value, str) and len(value) <= 20000
        if kind == "number":
            valid = type(value) in {int, float} and math.isfinite(value)
        elif kind == "single_select":
            valid = isinstance(value, str) and value in {option["id"] for option in field["options"]}
        elif kind == "url":
            valid = valid and valid_url(value)
        elif kind == "email":
            valid = valid and "@" in value and not any(char.isspace() for char in value)
        elif kind == "date" and valid:
            try:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                valid = False
        if not valid:
            raise ValueError(f"‘{field['name']}’ 항목의 입력 형식을 확인하세요.")
        normalized[key] = value
    if not subject and not body.strip() and not attachment and not normalized:
        raise ValueError("제목·본문·첨부·추가 항목 중 하나 이상 입력하세요.")
    return subject, body, {"attachment": attachment, "custom_properties": normalized}


def valid_url(value):
    try:
        url = urlsplit(value)
        return url.scheme in {"https", "http"} and bool(url.hostname) and not url.username and not url.password
    except ValueError:
        return False
