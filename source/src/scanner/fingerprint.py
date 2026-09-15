from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import datetime
import hashlib
from pathlib import Path
import struct

from .models import FileFingerprint


PE_MACHINE_NAMES = {
    0x014C: "x86",
    0x8664: "x64",
    0xAA64: "arm64",
}


def _file_version(path: Path) -> str | None:
    if not hasattr(ctypes, "windll"):
        return None
    try:
        version = ctypes.windll.version
        ignored = wintypes.DWORD()
        size = version.GetFileVersionInfoSizeW(str(path), ctypes.byref(ignored))
        if not size:
            return None
        buffer = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(str(path), 0, size, buffer):
            return None
        pointer = ctypes.c_void_p()
        length = wintypes.UINT()
        if not version.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)):
            return None

        class VS_FIXEDFILEINFO(ctypes.Structure):
            _fields_ = [("dwSignature", wintypes.DWORD), ("dwStrucVersion", wintypes.DWORD),
                        ("dwFileVersionMS", wintypes.DWORD), ("dwFileVersionLS", wintypes.DWORD),
                        ("dwProductVersionMS", wintypes.DWORD), ("dwProductVersionLS", wintypes.DWORD),
                        ("dwFileFlagsMask", wintypes.DWORD), ("dwFileFlags", wintypes.DWORD),
                        ("dwFileOS", wintypes.DWORD), ("dwFileType", wintypes.DWORD),
                        ("dwFileSubtype", wintypes.DWORD), ("dwFileDateMS", wintypes.DWORD),
                        ("dwFileDateLS", wintypes.DWORD)]

        info = ctypes.cast(pointer, ctypes.POINTER(VS_FIXEDFILEINFO)).contents
        return ".".join(str(value) for value in (
            info.dwFileVersionMS >> 16,
            info.dwFileVersionMS & 0xFFFF,
            info.dwFileVersionLS >> 16,
            info.dwFileVersionLS & 0xFFFF,
        ))
    except Exception:
        return None


def pe_metadata(path: Path) -> tuple[str | None, int | None]:
    try:
        with path.open("rb") as source:
            header = source.read(4096)
        if header[:2] != b"MZ" or len(header) < 0x40:
            return None, None
        pe_offset = struct.unpack_from("<I", header, 0x3C)[0]
        if pe_offset + 12 > len(header):
            with path.open("rb") as source:
                source.seek(pe_offset)
                pe = source.read(24)
        else:
            pe = header[pe_offset:pe_offset + 24]
        if pe[:4] != b"PE\0\0":
            return None, None
        machine = struct.unpack_from("<H", pe, 4)[0]
        timestamp = struct.unpack_from("<I", pe, 8)[0]
        return PE_MACHINE_NAMES.get(machine, f"machine_0x{machine:04X}"), timestamp
    except (OSError, struct.error):
        return None, None


def fingerprint_file(path: Path, include_sha1: bool = True) -> FileFingerprint:
    resolved = path.resolve()
    stat = resolved.stat()
    sha256 = hashlib.sha256()
    sha1 = hashlib.sha1() if include_sha1 else None
    with resolved.open("rb") as source:
        for block in iter(lambda: source.read(4 * 1024 * 1024), b""):
            sha256.update(block)
            if sha1 is not None:
                sha1.update(block)
    machine, timestamp = pe_metadata(resolved)
    return FileFingerprint(
        path=str(resolved),
        size=stat.st_size,
        modified=datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(),
        sha256=sha256.hexdigest().upper(),
        sha1=sha1.hexdigest().upper() if sha1 is not None else "",
        file_version=_file_version(resolved),
        pe_machine=machine,
        pe_timestamp=timestamp,
    )
