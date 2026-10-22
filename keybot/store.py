"""Durable local grants and licenses; plaintext keys are never stored."""
import calendar
import hashlib
import hmac
import secrets
import sqlite3
import string
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

OWNER_ID = 481652220618604544
KST = timezone(timedelta(hours=9))
KINDS = {"uses", "days", "month", "year", "forever"}


class LicenseError(ValueError):
    pass


class RateLimited(LicenseError):
    pass


def expiry(kind, amount, now):
    start = datetime.fromtimestamp(now, KST)
    if kind == "days":
        return (start + timedelta(days=amount)).timestamp()
    if kind in {"month", "year"}:
        month_index = start.year * 12 + start.month - 1 + (amount if kind == "month" else amount * 12)
        year, month = divmod(month_index, 12)
        month += 1
        return start.replace(year=year, month=month,
                             day=min(start.day, calendar.monthrange(year, month)[1])).timestamp()
    return None


class Store:
    def __init__(self, path, pepper):
        if len(pepper) < 32:
            raise ValueError("KEY_HASH_SECRET must contain at least 32 characters")
        self.path, self.pepper = str(path), pepper.encode()
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS grants (
                    id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, kind TEXT NOT NULL,
                    amount INTEGER NOT NULL, remaining INTEGER NOT NULL, created REAL NOT NULL,
                    revoked INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS licenses (
                    id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, user_id TEXT NOT NULL,
                    grant_id INTEGER NOT NULL, remaining INTEGER, expires REAL,
                    state TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS charges (
                    request_id TEXT PRIMARY KEY, license_id TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts (
                    bucket TEXT PRIMARY KEY, start REAL NOT NULL, count INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS tickets (
                    guild_id TEXT NOT NULL, user_id TEXT NOT NULL, channel_id INTEGER NOT NULL,
                    PRIMARY KEY(guild_id, user_id));
                CREATE INDEX IF NOT EXISTS grant_owner ON grants(user_id, remaining);
            """)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def digest(self, key):
        return hmac.new(self.pepper, key.strip().upper().encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def require_owner(actor):
        if int(actor) != OWNER_ID:
            raise LicenseError("관리자만 사용할 수 있습니다.")

    def grant(self, actor, user, kind, amount, quantity=1):
        self.require_owner(actor)
        if kind not in KINDS or not 1 <= amount <= 36500 or not 1 <= quantity <= 100:
            raise LicenseError("발급 설정을 확인하세요.")
        if kind in {"month", "year", "forever"} and amount != 1:
            raise LicenseError("달·년·영구 발급 단위는 1입니다.")
        with self.db() as db:
            return db.execute("INSERT INTO grants(user_id,kind,amount,remaining,created) VALUES(?,?,?,?,?)",
                              (str(user), kind, amount, quantity, time.time())).lastrowid

    def available(self, user):
        with self.db() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM grants WHERE user_id=? AND remaining>0 AND revoked=0 ORDER BY id LIMIT 25", (str(user),))]

    def prepare(self, user, grant_id):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM grants WHERE id=? AND user_id=? AND remaining>0 AND revoked=0",
                             (grant_id, str(user))).fetchone()
            if not row:
                raise LicenseError("사용할 수 있는 발급권이 없습니다.")
            for _ in range(100):
                key = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(5))
                if not any(c.isalpha() for c in key) or not any(c.isdigit() for c in key):
                    continue
                if not db.execute("SELECT 1 FROM licenses WHERE digest=?", (self.digest(key),)).fetchone():
                    break
            else:
                raise LicenseError("키를 생성하지 못했습니다. 다시 시도하세요.")
            now, license_id = time.time(), secrets.token_urlsafe(24)
            until = expiry(row["kind"], row["amount"], now)
            remaining = row["amount"] if row["kind"] == "uses" else None
            db.execute("INSERT INTO licenses VALUES(?,?,?,?,?,?,?,?)",
                       (license_id, self.digest(key), str(user), grant_id, remaining, until, "pending", now))
            db.execute("UPDATE grants SET remaining=remaining-1 WHERE id=?", (grant_id,))
            return {"key": key, "id": license_id, "remaining": remaining, "expires": until}

    def delivered(self, license_id):
        with self.db() as db:
            db.execute("UPDATE licenses SET state='active' WHERE id=? AND state='pending'", (license_id,))

    def delivery_failed(self, license_id=None):
        # Also recover interrupted deliveries at startup. A sent-but-uncommitted DM key is revoked.
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            query = "SELECT id,grant_id FROM licenses WHERE state='pending'"
            rows = db.execute(query + (" AND id=?" if license_id else ""),
                              (license_id,) if license_id else ()).fetchall()
            for row in rows:
                db.execute("UPDATE licenses SET state='revoked' WHERE id=?", (row["id"],))
                db.execute("UPDATE grants SET remaining=remaining+1 WHERE id=? AND revoked=0", (row["grant_id"],))

    @staticmethod
    def valid(row, now):
        if (not row or row["state"] != "active" or
                row["expires"] is not None and now >= row["expires"] or
                row["remaining"] is not None and row["remaining"] <= 0):
            raise LicenseError("사용할 수 없는 키입니다. 키·소유자·기한·잔여 횟수를 확인하세요.")

    def verify(self, user, key):
        return self._verify("user:" + str(user), key, user)

    def verify_key(self, key, client_id):
        return self._verify("web:" + client_id, key)

    def _verify(self, bucket_id, key, user=None):
        # Persist limits so a restart cannot reset guessing attempts. Owner binding adds another gate.
        now = time.time()
        limited = False
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            for bucket, seconds, maximum in [(bucket_id, 600, 5), ("global", 60, 60)]:
                row = db.execute("SELECT * FROM attempts WHERE bucket=?", (bucket,)).fetchone()
                if row and now - row["start"] < seconds:
                    limited |= row["count"] >= maximum
                    db.execute("UPDATE attempts SET count=count+1 WHERE bucket=?", (bucket,))
                else:
                    db.execute("INSERT OR REPLACE INTO attempts VALUES(?,?,1)", (bucket, now))
            db.execute("DELETE FROM attempts WHERE start<?", (now - 86400,))
        if limited:
            raise RateLimited("인증 시도가 너무 많습니다. 10분 후 다시 시도하세요.")
        with self.db() as db:
            query = "SELECT * FROM licenses WHERE digest=?"
            args = (self.digest(key),)
            if user is not None:
                query += " AND user_id=?"
                args += (str(user),)
            row = db.execute(query, args).fetchone()
            self.valid(row, now)
            return {"license_id": row["id"], "expires": row["expires"]}

    def check(self, license_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM licenses WHERE id=?", (license_id,)).fetchone()
            self.valid(row, time.time())
            return {"expires": row["expires"]}

    def consume(self, license_id, request_id):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            charged = db.execute("SELECT license_id FROM charges WHERE request_id=?", (request_id,)).fetchone()
            row = db.execute("SELECT * FROM licenses WHERE id=?", (license_id,)).fetchone()
            if charged:
                if charged["license_id"] != license_id or not row or row["state"] != "active":
                    raise LicenseError("유효하지 않은 실행 요청입니다.")
                if row["expires"] is not None and time.time() >= row["expires"]:
                    raise LicenseError("키가 만료되었습니다.")
                return {"expires": row["expires"]}
            self.valid(row, time.time())
            if row["remaining"] is not None:
                db.execute("UPDATE licenses SET remaining=remaining-1 WHERE id=?", (license_id,))
            db.execute("INSERT INTO charges VALUES(?,?,?)", (request_id, license_id, time.time()))
            return {"expires": row["expires"]}

    def revoke(self, actor, license_id):
        self.require_owner(actor)
        with self.db() as db:
            return db.execute("UPDATE licenses SET state='revoked' WHERE id=? AND state='active'",
                              (license_id,)).rowcount

    def revoke_grants(self, actor, user):
        self.require_owner(actor)
        with self.db() as db:
            db.execute("UPDATE grants SET remaining=0, revoked=1 WHERE user_id=?", (str(user),))

    def ticket(self, guild, user, channel=None):
        with self.db() as db:
            if channel is not None:
                db.execute("INSERT OR REPLACE INTO tickets VALUES(?,?,?)", (str(guild), str(user), channel))
            row = db.execute("SELECT channel_id FROM tickets WHERE guild_id=? AND user_id=?",
                             (str(guild), str(user))).fetchone()
            return row[0] if row else None
