"""Encryption for secrets saved from the Settings tab (API keys, tokens, passwords).

The key is SETTINGS_KEY from the compose file when set (any string), otherwise a
random key generated into settings.key next to the database. Values encrypted
with a key file are re-encrypted with SETTINGS_KEY once it is added.
"""
import base64
import hashlib
import logging
import os

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from database import DB_PATH

logger = logging.getLogger(__name__)

PREFIX = "enc:v1:"
KEY_FILE = os.path.join(os.path.dirname(DB_PATH), "settings.key")

_box = None
_primary = None


def _env_key() -> Fernet | None:
    secret = os.environ.get("SETTINGS_KEY", "")
    if not secret:
        return None
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


def _file_key(create: bool) -> Fernet | None:
    try:
        with open(KEY_FILE, "rb") as f:
            return Fernet(f.read().strip())
    except FileNotFoundError:
        if not create:
            return None
    key = Fernet.generate_key()
    fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    logger.info("Generated settings encryption key at %s", KEY_FILE)
    return Fernet(key)


def _get_box() -> MultiFernet:
    """First key encrypts; every key can decrypt."""
    global _box, _primary
    if _box is None:
        env = _env_key()
        keys = [k for k in (env, _file_key(create=env is None)) if k]
        _box, _primary = MultiFernet(keys), keys[0]
    return _box


def is_encrypted(value: str) -> bool:
    return value.startswith(PREFIX)


def encrypt(value: str) -> str:
    return PREFIX + _get_box().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    """Plain text (saved before encryption existed) is returned unchanged.
    Raises ValueError when no available key can decrypt the value."""
    if not is_encrypted(value):
        return value
    try:
        return _get_box().decrypt(value[len(PREFIX):].encode()).decode()
    except InvalidToken:
        raise ValueError("settings key does not match") from None


def on_primary_key(value: str) -> bool:
    """Whether an encrypted value already uses the current primary key (SETTINGS_KEY when set)."""
    _get_box()
    try:
        _primary.decrypt(value[len(PREFIX):].encode())
        return True
    except InvalidToken:
        return False


def rotate(value: str) -> str:
    """Re-encrypt with the current primary key."""
    return PREFIX + _get_box().rotate(value[len(PREFIX):].encode()).decode()


def primary_is_env() -> bool:
    return _env_key() is not None


def remove_key_file():
    """Called once every value is encrypted with SETTINGS_KEY, so the data folder alone can't decrypt them."""
    try:
        os.remove(KEY_FILE)
        logger.info("SETTINGS_KEY in use: removed %s", KEY_FILE)
    except FileNotFoundError:
        pass
