from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
from typing import Iterable

from .constants import STEAM_APP_ID


@dataclass(slots=True)
class DiscoveryResult:
    steam_roots: list[str]
    library_roots: list[str]
    game_executables: list[str]
    sync_bins: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


def _unique_paths(paths: Iterable[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        try:
            key = str(path.resolve()).casefold()
        except OSError:
            key = str(path).casefold()
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def _registry_steam_roots() -> list[Path]:
    if os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:
        return []
    keys = (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
    )
    roots: list[Path] = []
    for hive, key_name, value_name in keys:
        try:
            with winreg.OpenKey(hive, key_name) as key:
                value, _ = winreg.QueryValueEx(key, value_name)
                if value:
                    roots.append(Path(str(value)))
        except OSError:
            continue
    return roots


def steam_roots() -> list[Path]:
    candidates = _registry_steam_roots()
    for environment_name in ("PROGRAMFILES(X86)", "PROGRAMFILES"):
        value = os.environ.get(environment_name)
        if value:
            candidates.append(Path(value) / "Steam")
    return [path for path in _unique_paths(candidates) if path.is_dir()]


def parse_libraryfolders_vdf(text: str) -> list[Path]:
    paths: list[Path] = []
    # New Steam format uses nested entries containing a "path" key; older
    # formats put the numeric key and path on one line. Both are accepted.
    for match in re.finditer(r'"path"\s+"([^"]+)"', text, flags=re.I):
        paths.append(Path(match.group(1).replace("\\\\", "\\")))
    for match in re.finditer(r'^\s*"\d+"\s+"([^"]+)"', text, flags=re.M):
        paths.append(Path(match.group(1).replace("\\\\", "\\")))
    return _unique_paths(paths)


def steam_library_roots(roots: list[Path] | None = None) -> list[Path]:
    roots = roots or steam_roots()
    libraries: list[Path] = list(roots)
    for root in roots:
        vdf = root / "steamapps" / "libraryfolders.vdf"
        try:
            libraries.extend(parse_libraryfolders_vdf(vdf.read_text(encoding="utf-8-sig", errors="replace")))
        except OSError:
            continue
    return [path for path in _unique_paths(libraries) if path.is_dir()]


def _acf_install_dir(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    match = re.search(r'"installdir"\s+"([^"]+)"', text, flags=re.I)
    return match.group(1) if match else None


def discover_game_executables(libraries: list[Path] | None = None, process_path: Path | None = None) -> list[Path]:
    candidates: list[Path] = []
    if process_path:
        candidates.append(process_path)
    for library in libraries or steam_library_roots():
        steamapps = library / "steamapps"
        manifest = steamapps / f"appmanifest_{STEAM_APP_ID}.acf"
        install_dir = _acf_install_dir(manifest)
        if install_dir:
            candidates.append(steamapps / "common" / install_dir / "NBA2K17.exe")
        candidates.extend((
            steamapps / "common" / "NBA 2K17" / "NBA2K17.exe",
            library / "NBA 2K17" / "NBA2K17.exe",
        ))
    return [path.resolve() for path in _unique_paths(candidates) if path.is_file()]


def _bounded_sync_search(root: Path, max_depth: int = 5) -> list[Path]:
    if not root.is_dir():
        return []
    base_parts = len(root.parts)
    found: list[Path] = []
    try:
        for current, dirs, files in os.walk(root):
            current_path = Path(current)
            depth = len(current_path.parts) - base_parts
            if depth >= max_depth:
                dirs[:] = []
            dirs[:] = [name for name in dirs if name.casefold() not in {
                ".git", "build", "dist", "shadercache", "workshop", "temp", "tmp"
            }]
            for name in files:
                if name.casefold() == "sync.bin":
                    found.append(current_path / name)
    except OSError:
        return found
    return found


def discover_sync_bins(
    roots: list[Path] | None = None,
    libraries: list[Path] | None = None,
    executables: list[Path] | None = None,
) -> list[Path]:
    roots = roots or steam_roots()
    libraries = libraries or steam_library_roots(roots)
    candidates: list[Path] = []
    for steam in roots:
        userdata = steam / "userdata"
        if userdata.is_dir():
            try:
                for user_dir in userdata.iterdir():
                    if user_dir.is_dir():
                        app_root = user_dir / STEAM_APP_ID
                        candidates.extend((
                            app_root / "local" / "SYNC.BIN",
                            app_root / "remote" / "SYNC.BIN",
                            app_root / "SYNC.BIN",
                        ))
            except OSError:
                pass
    search_roots: list[Path] = []
    for executable in executables or []:
        search_roots.extend((executable.parent, executable.parent / "OfflineStorage"))
    user_profile = Path(os.environ.get("USERPROFILE") or Path.home())
    for environment_name in ("APPDATA", "LOCALAPPDATA", "PROGRAMDATA"):
        value = os.environ.get(environment_name)
        if value:
            search_roots.extend((Path(value) / "2K Sports" / "NBA 2K17", Path(value) / "NBA 2K17"))
    search_roots.extend((
        user_profile / "Documents" / "NBA 2K17",
        user_profile / "Documents" / "2K Sports" / "NBA 2K17",
    ))
    for root in _unique_paths(search_roots):
        candidates.extend(_bounded_sync_search(root, max_depth=5))
    return [path.resolve() for path in _unique_paths(candidates) if path.is_file()]


def discover_local_data(process_path: Path | None = None) -> DiscoveryResult:
    roots = steam_roots()
    libraries = steam_library_roots(roots)
    executables = discover_game_executables(libraries, process_path)
    sync_bins = discover_sync_bins(roots, libraries, executables)
    return DiscoveryResult(
        steam_roots=[str(path.resolve()) for path in roots],
        library_roots=[str(path.resolve()) for path in libraries],
        game_executables=[str(path) for path in executables],
        sync_bins=[str(path) for path in sync_bins],
    )


def save_discovery(result: DiscoveryResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
