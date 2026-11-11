"""Encryption for the secrets the server has to keep: model-provider keys and
sandbox tokens.

API keys for the server itself are only ever stored as hashes. These are
different: the server must be able to send them to a provider, so they are
stored encrypted and unlocked with a secret that lives outside the database.

    MINI_MLOP_SECRET_KEY set    the secret is that value; a copy of the data
                                directory or a backup is useless without it
    not set                     a random key file, <data>/secret.key, is used:
                                this protects a copied database file, but not
                                a copy of the whole data directory

Built from standard-library primitives so the server keeps its no-dependency
promise: scrypt derives the keys, SHAKE-256 keyed with a fresh nonce produces
the keystream, and HMAC-SHA-256 over the nonce and ciphertext authenticates
it (encrypt-then-MAC). Stored form: "enc:v1:" + base64(nonce | tag | data).
"""

import base64
import hashlib
import hmac
import os
import secrets

PREFIX = "enc:v1:"
ENV = "MINI_MLOP_SECRET_KEY"
_NONCE, _TAG = 16, 32


class SecretError(Exception):
    """A stored secret cannot be read with the current key."""


def _derive(secret, salt):
    try:
        return hashlib.scrypt(secret, salt=salt, n=2**14, r=8, p=1, dklen=64)
    except (AttributeError, ValueError):  # Python built without scrypt
        return hashlib.pbkdf2_hmac("sha256", secret, salt, 600_000, dklen=64)


class SecretBox:
    def __init__(self, data_dir, secret=None):
        """`secret` is the operator's secret (from the environment), or None
        to use the key file in the data directory."""
        self.source = "env" if secret else "keyfile"
        salt_path = os.path.join(data_dir, "secret.salt")
        salt = self._read_or_create(salt_path, 16)
        if secret:
            material = secret.encode() if isinstance(secret, str) else secret
        else:
            material = self._read_or_create(os.path.join(data_dir, "secret.key"), 32)
        keys = _derive(material, salt)
        self._enc, self._mac = keys[:32], keys[32:]

    @staticmethod
    def _read_or_create(path, size):
        try:
            with open(path, "rb") as f:
                value = f.read()
            if len(value) >= size:
                return value
        except FileNotFoundError:
            pass
        value = secrets.token_bytes(size)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(value)
        return value

    def _stream(self, nonce, length):
        return hashlib.shake_256(self._enc + nonce).digest(length)

    def _tag(self, nonce, data):
        return hmac.new(self._mac, PREFIX.encode() + nonce + data, hashlib.sha256).digest()

    def encrypt(self, text):
        if not text:
            return ""
        plain = text.encode()
        nonce = secrets.token_bytes(_NONCE)
        data = bytes(a ^ b for a, b in zip(plain, self._stream(nonce, len(plain))))
        return PREFIX + base64.b64encode(nonce + self._tag(nonce, data) + data).decode()

    def decrypt(self, stored):
        if not stored:
            return ""
        if not is_encrypted(stored):
            return stored  # written before encryption existed
        try:
            raw = base64.b64decode(stored[len(PREFIX):], validate=True)
        except ValueError:
            raise SecretError("stored secret is damaged")
        nonce, tag, data = raw[:_NONCE], raw[_NONCE:_NONCE + _TAG], raw[_NONCE + _TAG:]
        if len(tag) != _TAG or not hmac.compare_digest(tag, self._tag(nonce, data)):
            raise SecretError("stored secret cannot be read with the current secret key")
        return bytes(a ^ b for a, b in zip(data, self._stream(nonce, len(data)))).decode()


def is_encrypted(value):
    return isinstance(value, str) and value.startswith(PREFIX)
