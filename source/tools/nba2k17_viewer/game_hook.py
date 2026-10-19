"""Manage the optional game hook without loading DLLs or writing game memory.

The hook reads its configuration at startup and exposes no control API. A saved
switch therefore describes the next launch, never a proven active feature.
"""
from __future__ import annotations

from collections import OrderedDict
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import tempfile


HOOK_OPTIONS = OrderedDict([
    ('rosterCustomData', {'title': 'Preserve custom roster data',
                         'description': 'Disable the game roster synchronization that can restore official values.'}),
    ('forcedisplayphotos', {'title': 'Keep player portraits after team changes',
                           'description': 'Keep available player photos when a player changes teams; missing photos still use renders.'}),
    ('unlockRetiredNumber', {'title': 'Unlock retired jersey numbers',
                           'description': 'Allow retired numbers in the game Edit Player menu.'}),
    ('unlockedShoesBrands', {'title': 'Unlock shoe brands',
                           'description': 'Allow any shoe brand for any player in the game editor.'}),
    ('quickGameReset', {'title': 'Reset Play Now and 2KU teams',
                       'description': 'Restore the initial default teams when those modes initialize.'}),
    ('debugLogging', {'title': 'Write hook diagnostics',
                      'description': 'Write troubleshooting messages to NBA2K17_Hook.log after restarting the game.'}),
])
DEPENDENCIES = ('2K17WAIGUADLL.dll', 'dinput8.dll')
CONFIG_RELATIVE = 'NBA2K_Hook/config.txt'
HOOK_EXECUTABLE_SHA256 = '86c9d5396761e64567f8ec6233bd1ccb58f483d81455d055bb5c6c9444076a35'
HOOK_PE_TIMESTAMP = 0x58DECE37
ORIGINAL_HOOK_IMAGE_SIZE = 0x09A70000
HOOK_IMAGE_SIZE = 0x09A4C000
HOOK_PROFILE = 'March 2017 · verified executable compatibility build'
HOOK_LAYOUT_OFFSETS = (0xC74, 0xCE0, 0xD08, 0xE0F, 0xF3A, 0xF54, 0x113C, 0x1277, 0xC4DC)
HOOK_INSTALL_SHA256 = 'c2cb35fcab2d9c227a8c90cae411ba6c44d7fb47d9fbb549c29c62bb94179b5c'
# Reviewed package fingerprints. They identify the supported payload, not an
# arbitrary similarly named DLL downloaded later.
PAYLOAD_HASHES = {
    'd3d11.dll': '4e8ac6b1b18a2fa78ee1136c85d9c996a2c86adacc6dbbcfe5f2b0e6549766ae',
    'NBA2K_Hook.dll': '54c017897a10b14da4b3d86158f0bee756cbbb8dc279808c15671daf97ad3fe6',
    CONFIG_RELATIVE: 'cec41bb6a74e911c1f307fcdb95bd2d79dcf0172a6ac0a086572725945e55290',
    'readme.txt': '5b00923128c9e88a0cb05d75938faa49d4e692ba64124f5bd66bc01caa889eed',
}
INSTALL_FILES = {
    'd3d11.dll': 'd3d11.dll',
    'NBA2K_Hook.dll': 'NBA2K_Hook.dll',
    CONFIG_RELATIVE: CONFIG_RELATIVE,
    'NBA2K_Hook/readme.txt': 'readme.txt',
}


def installed_payload_hash(source):
    return HOOK_INSTALL_SHA256 if source == 'NBA2K_Hook.dll' else PAYLOAD_HASHES[source]


