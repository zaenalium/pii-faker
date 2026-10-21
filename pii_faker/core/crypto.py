"""
Encryption helpers for mapping files (roadmap 3.1).

On-disk envelope::

    {
      "enc": "aes-gcm",
      "kdf": "scrypt" | "none",
      "salt": "<base64>",   # only when kdf == scrypt
      "nonce": "<base64>",
      "ct": "<base64>"
    }

When kdf == "none", the key is a raw 32-byte AES key from the keychain or
PII_FAKER_PASSPHRASE (base64-encoded 32 bytes) or an explicit b64
--passphrase. When kdf == "scrypt", the key is derived from the passphrase
via Scrypt(passphrase, salt). Old plaintext v2 files keep loading unchanged —
decryption probes the envelope before falling back to plaintext.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
from typing import Any, Dict, Optional, Tuple

SERVICE = "pii-faker"
ACCOUNT = "master"
ENV_VAR = "PII_FAKER_PASSPHRASE"


def _b64e(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _b64d(s: str) -> bytes:
    return base64.b64decode(s.encode())


class CryptoError(RuntimeError):
    pass


def is_encrypted_envelope(data: Any) -> bool:
    return isinstance(data, dict) and data.get("enc") == "aes-gcm" and "ct" in data


def _derive(passphrase: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    kdf = Scrypt(salt=salt, length=32, n=2**14, r=8, p=1)
    return kdf.derive(passphrase.encode("utf-8"))


def _get_key_for_encrypt(
    passphrase: Optional[str], use_keyring: bool = True
) -> Tuple[bytes, str, Optional[bytes]]:
    """Resolve a 32-byte raw AES key for encryption.

    Returns (key_bytes, kdf_name, salt_or_None).
    """
    if passphrase is not None:
        salt = secrets.token_bytes(16)
        return _derive(passphrase, salt), "scrypt", salt
    env_pw = os.environ.get(ENV_VAR)
    if env_pw:
        salt = secrets.token_bytes(16)
        return _derive(env_pw, salt), "scrypt", salt
    if use_keyring:
        raw = _keyring_get()
        if raw is not None:
            return raw, "none", None
    raise CryptoError(
        "No encryption key available. Set --passphrase, PII_FAKER_PASSPHRASE, "
        "or run: pii-faker key set"
    )


def _keyring_get() -> Optional[bytes]:
    try:
        import keyring

        val = keyring.get_password(SERVICE, ACCOUNT)
        if val is None:
            return None
        try:
            decoded = _b64d(val)
            if len(decoded) == 32:
                return decoded
        except Exception:
            pass
        return val.encode("utf-8")[:32].ljust(32, b"\x00")
    except Exception:
        return None


def keyring_set(raw_key: Optional[bytes] = None) -> None:
    """Store a 32-byte master key in the OS keychain. Generates one if None."""
    import keyring

    if raw_key is None:
        raw_key = secrets.token_bytes(32)
    if len(raw_key) != 32:
        raise ValueError("Master key must be 32 bytes")
    keyring.set_password(SERVICE, ACCOUNT, _b64e(raw_key))


def keyring_exists() -> bool:
    try:
        import keyring

        return keyring.get_password(SERVICE, ACCOUNT) is not None
    except Exception:
        return False


def keyring_delete() -> bool:
    try:
        import keyring

        keyring.delete_password(SERVICE, ACCOUNT)
        return True
    except Exception:
        return False


def _resolve_decrypt_key(
    envelope: Dict[str, Any], passphrase: Optional[str], use_keyring: bool = True
) -> bytes:
    kdf_name = envelope.get("kdf", "none")
    if kdf_name == "scrypt":
        salt_b64 = envelope.get("salt")
        if not salt_b64:
            raise CryptoError("Missing salt for scrypt envelope")
        salt = _b64d(salt_b64)
        pw = passphrase if passphrase is not None else os.environ.get(ENV_VAR)
        if pw is None:
            raise CryptoError("Passphrase required for this file (set --passphrase or PII_FAKER_PASSPHRASE)")
        return _derive(pw, salt)
    if kdf_name == "none":
        if passphrase is not None:
            try:
                decoded = _b64d(passphrase)
                if len(decoded) == 32:
                    return decoded
            except Exception:
                pass
            return passphrase.encode("utf-8")[:32].ljust(32, b"\x00")
        env_pw = os.environ.get(ENV_VAR)
        if env_pw is not None:
            try:
                decoded = _b64d(env_pw)
                if len(decoded) == 32:
                    return decoded
            except Exception:
                pass
            return env_pw.encode("utf-8")[:32].ljust(32, b"\x00")
        if use_keyring:
            k = _keyring_get()
            if k is not None:
                return k
        raise CryptoError("No key available to decrypt (set keychain or --passphrase)")
    raise CryptoError(f"Unknown kdf: {kdf_name}")


def encrypt_bytes(plaintext: bytes, passphrase: Optional[str] = None, use_keyring: bool = True) -> Dict[str, str]:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key, kdf_name, salt = _get_key_for_encrypt(passphrase, use_keyring=use_keyring)
    nonce = secrets.token_bytes(12)
    ct = AESGCM(key).encrypt(nonce, plaintext, None)
    env: Dict[str, str] = {"enc": "aes-gcm", "kdf": kdf_name, "nonce": _b64e(nonce), "ct": _b64e(ct)}
    if salt is not None:
        env["salt"] = _b64e(salt)
    return env


def decrypt_bytes(envelope: Dict[str, Any], passphrase: Optional[str] = None, use_keyring: bool = True) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not is_encrypted_envelope(envelope):
        raise CryptoError("Not an encrypted envelope")
    key = _resolve_decrypt_key(envelope, passphrase, use_keyring=use_keyring)
    ct = _b64d(envelope["ct"])
    nonce = _b64d(envelope["nonce"])
    try:
        return AESGCM(key).decrypt(nonce, ct, None)
    except Exception as e:
        raise CryptoError("Decryption failed — wrong key or corrupted file") from e


def encrypt_dict(data: Dict[str, Any], passphrase: Optional[str] = None, use_keyring: bool = True) -> Dict[str, str]:
    raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
    return encrypt_bytes(raw, passphrase=passphrase, use_keyring=use_keyring)


def decrypt_dict(envelope: Dict[str, Any], passphrase: Optional[str] = None, use_keyring: bool = True) -> Dict[str, Any]:
    raw = decrypt_bytes(envelope, passphrase=passphrase, use_keyring=use_keyring)
    return json.loads(raw.decode("utf-8"))
