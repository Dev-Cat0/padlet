"""Runs against a throwaway CI database, never the production license ledger."""
import os
import secrets
from concurrent.futures import ThreadPoolExecutor

import pytest

from server.license_store import Store, OWNER_ID, LicenseError


@pytest.mark.skipif(not os.getenv('TEST_KEY_DATABASE_URL'), reason='CI PostgreSQL service required')
def test_postgres_transactions_durable_balance_and_audit():
    store = Store(os.environ['TEST_KEY_DATABASE_URL'], 'ci-only-pepper-' * 3)
    try:
        item = store.prepare(OWNER_ID, store.grant(OWNER_ID, OWNER_ID, 'uses', 1))
        store.delivered(item['id'])
        def attempt(_):
            try:
                store.consume(item['id'], secrets.token_hex(16))
                return 1
            except LicenseError:
                return 0
        with ThreadPoolExecutor(max_workers=8) as workers:
            assert sum(workers.map(attempt, range(16))) == 1
        with store.db() as db:
            row = db.execute('SELECT remaining FROM licenses WHERE id=?', (item['id'],)).fetchone()
            assert row['remaining'] == 0
            assert db.execute("SELECT COUNT(*) AS count FROM audit_events WHERE kind='used'").fetchone()['count'] >= 1
        store.ticket(1562751204935602258, OWNER_ID, 1562751204935602258)
        assert store.ticket(1562751204935602258, OWNER_ID) == 1562751204935602258
        with pytest.raises(LicenseError):
            store.verify_key(item['key'], secrets.token_hex(16))
        nonce = secrets.token_hex(16)
        store.accept_nonce(nonce, 123)
        with pytest.raises(LicenseError):
            store.accept_nonce(nonce, 123)
    finally:
        store.close()