def materialize_install_payload(source, raw):
    """Derive only the reviewed exact-executable profile; keep originals intact.

    All eight compiled layout selectors and their descriptor must agree. The
    original PE timestamp and all eleven runtime byte guards remain unchanged.
    Installation independently requires the pinned executable hash and header.
    """
    if hashlib.sha256(raw).hexdigest() != PAYLOAD_HASHES[source]:
        raise ValueError('Bundled hook file changed: ' + source)
    if source != 'NBA2K_Hook.dll':
        return raw
    old = struct.pack('<I', ORIGINAL_HOOK_IMAGE_SIZE)
    new = struct.pack('<I', HOOK_IMAGE_SIZE)
    occurrences, start = [], 0
    while (offset := raw.find(old, start)) >= 0:
        occurrences.append(offset)
        start = offset + 1
    if tuple(occurrences) != HOOK_LAYOUT_OFFSETS:
        raise ValueError('Hook layout selectors differ from the reviewed compatibility profile.')
    output = bytearray(raw)
    for offset in HOOK_LAYOUT_OFFSETS:
        output[offset:offset + 4] = new
    output = bytes(output)
    if hashlib.sha256(output).hexdigest() != HOOK_INSTALL_SHA256:
        raise ValueError('Hook compatibility build failed its fingerprint check.')
    return output


def hook_payload_dir():
    """Locate only bundled or project-owned package data, never a game DLL."""
    candidates = []
    if getattr(sys, 'frozen', False):
        candidates.append(Path(getattr(sys, '_MEIPASS', Path(sys.executable).parent)) / 'game_hook')
        candidates.append(Path(sys.executable).parent / 'NBA 2K17 Hook')
    else:
        candidates.extend(parent / 'NBA 2K17 Hook' for parent in Path(__file__).resolve().parents)
        candidates.append(Path(__file__).parent / 'assets/game_hook')
    return next((p for p in candidates if p.is_dir()), None)


def _root(game_dir):
    if not str(game_dir).strip():
        raise ValueError('Choose the folder containing NBA2K17.exe.')
    root = Path(game_dir).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError('Choose the folder containing NBA2K17.exe.')
    return root


def _inside(root, relative):
    path = root / relative
    resolved = path.resolve()
    if not resolved.is_relative_to(root) or resolved == root:
        raise ValueError('Hook file resolves outside the selected game folder: ' + relative)
    if path.exists() and not path.is_file():
        raise ValueError('Hook file is not a regular file: ' + relative)
    return path


@lru_cache(maxsize=64)
def _hash_stat(path, size, mtime_ns, ctime_ns, inode):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def file_sha256(path):
    path = Path(path).resolve(strict=True)
    before = path.stat()
    key = (str(path), before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_ino)
    result = _hash_stat(*key)
    after = path.stat()
    if (after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_ino) != key[1:]:
        raise ValueError('File changed while it was being checked: ' + path.name)
    return result


@lru_cache(maxsize=1)
def supported_executable_sha256():
    path = Path(__file__).parent / 'assets/injection-runtime.json'
    mapped = json.loads(path.read_text(encoding='utf-8'))['executable_sha256'].lower()
    # Updating the injector registry alone cannot approve another executable
    # for this separately fingerprinted compatibility profile.
    return HOOK_EXECUTABLE_SHA256 if mapped == HOOK_EXECUTABLE_SHA256 else None


def _file_info(root, relative, expected=None):
    result = {'path': str(root / relative), 'exists': False, 'sha256': None,
              'payload_match': False if expected else None, 'error': None}
    try:
        path = _inside(root, relative)
        result['exists'] = path.is_file()
        if result['exists']:
            result['sha256'] = file_sha256(path)
            if expected:
                result['payload_match'] = result['sha256'] == expected
    except (OSError, ValueError) as exc:
        result['error'] = str(exc)
    return result


def _x64_dll(path):
    """Read only the PE header; never load a dependency to inspect it."""
    try:
        with Path(path).open('rb') as stream:
            head = stream.read(64)
            if len(head) != 64 or head[:2] != b'MZ':
                return False
            offset = struct.unpack_from('<I', head, 0x3c)[0]
            if offset > 1024 * 1024:
                return False
            stream.seek(offset)
            pe = stream.read(24)
        return (len(pe) == 24 and pe[:4] == b'PE\0\0'
                and struct.unpack_from('<H', pe, 4)[0] == 0x8664
                and bool(struct.unpack_from('<H', pe, 22)[0] & 0x2000))
    except OSError:
        return False


