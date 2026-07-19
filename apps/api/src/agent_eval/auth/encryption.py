import base64
import binascii
import os
from uuid import UUID

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr

TOKEN_PREFIX = "v1:"
TOKEN_AAD = b"agent-evaluation-platform:agent-token:v1"
PRIVATE_TOKEN_PREFIX = "v2:"
PRIVATE_TOKEN_AAD_PREFIX = "agent-evaluation-platform:private-agent-token:v2"
MIN_ENCRYPTED_PAYLOAD_BYTES = 12 + 16


class EncryptionConfigurationError(ValueError):
    pass


def generate_encryption_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


class TokenCipher:
    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise EncryptionConfigurationError("encryption key must decode to 32 bytes")
        self._cipher = AESGCM(key)

    @classmethod
    def from_secret(cls, secret: SecretStr) -> "TokenCipher":
        try:
            key = base64.b64decode(
                secret.get_secret_value().encode("ascii"), altchars=b"-_", validate=True
            )
        except (binascii.Error, ValueError, UnicodeEncodeError) as error:
            raise EncryptionConfigurationError(
                "encryption key is not valid URL-safe Base64"
            ) from error
        return cls(key)

    def encrypt(self, token: str) -> str:
        nonce = os.urandom(12)
        ciphertext = self._cipher.encrypt(nonce, token.encode("utf-8"), TOKEN_AAD)
        payload = base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")
        return f"{TOKEN_PREFIX}{payload}"

    def decrypt(self, encrypted_token: str) -> str:
        if not encrypted_token.startswith(TOKEN_PREFIX):
            raise ValueError("unsupported encrypted token version")
        try:
            payload = base64.b64decode(
                encrypted_token.removeprefix(TOKEN_PREFIX).encode("ascii"),
                altchars=b"-_",
                validate=True,
            )
        except (binascii.Error, ValueError, UnicodeEncodeError) as error:
            raise ValueError("encrypted token payload is not valid URL-safe Base64") from error
        if len(payload) < MIN_ENCRYPTED_PAYLOAD_BYTES:
            raise ValueError("encrypted token payload is too short")
        nonce, ciphertext = payload[:12], payload[12:]
        return self._cipher.decrypt(nonce, ciphertext, TOKEN_AAD).decode("utf-8")

    def encrypt_private(self, token: str, owner_user_id: UUID, version_id: UUID) -> str:
        nonce = os.urandom(12)
        ciphertext = self._cipher.encrypt(
            nonce,
            token.encode("utf-8"),
            self._private_aad(owner_user_id, version_id),
        )
        payload = base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")
        return f"{PRIVATE_TOKEN_PREFIX}{payload}"

    def decrypt_private(
        self, encrypted_token: str, owner_user_id: UUID, version_id: UUID
    ) -> str:
        if not encrypted_token.startswith(PRIVATE_TOKEN_PREFIX):
            raise ValueError("unsupported encrypted private token version")
        try:
            payload = base64.b64decode(
                encrypted_token.removeprefix(PRIVATE_TOKEN_PREFIX).encode("ascii"),
                altchars=b"-_",
                validate=True,
            )
        except (binascii.Error, ValueError, UnicodeEncodeError) as error:
            raise ValueError("encrypted token payload is not valid URL-safe Base64") from error
        if len(payload) < MIN_ENCRYPTED_PAYLOAD_BYTES:
            raise ValueError("encrypted token payload is too short")
        nonce, ciphertext = payload[:12], payload[12:]
        return self._cipher.decrypt(
            nonce,
            ciphertext,
            self._private_aad(owner_user_id, version_id),
        ).decode("utf-8")

    @staticmethod
    def _private_aad(owner_user_id: UUID, version_id: UUID) -> bytes:
        return f"{PRIVATE_TOKEN_AAD_PREFIX}:{owner_user_id}:{version_id}".encode("ascii")
