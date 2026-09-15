from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Callable


SERVICE_HOST = "nba2k17-ws.2ksports.com"
LOOPBACK_ADDRESS = "127.0.0.1"
BEGIN_MARKER = "# BEGIN NBA2K17_LOCAL_MYTEAM"
END_MARKER = "# END NBA2K17_LOCAL_MYTEAM"
FINAL_NEWLINE_PREFIX = "# ORIGINAL_FINAL_NEWLINE="


@dataclass(frozen=True)
class HostsStatus:
    installed: bool
    exact: bool
    malformed: bool
    conflicts: tuple[str, ...]


@dataclass(frozen=True)
class HostsEncoding:
    name: str
    bom: bytes = b""


def windows_hosts_path() -> Path:
    root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    return root / "System32" / "drivers" / "etc" / "hosts"


def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _marker_indexes(lines: list[str]) -> tuple[list[int], list[int]]:
    begins = [index for index, line in enumerate(lines) if line.strip() == BEGIN_MARKER]
    ends = [index for index, line in enumerate(lines) if line.strip() == END_MARKER]
    return begins, ends


def _mapping_conflicts(lines: list[str], excluded: range | None = None) -> tuple[str, ...]:
    ignored = set(excluded or ())
    conflicts = []
    for index, raw in enumerate(lines):
        if index in ignored:
            continue
        content = raw.split("#", 1)[0].strip()
        if not content:
            continue
        parts = content.split()
        if len(parts) > 1 and SERVICE_HOST.casefold() in {item.casefold() for item in parts[1:]}:
            conflicts.append(f"line {index + 1}: {raw}")
    return tuple(conflicts)


def analyze_hosts_text(text: str) -> HostsStatus:
    lines = text.splitlines()
    begins, ends = _marker_indexes(lines)
    malformed = len(begins) != len(ends) or len(begins) > 1
    block_range = None
    exact = False
    installed = bool(begins or ends)
    if not malformed and len(begins) == 1:
        begin, end = begins[0], ends[0]
        malformed = end <= begin
        if not malformed:
            block_range = range(begin, end + 1)
            body = [line.strip() for line in lines[begin + 1:end]]
            expected_mapping = f"{LOOPBACK_ADDRESS} {SERVICE_HOST}"
            exact = (
                len(body) == 2
                and body[0] in {FINAL_NEWLINE_PREFIX + "0", FINAL_NEWLINE_PREFIX + "1"}
                and body[1] == expected_mapping
            )
    return HostsStatus(
        installed=installed,
        exact=exact,
        malformed=malformed,
        conflicts=_mapping_conflicts(lines, block_range),
    )


def install_redirect_text(text: str, host: str = SERVICE_HOST) -> str:
    if host.casefold() != SERVICE_HOST:
        raise ValueError("Strict Local Mode permits only the confirmed NBA 2K17 service hostname")
    status = analyze_hosts_text(text)
    if status.malformed:
        raise ValueError("The NBA2K17_LOCAL_MYTEAM hosts block is malformed")
    if status.installed:
        if not status.exact:
            raise ValueError("A project-owned hosts block exists but does not match the expected strict-local entry")
        return text
    if status.conflicts:
        raise ValueError("A pre-existing mapping for the NBA 2K17 hostname must be reviewed first")

    newline = _newline(text)
    had_final_newline = text.endswith(("\n", "\r"))
    separator = "" if not text or had_final_newline else newline
    block = newline.join((
        BEGIN_MARKER,
        FINAL_NEWLINE_PREFIX + ("1" if had_final_newline else "0"),
        f"{LOOPBACK_ADDRESS} {SERVICE_HOST}",
        END_MARKER,
        "",
    ))
    return text + separator + block


def remove_redirect_text(text: str) -> str:
    status = analyze_hosts_text(text)
    if status.malformed:
        raise ValueError("The NBA2K17_LOCAL_MYTEAM hosts block is malformed; refusing an ambiguous removal")
    if not status.installed:
        return text
    if not status.exact:
        raise ValueError("The project-owned hosts block was modified; refusing to remove unrelated content")

    newline = _newline(text)
    lines = text.splitlines(keepends=True)
    begin = next(index for index, line in enumerate(lines) if line.strip() == BEGIN_MARKER)
    end = next(index for index, line in enumerate(lines) if line.strip() == END_MARKER)
    metadata = lines[begin + 1].strip()
    had_final_newline = metadata == FINAL_NEWLINE_PREFIX + "1"
    prefix = "".join(lines[:begin])
    suffix = "".join(lines[end + 1:])
    if not had_final_newline and prefix.endswith(newline):
        prefix = prefix[:-len(newline)]
    return prefix + suffix


def decode_hosts_bytes(raw: bytes) -> tuple[str, HostsEncoding]:
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8"), HostsEncoding("utf-8", b"\xef\xbb\xbf")
    if raw.startswith(b"\xff\xfe"):
        return raw[2:].decode("utf-16-le"), HostsEncoding("utf-16-le", b"\xff\xfe")
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be"), HostsEncoding("utf-16-be", b"\xfe\xff")
    try:
        return raw.decode("utf-8"), HostsEncoding("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252"), HostsEncoding("cp1252")


def encode_hosts_text(text: str, encoding: HostsEncoding) -> bytes:
    return encoding.bom + text.encode(encoding.name)


def transform_hosts_file(
    path: Path,
    transform: Callable[[str], str],
    *,
    backup_path: Path | None = None,
) -> bool:
    """Apply a tested transformation while preserving the file's encoding and ACL-bearing inode."""
    path = path.resolve()
    original = path.read_bytes()
    text, encoding = decode_hosts_bytes(original)
    updated = encode_hosts_text(transform(text), encoding)
    if updated == original:
        return False
    if backup_path is not None:
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        if backup_path.exists() and backup_path.read_bytes() != original:
            raise ValueError("Hosts backup already exists with different content")
        if not backup_path.exists():
            backup_path.write_bytes(original)
    try:
        with path.open("r+b") as stream:
            stream.seek(0)
            stream.write(updated)
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        try:
            with path.open("r+b") as stream:
                stream.seek(0)
                stream.write(original)
                stream.truncate()
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            pass
        raise
    return True
