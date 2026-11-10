"""Shared account/quota storage. Passwords are never stored in plaintext."""
import base64
import hashlib
import hmac
import ipaddress
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation

DB_PATH = '/var/lib/rdnet-proxy/accounts.db'
GIB = 1024 ** 3


def quota_bytes(value):
    try:
        n = Decimal(str(value))
        if not n.is_finite() or not Decimal('0.01') <= n <= 100000:
            raise ValueError()
        return int(n * GIB)
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError('حجم باید بین 0.01 و 100000 گیگابایت باشد.') from None


def allowed_networks(value):
    if value == '*':
        return []
    try:
        parts = [p.strip() for p in value.split(',')]
        if not 1 <= len(parts) <= 16:
            raise ValueError()
        return [str(ipaddress.ip_network(p, strict=False)) for p in parts]
    except (ValueError, AttributeError):
        raise ValueError('آی‌پی یا CIDR نامعتبر است؛ حداکثر 16 مورد با کاما، یا * بفرستید.') from None


def digest(password, salt):
    return hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 120000).hex()


class Store:
    def __init__(self, path=DB_PATH):
        self.path = path

    @contextmanager
    def db(self):
        c = sqlite3.connect(self.path, timeout=5)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    def initialize(self):
        with self.db() as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS accounts (
                    username TEXT PRIMARY KEY, name TEXT NOT NULL,
                    salt TEXT NOT NULL, password_hash TEXT NOT NULL,
                    quota INTEGER NOT NULL CHECK(quota > 0),
                    used INTEGER NOT NULL DEFAULT 0,
                    max_ips INTEGER NOT NULL CHECK(max_ips BETWEEN 1 AND 32),
                    allowed_ips TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL);
            ''')

    def create(self, name, gb, max_ips, acl='*'):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 64 or any(ord(x) < 32 for x in name):
            raise ValueError('نام باید 1 تا 64 نویسه باشد.')
        if isinstance(max_ips, bool) or not isinstance(max_ips, int) or not 1 <= max_ips <= 32:
            raise ValueError('تعداد IP هم‌زمان باید بین 1 و 32 باشد.')
        quota, networks = quota_bytes(gb), allowed_networks(acl)
        username, password, salt = 'p_' + secrets.token_hex(6), secrets.token_urlsafe(24), secrets.token_hex(16)
        with self.db() as c:
            c.execute('INSERT INTO accounts(username,name,salt,password_hash,quota,max_ips,allowed_ips,created_at) VALUES(?,?,?,?,?,?,?,?)',
                      (username, name.strip(), salt, digest(password, salt), quota, max_ips, json.dumps(networks), int(time.time())))
        return {'username': username, 'password': password, 'name': name.strip(), 'quota': quota, 'max_ips': max_ips, 'allowed_ips': networks}

    def get(self, username):
        with self.db() as c:
            row = c.execute('SELECT * FROM accounts WHERE username=?', (username,)).fetchone()
            return dict(row) if row else None

    def authenticate(self, username, password):
        row = self.get(username)
        if not row:
            # Also spend hash work for unknown users to reduce username timing differences.
            digest(password, '00' * 16)
            return None
        if not hmac.compare_digest(row['password_hash'], digest(password, row['salt'])):
            return None
        return row if row['enabled'] and row['used'] < row['quota'] else None

    def enabled(self, username, password_hash):
        row = self.get(username)
        return bool(row and row['enabled'] and row['used'] < row['quota'] and row['password_hash'] == password_hash)

    def charge(self, username, amount, password_hash):
        """Reserve quota before forwarding, atomically across all sessions."""
        with self.db() as c:
            result = c.execute('UPDATE accounts SET used=used+? WHERE username=? AND enabled=1 AND password_hash=? AND used+?<=quota',
                               (amount, username, password_hash, amount))
            return result.rowcount == 1

    def list(self):
        with self.db() as c:
            return [dict(r) for r in c.execute('SELECT username,name,quota,used,max_ips,allowed_ips,enabled FROM accounts ORDER BY created_at DESC,username')]

    def disable(self, username):
        with self.db() as c:
            if c.execute('UPDATE accounts SET enabled=0 WHERE username=?', (username,)).rowcount != 1:
                raise ValueError('حساب پیدا نشد.')

    def reset_password(self, username):
        password, salt = secrets.token_urlsafe(24), secrets.token_hex(16)
        with self.db() as c:
            if c.execute('UPDATE accounts SET salt=?,password_hash=? WHERE username=?', (salt, digest(password, salt), username)).rowcount != 1:
                raise ValueError('حساب پیدا نشد.')
        return password