def _executable_header(path):
    result = {'pe_timestamp': None, 'image_size': None, 'hook_header_match': False}
    try:
        with Path(path).open('rb') as stream:
            head = stream.read(64)
            if len(head) != 64 or head[:2] != b'MZ':
                return result
            offset = struct.unpack_from('<I', head, 0x3c)[0]
            if offset > 1024 * 1024:
                return result
            stream.seek(offset)
            pe = stream.read(84)
        if len(pe) != 84 or pe[:4] != b'PE\0\0' or struct.unpack_from('<H', pe, 4)[0] != 0x8664:
            return result
        result['pe_timestamp'] = struct.unpack_from('<I', pe, 8)[0]
        result['image_size'] = struct.unpack_from('<I', pe, 80)[0]
        result['hook_header_match'] = (result['pe_timestamp'] == HOOK_PE_TIMESTAMP
                                       and result['image_size'] == HOOK_IMAGE_SIZE)
    except OSError:
        pass
    return result


def _parse_config(raw):
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate configuration key: ' + key)
            result[key] = value
        return result
    value = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=unique_pairs)
    if not isinstance(value, dict) or not isinstance(value.get('global'), dict):
        raise ValueError('Hook config must contain a global object.')
    return value


def load_options(game_dir):
    result = {'path': str(Path(game_dir) / CONFIG_RELATIVE), 'exists': False,
              'values': {key: None for key in HOOK_OPTIONS}, 'errors': [], 'sha256': None}
    try:
        root = _root(game_dir)
        path = _inside(root, CONFIG_RELATIVE)
        result['path'] = str(path)
        result['exists'] = path.is_file()
        if not result['exists']:
            return result
        if path.stat().st_size > 1024 * 1024:
            raise ValueError('Hook configuration is unexpectedly large.')
        raw = path.read_bytes()
        result['sha256'] = hashlib.sha256(raw).hexdigest()
        payload = _parse_config(raw)
        for key in HOOK_OPTIONS:
            value = payload['global'].get(key, 0 if key == 'debugLogging' else None)
            if type(value) is int and value in (0, 1):
                result['values'][key] = bool(value)
            else:
                result['errors'].append('Missing or invalid 0/1 value for ' + key)
    except (OSError, ValueError, UnicodeError) as exc:
        result['errors'].append(str(exc))
    return result


def _log_tail(root):
    result = {'path': str(root / 'NBA2K17_Hook.log'), 'text': '', 'error': None,
              'session_verified': False}
    try:
        path = _inside(root, 'NBA2K17_Hook.log')
        if path.is_file():
            with path.open('rb') as stream:
                stream.seek(max(0, path.stat().st_size - 16384))
                result['text'] = stream.read(16384).decode('utf-8', errors='replace')
    except (OSError, ValueError) as exc:
        result['error'] = str(exc)
    return result


