from __future__ import annotations

from cryptography.fernet import Fernet

from ..config import get_settings


def _fernet() -> Fernet:
    return Fernet(get_settings().fernet_key.encode())


def encrypt_token(plaintext: str) -> bytes:
    return _fernet().encrypt(plaintext.encode())


def decrypt_token(ciphertext: bytes) -> str:
    return _fernet().decrypt(ciphertext).decode()
