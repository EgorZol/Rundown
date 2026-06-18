from __future__ import annotations

from cryptography.fernet import Fernet


class SecretBox:
    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key.encode("utf-8"))

    def encrypt(self, value: str) -> str:
        token = self._fernet.encrypt(value.encode("utf-8"))
        return token.decode("utf-8")

    def decrypt(self, value: str) -> str:
        raw = self._fernet.decrypt(value.encode("utf-8"))
        return raw.decode("utf-8")