def inspect_installation(game_dir):
    """Inspect disk state. ``ready`` means installed, never proven running."""
    root = _root(game_dir)
    executable = _file_info(root, 'NBA2K17.exe')
    supported_hash = supported_executable_sha256()
    executable['supported'] = bool(supported_hash) and executable['sha256'] == supported_hash
    executable.update(_executable_header(executable['path']) if not executable['error'] else {})
    executable['hook_supported'] = bool(executable['supported'] and executable.get('hook_header_match'))
    files = {dest: _file_info(root, dest, installed_payload_hash(source))
             for dest, source in INSTALL_FILES.items()}
    dependencies = {name: _file_info(root, name) for name in DEPENDENCIES}
    for info in dependencies.values():
        info['valid_x64_dll'] = info['exists'] and not info['error'] and _x64_dll(info['path'])
    config = load_options(root)
    issues = []
    if not executable['supported']:
        issues.append('NBA2K17.exe is missing or is not the supported executable build.')
    elif not executable['hook_supported']:
        issues.append('The injector supports this executable, but the bundled hook does not match its PE layout. A compatible hook build is required.')
    for name, info in dependencies.items():
        if not info['valid_x64_dll']:
            issues.append('Missing or invalid required 64-bit DLL: ' + name)
    for name in ('d3d11.dll', 'NBA2K_Hook.dll'):
        info = files[name]
        if info['error'] or (info['exists'] and not info['payload_match']):
            issues.append('Existing hook DLL conflicts with the bundled version: ' + name)
    issues.extend(config['errors'])
    payload = hook_payload_dir()
    payload_valid = False
    if payload:
        try:
            payload_valid = all(file_sha256(_inside(payload.resolve(), name)) == digest
                                for name, digest in PAYLOAD_HASHES.items())
            if payload_valid:
                materialize_install_payload('NBA2K_Hook.dll', (payload / 'NBA2K_Hook.dll').read_bytes())
        except (OSError, ValueError):
            payload_valid = False
    ready = not issues and config['exists'] and all(files[name]['payload_match'] for name in ('d3d11.dll', 'NBA2K_Hook.dll'))
    return {'game_dir': str(root), 'executable': executable, 'files': files,
            'dependencies': dependencies, 'config': config, 'ready': ready,
            'installable': not issues and payload_valid, 'issues': issues,
            'payload_available': payload_valid, 'log': _log_tail(root),
            'settings_scope': 'next_game_launch', 'hook_profile': HOOK_PROFILE}


def save_options(game_dir, values, *, expected_sha256=None):
    """Atomically save next-launch settings while preserving unknown fields.

    Pass the hash from load_options to reject concurrent edits. Missing configs
    can be created by the installer, so the settings editor never invents one.
    """
    unknown = set(values) - set(HOOK_OPTIONS)
    if unknown:
        raise ValueError('Unknown hook option: ' + ', '.join(sorted(unknown)))
    if any(type(value) not in (int, bool) or value not in (0, 1) for value in values.values()):
        raise ValueError('Hook options must be Yes/No values.')
    root = _root(game_dir)
    if not _inside(root, 'NBA2K17.exe').is_file():
        raise ValueError('The selected game folder does not contain NBA2K17.exe.')
    path = _inside(root, CONFIG_RELATIVE)
    if path.stat().st_size > 1024 * 1024:
        raise ValueError('Hook configuration is unexpectedly large.')
    before = path.read_bytes()
    digest = hashlib.sha256(before).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise ValueError('Hook settings changed elsewhere. Refresh before saving.')
    config = _parse_config(before)
    config['global'].update({key: int(value) for key, value in values.items()})
    for key in HOOK_OPTIONS:
        value = config['global'].get(key, 0 if key == 'debugLogging' else None)
        if type(value) is not int or value not in (0, 1):
            raise ValueError('Provide a valid Yes/No value for ' + key)
    after = (json.dumps(config, indent=4, ensure_ascii=False) + '\n').encode('utf-8')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.hook-settings-', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(after)
            stream.flush()
            os.fsync(stream.fileno())
        _inside(root, CONFIG_RELATIVE)
        if path.read_bytes() != before:
            raise ValueError('Hook settings changed elsewhere. Refresh before saving.')
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()
    result = load_options(root)
    result['restart_required'] = True
    return result


def _process_api():
    from scanner.windows_process import ProcessReader, find_processes
    from scanner.discovery.live import process_token
    return find_processes, ProcessReader, process_token


