"""Build-bound jersey decoding and offline record previews.

Derived from the September 9 NBA 2K17 accepted UI edits on John Wall. Only
observed values are supported. No process is opened and no memory is written.
The caller still needs live team resolution and destination ownership checks.
"""
import struct

from .roster_record import ROSTER_PLAYER_RECORD_SIZE
from .roster_scan import EXPECTED_SHA256


JERSEY_WORD_OFFSET = 0x69
JERSEY_SHIFT = 5
JERSEY_MASK = 0x0FE0
OBSERVED_JERSEY_CODES = {"0": 0, "2": 2, "30": 30, "99": 99, "00": 100}


def _checked_word(record: bytes, executable_sha256: str) -> int:
    if not isinstance(executable_sha256, str) or executable_sha256.lower() != EXPECTED_SHA256:
        raise ValueError("Jersey evidence applies only to the captured NBA 2K17 build")
    if not isinstance(record, bytes) or len(record) != ROSTER_PLAYER_RECORD_SIZE:
        raise ValueError("Expected an immutable 0x460-byte player record")
    return struct.unpack_from("<H", record, JERSEY_WORD_OFFSET)[0]


def decode_observed_jersey(record: bytes, *, executable_sha256: str) -> str | None:
    """Return the observed display string, preserving 00; unknown codes are None."""
    code = (_checked_word(record, executable_sha256) & JERSEY_MASK) >> JERSEY_SHIFT
    return next((display for display, observed in OBSERVED_JERSEY_CODES.items() if observed == code), None)


def preview_observed_jersey(record: bytes, jersey: str, *, executable_sha256: str) -> bytes:
    """Return a changed copy for a tested value; preserve every other record bit.

    Display strings distinguish 0 from 00. This deliberately rejects untested
    jersey values and does not authorize applying the returned bytes to a game.
    """
    word = _checked_word(record, executable_sha256)
    if not isinstance(jersey, str) or jersey not in OBSERVED_JERSEY_CODES:
        raise ValueError("Jersey value has no completed controlled capture")
    changed = bytearray(record)
    word = (word & ~JERSEY_MASK) | (OBSERVED_JERSEY_CODES[jersey] << JERSEY_SHIFT)
    struct.pack_into("<H", changed, JERSEY_WORD_OFFSET, word)
    return bytes(changed)
