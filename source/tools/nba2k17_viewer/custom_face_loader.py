"""Read-only health checks for the reviewed WAIGUA custom-file reader.

DLL presence and the separate manifest hook do not prove file reading is active.
Never invoke the legacy installer in a running game: its live recovery crashed
the November 14 test session. Startup compatibility requires a separate test.
"""
from pathlib import Path
import hashlib
import json
import struct
from functools import lru_cache

from .game_hook import HOOK_EXECUTABLE_SHA256, file_sha256
from .custom_face_manifest import inspect_manifest

COMPANION_SHA256 = '6dbcdd5dde699af266430ae59c58b99ad1fe3765fc9e11bf947ddc31b13668d1'
# Same code/data bytes; only limdata's IMAGE_SCN_MEM_SHARED flag is cleared.
# Process isolation tested separately; this fingerprint does not assert rendering.
ISOLATED_COMPANION_SHA256 = '168beb3078043e45cb001a83b4410ef36ef294a3033ece1244cf742b6815e53e'
SITES = ((0x140FA0F, bytes.fromhex('48894c240855565741544155415641574883ec60')),
         (0x142479C, bytes.fromhex('488b004533c9488bd64889442420e8d144f4ff')))
STUB_SHA256 = 'f69817d732aaffe9a1d8eecca592c135de87a6f70048b8fa1d72d409f1410b39'


def _matches_stub(stub, address, game_base, module_base):
    if len(stub) != 0xD0:
        return False
    if (struct.unpack_from('<Q', stub, 0x43)[0] != module_base + 0x1240
            or struct.unpack_from('<Q', stub, 0x9A)[0] != module_base + 0x14E0
            or address + 0xC8 + struct.unpack_from('<i', stub, 0xC4)[0] != game_base + 0x140FA14):
        return False
    normalized = bytearray(stub)
    for start, end in ((0x43, 0x4B), (0x9A, 0xA2), (0xC4, 0xC8)):
        normalized[start:end] = bytes(end - start)
    return hashlib.sha256(normalized).hexdigest() == STUB_SHA256


def isolated_candidate(raw):
    """Prepare the exact one-bit startup candidate; never install or hot-patch it.

    Cross-process isolation is verified. The original publication races remain;
    this is not a complete loader repair and must not be offered by the UI.
    """
    digest = hashlib.sha256(raw).hexdigest()
    if digest == ISOLATED_COMPANION_SHA256:
        return raw
    if digest != COMPANION_SHA256:
        raise ValueError('This companion DLL is not the reviewed isolation candidate input.')
    output = bytearray(raw)
    # limdata Characteristics: D0000040 -> C0000040. All code stays identical.
    output[0x2DF] &= ~0x10
    output = bytes(output)
    if hashlib.sha256(output).hexdigest() != ISOLATED_COMPANION_SHA256:
        raise ValueError('Companion isolation candidate failed its fingerprint check.')
    return output


def inspect_reader(reader):
    """Inspect only the exact reviewed build; unknown loaders stay unverified."""
    game = reader.main_module
    if file_sha256(game.path) != HOOK_EXECUTABLE_SHA256:
        return {'state': 'unverified', 'message': 'Custom-face reader uses an unverified game build.'}
    module = next((m for m in reader.modules if m.name.casefold() == '2k17waiguadll.dll'), None)
    if module is None:
        return {'state': 'missing', 'message': 'The companion custom-face reader is not loaded.'}
    if file_sha256(module.path) not in (COMPANION_SHA256, ISOLATED_COMPANION_SHA256):
        return {'state': 'unverified', 'message': 'Custom-face reader version is not verified.'}
    raw = [reader.read_or_raise(game.base_address + off, len(original)) for off, original in SITES]
    if all(data == original for data, (_, original) in zip(raw, SITES)):
        return {'state': 'inactive', 'message':
                'The hook DLL is loaded, but the companion custom-face reader did not initialize. '
                'Custom faces can appear invisible. Loader repair is required.'}
    if raw[0][0] == 0xE9 and raw[1][:1] == b'\xe8' and raw[1][5:6] == b'\x90':
        targets = [game.base_address + off + 5 + struct.unpack('<i', data[1:5])[0]
                   for data, (off, _) in zip(raw, SITES)]
        if targets[0] == targets[1] + 0x70:
            try:
                stub = reader.read_or_raise(targets[1], 0xD0)
            except (OSError, ValueError, RuntimeError):
                stub = b''
            if (_matches_stub(stub, targets[1], game.base_address, module.base_address)
                    and raw[0][5:] == SITES[0][1][5:]
                    and raw[1][6:] == SITES[1][1][6:]):
                return {'state': 'patched', 'message':
                        'Custom-face reader patches detected; individual models still need an in-game check.'}
    return {'state': 'broken', 'message':
            'The reviewed custom-face reader has incomplete or conflicting patches. Loader repair is required.'}


