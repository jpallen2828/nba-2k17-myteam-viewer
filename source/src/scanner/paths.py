from __future__ import annotations

import os
from pathlib import Path
import sys


def _frozen_project_root() -> Path:
    executable = Path(sys.executable).resolve()
    sibling = executable.parent / ".project"
    if sibling.is_dir():
        return sibling
    # A staged smoke-test executable lives below .project/build/dist. Route it
    # back to the real workspace instead of creating nested runtime clutter.
    for ancestor in executable.parents:
        if ancestor.name.casefold() == ".project":
            return ancestor
    return sibling


def distribution_root() -> Path:
    if getattr(sys, "frozen", False):
        return _frozen_project_root().parent
    return Path(__file__).resolve().parents[3]


def project_root() -> Path:
    if getattr(sys, "frozen", False):
        return _frozen_project_root()
    return distribution_root() / ".project"


def ensure_runtime_directories() -> dict[str, Path]:
    root = project_root()
    paths = {
        "root": root,
        "captures": root / "captures",
        "data": root / "data",
        "logs": root / "logs",
        "network_requests": root / "research" / "networking" / "requests",
        "profiles": root / "profiles",
        "research": root / "research",
        "runtime": root / "runtime",
        "schemas": root / "schemas",
        "secrets": root / "secrets",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        try:
            import ctypes

            FILE_ATTRIBUTE_HIDDEN = 0x2
            attrs = ctypes.windll.kernel32.GetFileAttributesW(str(root))
            if attrs != 0xFFFFFFFF and not (attrs & FILE_ATTRIBUTE_HIDDEN):
                ctypes.windll.kernel32.SetFileAttributesW(str(root), attrs | FILE_ATTRIBUTE_HIDDEN)
        except Exception:
            pass
    return paths
