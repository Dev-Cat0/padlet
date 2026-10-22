import secrets
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def licensed_session(monkeypatch):
    """Only tests may inject a license; production has no unauthenticated session endpoint."""
    from server.app import key_access, sessions
    monkeypatch.setattr(key_access, "rpc", AsyncMock(return_value={"ok": True, "expires": None}))
    return lambda: sessions.create(license_id=secrets.token_urlsafe(24))