def probe_processes():
    """Bounded read-only process/module probe, without roster or memory scans."""
    result = {'processes': [], 'error': None}
    try:
        find_processes, reader_type, process_token = _process_api()
        candidates = find_processes()
    except (OSError, ValueError, RuntimeError) as exc:
        result['error'] = str(exc)
        return result
    for pid, name in candidates:
        item = {'pid': pid, 'name': name, 'token': None, 'path': None, 'game_dir': None,
                'supported': False, 'executable_sha256': None, 'hook_loaded': False,
                'hook_verified': False, 'modules': [], 'error': None,
                'feature_status': {key: 'unverified' for key in HOOK_OPTIONS}}
        try:
            with reader_type(pid) as reader:
                item['token'] = process_token(reader)
                info = reader.process_info()
                path = Path(info.executable_path).resolve()
                item['path'] = str(path)
                item['game_dir'] = str(path.parent)
                item['executable_sha256'] = file_sha256(path)
                supported_hash = supported_executable_sha256()
                item['supported'] = bool(supported_hash) and item['executable_sha256'] == supported_hash
                item['hook_supported'] = bool(item['supported'] and _executable_header(path)['hook_header_match'])
                for module in info.modules:
                    name = module.name.casefold()
                    if name not in ('nba2k_hook.dll', 'd3d11.dll', '2k17waiguadll.dll', 'dinput8.dll'):
                        continue
                    module_info = {'name': module.name, 'path': module.path,
                                   'base_address': module.base_address, 'size': module.size,
                                   'sha256': None, 'payload_match': False, 'error': None}
                    try:
                        module_info['sha256'] = file_sha256(module.path)
                        expected = next((installed_payload_hash(k) for k in PAYLOAD_HASHES if k.casefold() == name), None)
                        module_info['payload_match'] = expected is not None and module_info['sha256'] == expected
                    except (OSError, ValueError) as exc:
                        module_info['error'] = str(exc)
                    item['modules'].append(module_info)
                    if name == 'nba2k_hook.dll':
                        item['hook_loaded'] = True
                        item['hook_verified'] = module_info['payload_match']
                if process_token(reader) != item['token']:
                    raise ValueError('Game process changed during the check.')
        except (OSError, ValueError, RuntimeError) as exc:
            item['error'] = str(exc)
            item['hook_loaded'] = item['hook_verified'] = False
        result['processes'].append(item)
    return result


def _require_game_closed():
    find_processes, _, _ = _process_api()
    if find_processes():
        raise ValueError('Close NBA 2K17 before installing the hook. Your game has not been closed automatically.')


def install_hook(game_dir):
    """Install the reviewed package only after complete, closed-game preflight.

    Existing configurations and matching binaries are retained. Conflicting
    binaries are never replaced. On failure, remove only files this call created.
    """
    root = _root(game_dir)
    _require_game_closed()
    inspected = inspect_installation(root)
    if not inspected['installable']:
        raise ValueError('; '.join(inspected['issues']) or 'The verified hook payload is unavailable.')
    payload = hook_payload_dir().resolve()
    planned = []
    for destination, source in INSTALL_FILES.items():
        target = _inside(root, destination)
        raw = _inside(payload, source).read_bytes()
        raw = materialize_install_payload(source, raw)
        if target.exists():
            # Configuration and documentation can differ without replacing them.
            if destination.endswith('.dll') and target.read_bytes() != raw:
                raise ValueError('An existing DLL conflicts with the bundled version: ' + destination)
            continue
        planned.append((destination, raw))
    created, directories = [], []
    _require_game_closed()
    try:
        for destination, raw in planned:
            _require_game_closed()
            target = _inside(root, destination)
            if not target.parent.exists():
                target.parent.mkdir()
                directories.append(target.parent)
            target = _inside(root, destination)
            with target.open('xb') as stream:
                created.append((target, raw))
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            if target.read_bytes() != raw:
                raise OSError('Installed hook file failed verification: ' + destination)
        _require_game_closed()
        result = inspect_installation(root)
        if not result['ready']:
            raise ValueError('Hook installation did not pass its final checks.')
        result['installed_files'] = [str(path.relative_to(root)) for path, _ in created]
        result['restart_required'] = True
        return result
    except Exception:
        for path, raw in reversed(created):
            # A concurrent edit is not ours to delete. A partial write is ours.
            try:
                current = _inside(root, str(path.relative_to(root))).read_bytes()
                if raw.startswith(current):
                    path.unlink()
            except OSError:
                pass
        for directory in reversed(directories):
            try:
                directory.rmdir()
            except OSError:
                pass
        raise
