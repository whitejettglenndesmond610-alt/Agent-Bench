import re
import unicodedata
from uuid import UUID

from agent_eval.db.enums import SessionRole

LEGACY_READ_ONLY_USER_ID = UUID("153bc83b-6584-56ec-a79b-70fe5054e537")
LEGACY_OPERATOR_USER_ID = UUID("f91b3c4a-9dbd-59ea-922f-56b64a56f801")
LEGACY_SUBJECT_KEYS = {
    SessionRole.READ_ONLY: "legacy_shared:read_only",
    SessionRole.OPERATOR: "legacy_shared:operator",
}
LEGACY_USER_IDS = {
    SessionRole.READ_ONLY: LEGACY_READ_ONLY_USER_ID,
    SessionRole.OPERATOR: LEGACY_OPERATOR_USER_ID,
}

USERNAME_PATTERN = re.compile(r"[a-z][a-z0-9._-]{2,63}")


def normalize_username(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    if USERNAME_PATTERN.fullmatch(normalized) is None:
        raise ValueError(
            "username must be 3 to 64 ASCII characters, start with a letter, and use only "
            "letters, numbers, dots, underscores, or hyphens"
        )
    return normalized


def legacy_user_id(role: SessionRole) -> UUID:
    return LEGACY_USER_IDS[role]


def legacy_subject_key(role: SessionRole) -> str:
    return LEGACY_SUBJECT_KEYS[role]
