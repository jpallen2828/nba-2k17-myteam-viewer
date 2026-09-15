"""Dependency-free validation for downloaded card-art image assets.

This module deliberately performs structural/header validation rather than image
recognition.  In particular, it never uses OCR or attempts to infer card text.
The SHA-256 digest and reason codes make placeholder and duplicate decisions
auditable by callers.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import struct
from typing import Iterable, Mapping, Sequence
import zlib


SUPPORTED_FORMATS = ("PNG", "JPEG", "GIF", "WEBP")

_EXPECTED_SUFFIXES: dict[str, frozenset[str]] = {
    "PNG": frozenset({".png"}),
    "JPEG": frozenset({".jpg", ".jpeg", ".jpe"}),
    "GIF": frozenset({".gif"}),
    "WEBP": frozenset({".webp"}),
}


class ImageHeaderError(ValueError):
    """Raised internally when a supported image has an invalid structure."""


@dataclass(frozen=True)
class AssetValidationResult:
    """Stable, serializable validation result for one downloaded asset.

    ``has_transparency`` describes alpha/transparency metadata visible without
    interpreting card content.  It is ``None`` when validation failed or the
    header cannot establish an answer.
    """

    valid: bool
    actual_format: str | None
    width: int | None
    height: int | None
    byte_size: int
    sha256: str
    has_transparency: bool | None
    duplicate_matches: tuple[str, ...]
    placeholder_or_error: bool
    placeholder_reasons: tuple[str, ...]
    errors: tuple[str, ...]
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "valid": self.valid,
            "actual_format": self.actual_format,
            "width": self.width,
            "height": self.height,
            "byte_size": self.byte_size,
            "sha256": self.sha256,
            "has_transparency": self.has_transparency,
            "duplicate_matches": list(self.duplicate_matches),
            "placeholder_or_error": self.placeholder_or_error,
            "placeholder_reasons": list(self.placeholder_reasons),
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class _DecodedHeader:
    actual_format: str
    width: int
    height: int
    has_transparency: bool | None
    warnings: tuple[str, ...] = ()


def _u24_little(value: bytes) -> int:
    if len(value) != 3:
        raise ImageHeaderError("invalid 24-bit field")
    return value[0] | (value[1] << 8) | (value[2] << 16)


def _decode_png(data: bytes) -> _DecodedHeader:
    signature = b"\x89PNG\r\n\x1a\n"
    if not data.startswith(signature):
        raise ImageHeaderError("invalid PNG signature")

    offset = len(signature)
    width: int | None = None
    height: int | None = None
    color_type: int | None = None
    transparency = False
    saw_idat = False
    saw_iend = False
    chunk_index = 0

    while offset < len(data):
        if len(data) - offset < 12:
            raise ImageHeaderError("truncated PNG chunk header")
        length = struct.unpack_from(">I", data, offset)[0]
        chunk_type = data[offset + 4 : offset + 8]
        payload_start = offset + 8
        payload_end = payload_start + length
        crc_end = payload_end + 4
        if crc_end > len(data):
            raise ImageHeaderError("truncated PNG chunk payload")
        stored_crc = struct.unpack_from(">I", data, payload_end)[0]
        actual_crc = zlib.crc32(chunk_type)
        actual_crc = zlib.crc32(data[payload_start:payload_end], actual_crc) & 0xFFFFFFFF
        if stored_crc != actual_crc:
            label = chunk_type.decode("ascii", "replace")
            raise ImageHeaderError(f"PNG {label} CRC mismatch")

        payload = data[payload_start:payload_end]
        if chunk_index == 0 and chunk_type != b"IHDR":
            raise ImageHeaderError("PNG IHDR is not the first chunk")
        if chunk_type == b"IHDR":
            if chunk_index != 0 or length != 13:
                raise ImageHeaderError("invalid PNG IHDR")
            width, height, bit_depth, color_type, compression, filtering, interlace = (
                struct.unpack(">IIBBBBB", payload)
            )
            if width <= 0 or height <= 0:
                raise ImageHeaderError("invalid PNG dimensions")
            valid_depths = {
                0: {1, 2, 4, 8, 16},
                2: {8, 16},
                3: {1, 2, 4, 8},
                4: {8, 16},
                6: {8, 16},
            }
            if color_type not in valid_depths or bit_depth not in valid_depths[color_type]:
                raise ImageHeaderError("invalid PNG bit-depth/color-type combination")
            if compression != 0 or filtering != 0 or interlace not in {0, 1}:
                raise ImageHeaderError("unsupported PNG header method")
            transparency = color_type in {4, 6}
        elif chunk_type == b"tRNS":
            if color_type is None:
                raise ImageHeaderError("PNG tRNS precedes IHDR")
            if color_type in {0, 2}:
                transparency = True
            elif color_type == 3:
                transparency = any(alpha < 255 for alpha in payload)
        elif chunk_type == b"IDAT":
            saw_idat = True
        elif chunk_type == b"IEND":
            if length != 0:
                raise ImageHeaderError("invalid PNG IEND")
            saw_iend = True
            offset = crc_end
            break

        offset = crc_end
        chunk_index += 1

    if width is None or height is None:
        raise ImageHeaderError("PNG is missing IHDR")
    if not saw_idat:
        raise ImageHeaderError("PNG is missing image data")
    if not saw_iend:
        raise ImageHeaderError("PNG is missing IEND")
    warnings = ("trailing_bytes_after_png",) if offset < len(data) else ()
    return _DecodedHeader("PNG", width, height, transparency, warnings)


_JPEG_SOF_MARKERS = frozenset(
    {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
)


def _decode_jpeg(data: bytes) -> _DecodedHeader:
    if not data.startswith(b"\xff\xd8"):
        raise ImageHeaderError("invalid JPEG SOI marker")
    offset = 2
    width: int | None = None
    height: int | None = None
    saw_scan = False

    while offset < len(data):
        if data[offset] != 0xFF:
            raise ImageHeaderError("invalid JPEG marker alignment")
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            raise ImageHeaderError("truncated JPEG marker")
        marker = data[offset]
        offset += 1
        if marker == 0xD9:
            break
        if marker in {0x01, *range(0xD0, 0xD8)}:
            continue
        if offset + 2 > len(data):
            raise ImageHeaderError("truncated JPEG segment length")
        segment_length = struct.unpack_from(">H", data, offset)[0]
        if segment_length < 2 or offset + segment_length > len(data):
            raise ImageHeaderError("invalid JPEG segment length")
        payload_start = offset + 2
        payload_end = offset + segment_length
        if marker in _JPEG_SOF_MARKERS:
            if segment_length < 8:
                raise ImageHeaderError("truncated JPEG SOF segment")
            height = struct.unpack_from(">H", data, payload_start + 1)[0]
            width = struct.unpack_from(">H", data, payload_start + 3)[0]
            if width <= 0 or height <= 0:
                raise ImageHeaderError("invalid JPEG dimensions")
        if marker == 0xDA:
            saw_scan = True
            offset = payload_end
            break
        offset = payload_end

    if width is None or height is None:
        raise ImageHeaderError("JPEG is missing a supported SOF marker")
    if not saw_scan:
        raise ImageHeaderError("JPEG is missing scan data")
    eoi = data.find(b"\xff\xd9", offset)
    if eoi < 0:
        raise ImageHeaderError("JPEG is missing EOI")
    warnings = ("trailing_bytes_after_jpeg",) if eoi + 2 < len(data) else ()
    return _DecodedHeader("JPEG", width, height, False, warnings)


def _skip_gif_sub_blocks(data: bytes, offset: int) -> int:
    while True:
        if offset >= len(data):
            raise ImageHeaderError("truncated GIF sub-block")
        length = data[offset]
        offset += 1
        if length == 0:
            return offset
        if offset + length > len(data):
            raise ImageHeaderError("truncated GIF sub-block payload")
        offset += length


def _decode_gif(data: bytes) -> _DecodedHeader:
    if not (data.startswith(b"GIF87a") or data.startswith(b"GIF89a")):
        raise ImageHeaderError("invalid GIF signature")
    if len(data) < 13:
        raise ImageHeaderError("truncated GIF logical screen descriptor")
    width, height = struct.unpack_from("<HH", data, 6)
    if width <= 0 or height <= 0:
        raise ImageHeaderError("invalid GIF dimensions")
    packed = data[10]
    offset = 13
    if packed & 0x80:
        offset += 3 * (2 ** ((packed & 0x07) + 1))
        if offset > len(data):
            raise ImageHeaderError("truncated GIF global color table")

    transparency = False
    saw_image = False
    saw_trailer = False
    while offset < len(data):
        introducer = data[offset]
        offset += 1
        if introducer == 0x3B:
            saw_trailer = True
            break
        if introducer == 0x21:
            if offset >= len(data):
                raise ImageHeaderError("truncated GIF extension")
            label = data[offset]
            offset += 1
            if label == 0xF9:
                if offset + 6 > len(data) or data[offset] != 4:
                    raise ImageHeaderError("invalid GIF graphics control extension")
                transparency = transparency or bool(data[offset + 1] & 0x01)
                if data[offset + 5] != 0:
                    raise ImageHeaderError("unterminated GIF graphics control extension")
                offset += 6
            else:
                offset = _skip_gif_sub_blocks(data, offset)
            continue
        if introducer == 0x2C:
            if offset + 9 > len(data):
                raise ImageHeaderError("truncated GIF image descriptor")
            image_width, image_height = struct.unpack_from("<HH", data, offset + 4)
            if image_width <= 0 or image_height <= 0:
                raise ImageHeaderError("invalid GIF frame dimensions")
            local_packed = data[offset + 8]
            offset += 9
            if local_packed & 0x80:
                offset += 3 * (2 ** ((local_packed & 0x07) + 1))
                if offset > len(data):
                    raise ImageHeaderError("truncated GIF local color table")
            if offset >= len(data):
                raise ImageHeaderError("GIF is missing LZW code size")
            minimum_code_size = data[offset]
            if not 2 <= minimum_code_size <= 12:
                raise ImageHeaderError("invalid GIF LZW code size")
            offset = _skip_gif_sub_blocks(data, offset + 1)
            saw_image = True
            continue
        raise ImageHeaderError("unknown GIF block introducer")

    if not saw_image:
        raise ImageHeaderError("GIF is missing image data")
    if not saw_trailer:
        raise ImageHeaderError("GIF is missing trailer")
    warnings = ("trailing_bytes_after_gif",) if offset < len(data) else ()
    return _DecodedHeader("GIF", width, height, transparency, warnings)


def _decode_webp(data: bytes) -> _DecodedHeader:
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise ImageHeaderError("invalid WebP RIFF signature")
    declared_end = struct.unpack_from("<I", data, 4)[0] + 8
    if declared_end < 12 or declared_end > len(data):
        raise ImageHeaderError("truncated WebP RIFF payload")

    offset = 12
    width: int | None = None
    height: int | None = None
    canvas_width: int | None = None
    canvas_height: int | None = None
    transparency = False
    saw_visual_chunk = False
    while offset < declared_end:
        if offset + 8 > declared_end:
            raise ImageHeaderError("truncated WebP chunk header")
        chunk_type = data[offset : offset + 4]
        chunk_size = struct.unpack_from("<I", data, offset + 4)[0]
        payload_start = offset + 8
        payload_end = payload_start + chunk_size
        padded_end = payload_end + (chunk_size & 1)
        if payload_end > declared_end or padded_end > declared_end:
            raise ImageHeaderError("truncated WebP chunk payload")
        payload = data[payload_start:payload_end]

        if chunk_type == b"VP8X":
            if chunk_size != 10:
                raise ImageHeaderError("invalid WebP VP8X header")
            transparency = transparency or bool(payload[0] & 0x10)
            canvas_width = _u24_little(payload[4:7]) + 1
            canvas_height = _u24_little(payload[7:10]) + 1
        elif chunk_type == b"VP8L":
            if chunk_size < 5 or payload[0] != 0x2F:
                raise ImageHeaderError("invalid WebP VP8L header")
            bits = int.from_bytes(payload[1:5], "little")
            if bits >> 29:
                raise ImageHeaderError("unsupported WebP VP8L version")
            width = (bits & 0x3FFF) + 1
            height = ((bits >> 14) & 0x3FFF) + 1
            transparency = transparency or bool((bits >> 28) & 0x01)
            saw_visual_chunk = True
        elif chunk_type == b"VP8 ":
            if chunk_size < 10 or payload[3:6] != b"\x9d\x01\x2a":
                raise ImageHeaderError("invalid WebP VP8 frame header")
            width = struct.unpack_from("<H", payload, 6)[0] & 0x3FFF
            height = struct.unpack_from("<H", payload, 8)[0] & 0x3FFF
            saw_visual_chunk = True
        elif chunk_type == b"ALPH":
            transparency = True
        elif chunk_type == b"ANMF":
            if chunk_size < 16:
                raise ImageHeaderError("invalid WebP animation frame header")
            saw_visual_chunk = True
        offset = padded_end

    width = canvas_width if canvas_width is not None else width
    height = canvas_height if canvas_height is not None else height
    if not saw_visual_chunk:
        raise ImageHeaderError("WebP is missing image/frame data")
    if width is None or height is None or width <= 0 or height <= 0:
        raise ImageHeaderError("WebP dimensions are unavailable or invalid")
    warnings = ("trailing_bytes_after_webp",) if declared_end < len(data) else ()
    return _DecodedHeader("WEBP", width, height, transparency, warnings)


def _sniff_and_decode(data: bytes) -> _DecodedHeader:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return _decode_png(data)
    if data.startswith(b"\xff\xd8"):
        return _decode_jpeg(data)
    if data.startswith((b"GIF87a", b"GIF89a")):
        return _decode_gif(data)
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _decode_webp(data)
    raise ImageHeaderError("content is not a supported PNG, JPEG, GIF, or WebP image")


def _duplicate_matches(
    digest: str,
    duplicate_hashes: Mapping[str, str | Sequence[str]] | None,
) -> tuple[str, ...]:
    if not duplicate_hashes:
        return ()
    matches = duplicate_hashes.get(digest) or duplicate_hashes.get(digest.upper())
    if matches is None:
        return ()
    if isinstance(matches, str):
        return (matches,)
    return tuple(sorted({str(item) for item in matches}))


def _looks_like_error_document(data: bytes) -> bool:
    prefix = data[:512].lstrip().lower()
    return prefix.startswith(
        (
            b"<!doctype html",
            b"<html",
            b"<head",
            b"<?xml",
            b"{\"error\"",
            b"{\"message\"",
        )
    )


def validate_image_bytes(
    data: bytes | bytearray | memoryview,
    *,
    filename: str | Path | None = None,
    known_placeholder_hashes: Iterable[str] = (),
    duplicate_hashes: Mapping[str, str | Sequence[str]] | None = None,
    minimum_card_art_dimension: int = 16,
    minimum_content_bytes: int = 32,
) -> AssetValidationResult:
    """Validate an in-memory card-art download using only its binary structure.

    Known placeholder hashes must be SHA-256 strings. ``duplicate_hashes`` maps
    a SHA-256 to one or more already-known asset identifiers; the input mapping
    is never mutated.
    """

    raw = bytes(data)
    digest = hashlib.sha256(raw).hexdigest()
    errors: list[str] = []
    warnings: list[str] = []
    header: _DecodedHeader | None = None
    try:
        header = _sniff_and_decode(raw)
        warnings.extend(header.warnings)
    except (ImageHeaderError, struct.error) as exc:
        errors.append(str(exc))

    if filename is not None and header is not None:
        suffix = Path(filename).suffix.lower()
        if suffix and suffix not in _EXPECTED_SUFFIXES[header.actual_format]:
            warnings.append("filename_extension_does_not_match_content")

    normalized_placeholder_hashes = {
        str(value).strip().lower() for value in known_placeholder_hashes
    }
    placeholder_reasons: list[str] = []
    if digest in normalized_placeholder_hashes:
        placeholder_reasons.append("known_placeholder_hash")
    if header is None:
        placeholder_reasons.append("invalid_image_content")
        if _looks_like_error_document(raw):
            placeholder_reasons.append("non_image_error_document")
    else:
        if (
            header.width < minimum_card_art_dimension
            or header.height < minimum_card_art_dimension
        ):
            placeholder_reasons.append("tiny_dimensions")
    if len(raw) < minimum_content_bytes:
        placeholder_reasons.append("tiny_content")

    return AssetValidationResult(
        valid=header is not None,
        actual_format=None if header is None else header.actual_format,
        width=None if header is None else header.width,
        height=None if header is None else header.height,
        byte_size=len(raw),
        sha256=digest,
        has_transparency=None if header is None else header.has_transparency,
        duplicate_matches=_duplicate_matches(digest, duplicate_hashes),
        placeholder_or_error=bool(placeholder_reasons),
        placeholder_reasons=tuple(placeholder_reasons),
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


def validate_image_file(
    path: str | Path,
    **kwargs: object,
) -> AssetValidationResult:
    """Read and validate one local asset, preserving the filename for checks."""

    asset_path = Path(path)
    return validate_image_bytes(
        asset_path.read_bytes(),
        filename=asset_path.name,
        **kwargs,
    )


def duplicate_hash_groups(
    results: Mapping[str, AssetValidationResult],
) -> dict[str, tuple[str, ...]]:
    """Return deterministic SHA-256 groups containing two or more asset IDs."""

    by_hash: dict[str, list[str]] = {}
    for asset_id, result in results.items():
        by_hash.setdefault(result.sha256, []).append(str(asset_id))
    return {
        digest: tuple(sorted(asset_ids))
        for digest, asset_ids in sorted(by_hash.items())
        if len(asset_ids) > 1
    }


__all__ = [
    "AssetValidationResult",
    "ImageHeaderError",
    "SUPPORTED_FORMATS",
    "duplicate_hash_groups",
    "validate_image_bytes",
    "validate_image_file",
]
