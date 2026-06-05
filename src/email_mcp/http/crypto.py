"""Encryption-at-rest for mailbox passwords in the multi-tenant HTTP service.

Each stored secret is sealed with AES-256-GCM under a per-row key derived (HKDF-SHA256)
from a single host master secret (`EMAIL_MCP_MASTER_KEY`) plus a fresh random per-row
salt. The salt and the sealed token (nonce + ciphertext+tag) are stored alongside the
row; the master secret never touches the database. Losing the master secret makes the
stored passwords unrecoverable (by design) — `logout`/delete simply drop the ciphertext.
"""
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_INFO = b"email-mcp mailbox password v1"
_ENV = "EMAIL_MCP_MASTER_KEY"


class MasterKeyMissing(RuntimeError):
    """Raised when no master secret is configured — we refuse to store plaintext."""


def generate_master_key():
    """A fresh master secret to put in EMAIL_MCP_MASTER_KEY (shown once at setup)."""
    import secrets
    return secrets.token_urlsafe(48)


def _ikm(master_key):
    """Resolve the input keying material as bytes (explicit arg wins over the env)."""
    if master_key is None:
        master_key = os.environ.get(_ENV)
    if not master_key:
        raise MasterKeyMissing(
            f"{_ENV} is not set; refusing to store a mailbox password without an "
            f"encryption key. Generate one with crypto.generate_master_key().")
    return master_key.encode("utf-8") if isinstance(master_key, str) else master_key


def _derive(salt, master_key):
    hkdf = HKDF(algorithm=SHA256(), length=32, salt=salt, info=_INFO)
    return hkdf.derive(_ikm(master_key))


def encrypt_secret(plaintext, master_key=None):
    """Seal `plaintext` → (salt, token). `token` is nonce(12) || AES-GCM(ciphertext+tag).
    A new random salt and nonce are used every call, so identical passwords differ."""
    salt = os.urandom(16)
    nonce = os.urandom(12)
    key = _derive(salt, master_key)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return salt, nonce + ct


def decrypt_secret(salt, token, master_key=None):
    """Reverse encrypt_secret. Raises if the master secret/salt/token don't match."""
    nonce, ct = token[:12], token[12:]
    key = _derive(salt, master_key)
    return AESGCM(key).decrypt(nonce, ct, None).decode("utf-8")


def master_key_configured(master_key=None):
    """Cheap check for setup/health: is an encryption key available at all?"""
    try:
        _ikm(master_key)
        return True
    except MasterKeyMissing:
        return False
