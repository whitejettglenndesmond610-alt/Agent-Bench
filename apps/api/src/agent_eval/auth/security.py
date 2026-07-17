import hashlib
import hmac
import secrets
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import HTTPException, Request, status

password_hasher = PasswordHasher()


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_match(token: str, expected_hash: str) -> bool:
    return hmac.compare_digest(hash_token(token), expected_hash)


def raw_tokens_match(left: str, right: str) -> bool:
    return hmac.compare_digest(left, right)


def verify_password(password: str, encoded_hash: str) -> bool:
    try:
        return password_hasher.verify(encoded_hash, password)
    except (InvalidHashError, VerificationError, VerifyMismatchError):
        return False


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def require_same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "origin_mismatch", "message": "请求来源不受信任"},
        )

    origin_parts = urlsplit(origin)
    request_host = request.headers.get("host", "")
    request_scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    if (
        origin_parts.scheme != request_scheme
        or origin_parts.netloc != request_host
        or origin_parts.path
        or origin_parts.query
        or origin_parts.fragment
        or origin_parts.username is not None
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "origin_mismatch", "message": "请求来源不受信任"},
        )


def client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",", maxsplit=1)[0].strip()
    return request.client.host if request.client is not None else "unknown"
