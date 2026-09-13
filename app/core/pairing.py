"""Minting and checking the two credentials in the branch handshake.

Two credentials, because they are handled by different things. The **pairing key** is handled by a
person — read off a screen, written on an install sheet, dictated over a phone — so it is short,
upper-case, and drawn from an alphabet with no characters that get misread. The **sync secret** is
handled only by two servers, so it is long and random and never has to survive being read aloud.
"""
import hmac
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt

# No 0/O, no 1/I/L, no U/V. Someone will read these down a bad phone line, and "was that a zero or
# an oh" is a support call that this alphabet simply prevents.
_ALPHABET = "23456789ABCDEFGHJKMNPQRSTWXYZ"
_GROUPS = 3
_GROUP_LEN = 4

# 12 characters out of 29 ≈ 58 bits. Far past guessable, and it only has to survive a two-week
# window before it expires — it is not a password, it is a one-time handover.
PAIRING_TTL = timedelta(days=14)


def new_pairing_code() -> str:
    """`DM-7K4P-QX92-M3RT` — grouped because people transcribe groups accurately and long strings
    badly."""
    body = "-".join(
        "".join(secrets.choice(_ALPHABET) for _ in range(_GROUP_LEN)) for _ in range(_GROUPS)
    )
    return f"DM-{body}"


def normalize_pairing_code(raw: str) -> str:
    """What the branch typed, reduced to what we compare. Case, spaces and dashes are presentation
    — someone typing `dm 7k4p qx92 m3rt` has typed the right key and should not be told otherwise."""
    cleaned = "".join(ch for ch in (raw or "").upper() if ch.isalnum())
    if cleaned.startswith("DM"):
        cleaned = cleaned[2:]
    return cleaned


def pairing_codes_match(stored: str | None, presented: str) -> bool:
    if not stored:
        return False
    # Constant time: a timing oracle on a 58-bit code is not a realistic attack, but comparing
    # secrets with `==` is the habit that eventually gets applied to something that matters.
    return hmac.compare_digest(normalize_pairing_code(stored), normalize_pairing_code(presented))


def pairing_expiry(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)) + PAIRING_TTL


def is_pairing_expired(expires_at: datetime | None, now: datetime | None = None) -> bool:
    if expires_at is None:
        return False
    return expires_at < (now or datetime.now(timezone.utc))


def new_sync_secret() -> str:
    """48 URL-safe characters. Machine-to-machine only, so length costs nothing."""
    return secrets.token_urlsafe(36)


def hash_sync_secret(secret: str) -> str:
    return bcrypt.hashpw(secret.encode(), bcrypt.gensalt()).decode()


def verify_sync_secret(secret: str, secret_hash: str | None) -> bool:
    if not secret_hash or not secret:
        return False
    try:
        return bcrypt.checkpw(secret.encode(), secret_hash.encode())
    except ValueError:
        # A malformed hash in the column is a corrupt row, not a valid login.
        return False
