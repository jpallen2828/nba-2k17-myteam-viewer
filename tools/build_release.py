"""Build the Windows application using the matching published resource bundle."""
import argparse
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--resources', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    resources = args.resources.resolve()
    for path in ('studio_assets/assets.json', 'studio_resources/models/player_background_removal.onnx'):
        if not (resources / path).is_file():
            raise FileNotFoundError(f'Missing build resource: {path}')
    tools, scanner = root / 'source/tools', root / 'source/src'
    command = [sys.executable, '-m', 'PyInstaller', '--clean', '--noconfirm', '--onefile', '--windowed',
               '--name', '2K17 MyTEAM', '--paths', str(tools), '--paths', str(scanner),
               '--icon', str(tools / 'nba2k17_viewer/assets/myteam17.ico'),
               '--distpath', str(root / 'dist'), '--workpath', str(root / 'build'), '--specpath', str(root / 'build')]
    for source, target in [(resources / 'studio_assets', 'studio_assets'),
                           (resources / 'studio_resources', 'studio_resources'),
                           (tools / 'nba2k17_viewer/assets', 'nba2k17_viewer/assets'),
                           (tools / 'nba2k17_catalog/jersey_numbers.json', 'nba2k17_catalog')]:
        command.extend(['--add-data', str(source) + os.pathsep + target])
    for name in ('tkinter', 'webview', 'torch', 'tensorflow', 'scipy', 'matplotlib', 'pandas', 'cv2',
                 'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets', 'PySide6.QtQml'):
        command.extend(['--exclude-module', name])
    command.append(str(tools / 'run_myteam_viewer.py'))
    env = os.environ.copy()
    env['PATH'] = os.pathsep.join((str(Path(sys.executable).parent), str(Path(os.environ['SystemRoot']) / 'System32'), os.environ['SystemRoot']))
    env['PYTHONPATH'] = os.pathsep.join((str(tools), str(scanner)))
    subprocess.run(command, cwd=root, env=env, check=True)


if __name__ == '__main__':
    main()
