from __future__ import annotations


ROSTER_PLAYER_RECORD_SIZE = 0x460
LAST_NAME_OFFSET = 0x00
FIRST_NAME_OFFSET = 0x24
ATTRIBUTE_BLOCK_OFFSET = 0x3B1
ATTRIBUTE_BLOCK_LENGTH = 62
ATTRIBUTE_BLOCK_END = ATTRIBUTE_BLOCK_OFFSET + ATTRIBUTE_BLOCK_LENGTH

# Module-relative sources observed to agree across Durant -> Klay -> Durant -> Curry.
# They remain HIGH-confidence candidates until rediscovered after a game restart.
SELECTED_RECORD_POINTER_RVAS = (0x208A7C0, 0x2233220, 0x673D160)
ROSTER_SELECTION_INDEX_RVAS = (0x69DBC08, 0x69DBC40, 0x6956AB0, 0x6956AB8)


def decode_roster_rating(raw: int) -> int:
    """Decode the verified NBA 2K17 roster rating representation."""
    if not isinstance(raw, int) or isinstance(raw, bool) or not 0 <= raw <= 0xFF:
        raise ValueError("raw rating must be an integer byte")
    if raw % 3 or raw > 222:
        raise ValueError(f"0x{raw:02X} is not a canonical encoded 25-99 rating")
    return raw // 3 + 25


def encode_roster_rating(rating: int) -> int:
    """Encode a 25-99 roster rating; this does not write game memory."""
    if not isinstance(rating, int) or isinstance(rating, bool) or not 25 <= rating <= 99:
        raise ValueError("rating must be an integer from 25 through 99")
    return (rating - 25) * 3


def utf16_fixed_field(raw: bytes, offset: int, size: int) -> str:
    field = raw[offset:offset + size]
    end = next(
        (position for position in range(0, len(field) - 1, 2) if field[position:position + 2] == b"\0\0"),
        len(field),
    )
    return field[:end].decode("utf-16-le", errors="replace")