@lru_cache(maxsize=1)
def asset_requirements():
    path = Path(__file__).with_name('assets') / 'mod-face-requirements.json'
    return json.loads(path.read_text(encoding='utf-8'))['faces']


def inspect_loader(reader):
    """Both the arbitrary-ID lookup hook and companion reader are dependencies."""
    reading = inspect_reader(reader)
    manifest = inspect_manifest(reader)
    components = {'reader': reading, 'manifest': manifest}
    for status in (reading, manifest):
        if status['state'] in ('inactive', 'missing', 'broken'):
            return {**status, 'components': components}
    uncertain = [status['message'] for status in (reading, manifest) if status['state'] == 'unverified']
    if uncertain:
        return {'state': 'unverified', 'message': ' '.join(uncertain), 'components': components}
    return {'state': 'patched', 'components': components, 'message':
            'Custom-face lookup and reader patches detected; individual models still need an in-game check.'}


def check_injection(reader, plans, *, allow_reader_repair=False):
    """Stop this known inactive-loader failure before any player data is written."""
    face_ids = set()
    for plan in plans:
        raw = bytes.fromhex(plan.get('after_hex', ''))
        if len(raw) >= 102:
            face_ids.add(struct.unpack_from('<H', raw, 100)[0])
    if not face_ids:
        return {'state': 'not_needed'}
    directory = Path(reader.main_module.path).parent
    required = asset_requirements()
    missing, conflicts = [], []
    for face in sorted(face_ids):
        for filename in required.get(str(face), {}).get('files', []):
            candidates = [directory / folder / filename for folder in ('Modded', 'waigua')
                          if (directory / folder / filename).is_file()]
            if not candidates:
                missing.append(filename)
            elif len(candidates) > 1 and len({file_sha256(path) for path in candidates}) > 1:
                conflicts.append(filename)
    if missing:
        raise ValueError('Required custom-face files are missing from Modded/waigua: '
                         + ', '.join(missing) + '. Injection stopped before changing players.')
    if conflicts:
        raise ValueError('Conflicting custom-face files exist in both Modded and waigua: '
                         + ', '.join(conflicts) + '. Keep one matching version of each file. '
                         'Injection stopped before changing players.')
    installed = sorted(face for face in face_ids if any(
        (directory / folder / f'{prefix}{face}.iff').is_file()
        for folder in ('Modded', 'waigua') for prefix in ('face', 'png')))
    if not installed:
        return {'state': 'not_needed'}
    companion = next((module for module in reader.modules
                      if module.name.casefold() == '2k17waiguadll.dll'), None)
    if companion and file_sha256(companion.path) in (COMPANION_SHA256, ISOLATED_COMPANION_SHA256):
        # The primary hook discovers both directories, but this exact companion
        # prefixes the unmodified filename with './Modded/' in its size helper.
        wrong_directory = [f'{prefix}{face}.iff' for face in installed for prefix in ('face', 'png')
                           if (directory / 'waigua' / f'{prefix}{face}.iff').is_file()
                           and not (directory / 'Modded' / f'{prefix}{face}.iff').is_file()]
        if wrong_directory:
            raise ValueError('This installed custom-face reader requires these files in Modded: '
                             + ', '.join(wrong_directory) + '. Copy them from waigua to Modded. '
                             'Injection stopped before changing players.')
    status = inspect_loader(reader)
    status['face_ids'] = installed
    if (allow_reader_repair and status['state'] == 'inactive'
            and companion and file_sha256(companion.path) == COMPANION_SHA256
            and status.get('components', {}).get('reader', {}).get('state') == 'inactive'
            and status.get('components', {}).get('manifest', {}).get('state') == 'patched'):
        return {**status, 'state': 'needs_repair',
                'message': 'Custom-face reader will initialize when you inject.'}
    if status['state'] in ('inactive', 'missing', 'broken'):
        raise ValueError(status['message'] + ' Injection stopped before changing players. '
                         'Affected custom face IDs: ' + ', '.join(map(str, installed)) + '.')
    return status
