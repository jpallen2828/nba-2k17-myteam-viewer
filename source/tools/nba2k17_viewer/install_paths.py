"""Steam installation roots used only to suggest roster folders."""
import os
from pathlib import Path


def steam_roots():
    candidates = []
    if os.name == 'nt':
        import winreg
        for hive, key, value in [
            (winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam', 'SteamPath'),
            (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\WOW6432Node\Valve\Steam', 'InstallPath'),
        ]:
            try:
                with winreg.OpenKey(hive, key) as handle:
                    candidates.append(Path(winreg.QueryValueEx(handle, value)[0]))
            except OSError:
                pass
    for key in ('PROGRAMFILES(X86)', 'PROGRAMFILES'):
        if os.environ.get(key):
            candidates.append(Path(os.environ[key]) / 'Steam')
    return list(dict.fromkeys(p for p in candidates if p.is_dir()))
