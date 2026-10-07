"""Record and organization ids (07 §3.1): a prefix plus a lowercase ULID.

A ULID is 26 Crockford base32 characters: a 48-bit millisecond timestamp, then 80 random bits.
Lowercase Crockford sorts the same way as the numbers it encodes, so ids sort by creation time.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

CROCKFORD = "0123456789abcdefghjkmnpqrstvwxyz"
RECORD_PREFIX = "gwa-"
ORG_PREFIX = "gwo-"

_TIME_BITS = 48
_RANDOM_BYTES = 10


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        value, rem = divmod(value, 32)
        chars.append(CROCKFORD[rem])
    if value:
        raise ValueError("value does not fit")
    return "".join(reversed(chars))


def _ulid(now: datetime, random_bytes: bytes) -> str:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if len(random_bytes) != _RANDOM_BYTES:
        raise ValueError(f"need {_RANDOM_BYTES} random bytes, got {len(random_bytes)}")
    ms = int(now.timestamp() * 1000)
    if not 0 <= ms < 2**_TIME_BITS:
        raise ValueError("timestamp out of ULID range")
    return _encode((ms << 80) | int.from_bytes(random_bytes, "big"), 26)


def new_record_id(now: datetime | None = None, rand: Callable[[int], bytes] = os.urandom) -> str:
    """A new `gwa-` id for a facility record."""
    return RECORD_PREFIX + _ulid(now or datetime.now(UTC), rand(_RANDOM_BYTES))


def new_org_id(now: datetime | None = None, rand: Callable[[int], bytes] = os.urandom) -> str:
    """A new `gwo-` id for data/orgs.json."""
    return ORG_PREFIX + _ulid(now or datetime.now(UTC), rand(_RANDOM_BYTES))


def deterministic_ids(
    seed: int, start: datetime, *, prefix: str = RECORD_PREFIX
) -> Callable[[], str]:
    """For tests and fixtures: ids that are reproducible, unique and strictly increasing.

    Call n uses the timestamp start + n ms and 80 bits taken from SHA-256(seed, n).
    """
    counter = 0

    def next_id() -> str:
        nonlocal counter
        digest = hashlib.sha256(f"{seed}:{counter}".encode()).digest()
        value = prefix + _ulid(start + timedelta(milliseconds=counter), digest[:_RANDOM_BYTES])
        counter += 1
        return value

    return next_id
