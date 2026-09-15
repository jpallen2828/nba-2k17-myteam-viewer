"""Standalone PyInstaller entry point; no retired scanner/service imports."""
import sys
import traceback
from pathlib import Path

if __name__ == '__main__':
    try:
        from nba2k17_viewer.desktop import main
        raise SystemExit(main())
    except Exception:
        from nba2k17_viewer.archive_repository import project_root
        log = project_root() / '.project/logs/myteam_viewer_error.log'
        try:
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(traceback.format_exc(), encoding='utf-8')
        except OSError:
            pass
        if sys.platform == 'win32' and not any(arg in sys.argv for arg in ('--smoke-test', '--ui-smoke-test')):
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, f'The viewer could not start.\nDetails: {log}', 'NBA 2K17 MyTEAM', 0x10)
        raise SystemExit(1)
