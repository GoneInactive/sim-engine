"""Auth for the two privilege surfaces (§7/§8): student accounts and the
admin password.

Account-centric by design: a key is generated *from* an account, not the
other way around. `by_account` is the source of truth (one active key per
account at a time); `by_key` is just a reverse index for fast lookup on
every request.

Two ways an account gets a key:
  - `register(account_id, password)`: self-serve from the website. Active
    immediately (no admin approval step) and password-protected so a
    student can log back in later from a different browser/session.
  - `issue_key(account_id)`: admin-driven (the admin panel's "Register
    account" form). Starts inactive; an admin must call `activate`. No
    password — meant for accounts set up ahead of time.

Calling `register` again for an account that already has a password is
treated as a login attempt, not a re-registration, so it never silently
overwrites credentials. Calling `register` for an account that was created
via the admin path (no password yet) "claims" it: sets the password and
activates it.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass, field


def _hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100_000)


@dataclass
class ApiKeyRecord:
    key: str
    account_id: str
    active: bool = False
    password_salt: bytes | None = field(default=None, repr=False)
    password_hash: bytes | None = field(default=None, repr=False)


class AccountExistsError(Exception):
    pass


class AuthStore:
    def __init__(self, admin_password: str, website_password: str):
        self.admin_password = admin_password
        self.website_password = website_password
        self.by_account: dict[str, ApiKeyRecord] = {}
        self.by_key: dict[str, ApiKeyRecord] = {}

    def _new_record(self, account_id: str, active: bool) -> ApiKeyRecord:
        record = ApiKeyRecord(key=secrets.token_urlsafe(24), account_id=account_id, active=active)
        self.by_account[account_id] = record
        self.by_key[record.key] = record
        return record

    def issue_key(self, account_id: str) -> ApiKeyRecord:
        existing = self.by_account.get(account_id)
        if existing is not None:
            return existing
        return self._new_record(account_id, active=False)

    def register(self, account_id: str, password: str) -> ApiKeyRecord:
        existing = self.by_account.get(account_id)
        if existing is not None and existing.password_hash is not None:
            raise AccountExistsError(f"{account_id} is already registered")
        record = existing if existing is not None else self._new_record(account_id, active=True)
        salt = os.urandom(16)
        record.password_salt = salt
        record.password_hash = _hash_password(password, salt)
        record.active = True
        return record

    def login(self, account_id: str, password: str) -> ApiKeyRecord | None:
        record = self.by_account.get(account_id)
        if record is None or record.password_hash is None:
            return None
        candidate = _hash_password(password, record.password_salt)
        if not hmac.compare_digest(candidate, record.password_hash):
            return None
        return record

    def regenerate_key(self, account_id: str) -> ApiKeyRecord:
        old = self.by_account.get(account_id)
        if old is not None:
            del self.by_key[old.key]
        record = ApiKeyRecord(
            key=secrets.token_urlsafe(24),
            account_id=account_id,
            active=old.active if old is not None else False,
            password_salt=old.password_salt if old is not None else None,
            password_hash=old.password_hash if old is not None else None,
        )
        self.by_account[account_id] = record
        self.by_key[record.key] = record
        return record

    def key_for_account(self, account_id: str) -> ApiKeyRecord | None:
        return self.by_account.get(account_id)

    def list_keys(self) -> list[ApiKeyRecord]:
        return list(self.by_account.values())

    def activate(self, key: str) -> ApiKeyRecord:
        record = self.by_key.get(key)
        if record is None:
            raise KeyError("no such key")
        record.active = True
        return record

    def deactivate(self, key: str) -> ApiKeyRecord:
        record = self.by_key.get(key)
        if record is None:
            raise KeyError("no such key")
        record.active = False
        return record

    def resolve(self, key: str) -> ApiKeyRecord | None:
        record = self.by_key.get(key)
        if record is None or not record.active:
            return None
        return record

    def check_admin_password(self, password: str) -> bool:
        return secrets.compare_digest(password, self.admin_password)

    def check_website_password(self, password: str) -> bool:
        return secrets.compare_digest(password, self.website_password)
