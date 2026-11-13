"""PII protection: keyed hashing for matching, encryption for reversible storage.

* Phones are matched by ``HMAC-SHA256(key, "+91XXXXXXXXXX")``. A keyed hash (not
  a plain SHA-256) matters here: there are only ~4 billion Indian mobile numbers,
  so an unkeyed hash could be reversed by brute force in minutes.
* Values I may need back (customer name, canonical phone) are encrypted with
  Fernet (AES-128-CBC + HMAC, authenticated).
* One master secret is the only input. Separate sub-keys for hashing and for
  encryption are derived from it, so neither key is used for two purposes.

Key source, in order: env var ``AU_PII_KEY``, otherwise a key file
``<db>.key`` next to the database, created on first use (mode 0600, gitignored).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
from pathlib import Path

from cryptography.fernet import Fernet

KEY_ENV = "AU_PII_KEY"


def _derive(master: bytes, label: str) -> bytes:
    return hmac.new(master, label.encode(), hashlib.sha256).digest()


class PiiCodec:
    def __init__(self, master_key: bytes):
        if not master_key:
            raise ValueError("PII key must not be empty")
        self._hash_key = _derive(master_key, "au/phone-hmac/v1")
        self._fernet = Fernet(base64.urlsafe_b64encode(_derive(master_key, "au/fernet/v1")))
        # Stored in the DB so a run with the wrong key fails loudly instead of
        # silently turning every event into an orphan.
        self.fingerprint = _derive(master_key, "au/fingerprint/v1").hex()[:16]

    def hash_phone(self, canonical_phone: str) -> str:
        return hmac.new(self._hash_key, canonical_phone.encode(), hashlib.sha256).hexdigest()

    def encrypt(self, data: dict) -> bytes:
        return self._fernet.encrypt(json.dumps(data, sort_keys=True).encode())

    def decrypt(self, token: bytes) -> dict:
        return json.loads(self._fernet.decrypt(token))


def key_file_for(db_path: str) -> Path:
    return Path(f"{db_path}.key")


def load_master_key(db_path: str) -> bytes:
    """Return the master key from ``AU_PII_KEY`` or the DB's key file (creating it)."""
    env = os.environ.get(KEY_ENV)
    if env:
        return env.encode()
    if db_path == ":memory:":
        return secrets.token_bytes(32)

    path = key_file_for(db_path)
    if path.exists():
        return path.read_text(encoding="ascii").strip().encode()
    token = secrets.token_urlsafe(32)
    # O_EXCL: never overwrite a key that appeared between the check and now.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(token + "\n")
    print(f"note: generated new PII key at {path} (set {KEY_ENV} to manage it yourself)",
          file=sys.stderr)
    return token.encode()
