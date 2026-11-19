"""Read-only checks for arbitrary-ID manifest/provider hook dependencies.

This is separate from the WAIGUA file reader: both must be installed for the
reviewed external-face path. Signatures and jump targets were independently
captured in fresh-launch-runtime-verification.json on 2026-10-16. Original
instructions and bridge prefixes come from the hash-pinned binaries. Detecting
these patches is not evidence that any particular model renders successfully.
"""
import struct

from .game_hook import HOOK_EXECUTABLE_SHA256, HOOK_INSTALL_SHA256, file_sha256


# feature, game RVA, original overwritten bytes, target hook RVA, bridge prefix
CORE_SITES = (
    ('manifest context', 0x1424294, bytes.fromhex('458b43204533d249f7d141ffc8'), 0xA93D,
     bytes.fromhex('9c50535152565755415041514152415341544155415641574881ec80000000f3')),
    ('manifest miss', 0x14242E1, bytes.fromhex('7ecd33c048'), 0xA900,
     bytes.fromhex('7e3541534883ec28498bcd4885c9480f44cd498bd34d8bc14d8bcae8b091ffff')),
    ('native provider', 0x1424240, bytes.fromhex('4883ec080fb602'), 0xB3C6,
     bytes.fromhex('9c50515241504151415241534881ec88000000f30f7f442420f30f7f4c2430f3')),
)


def _broken(detail):
    return {'state': 'broken', 'message':
            'The custom-face manifest/provider hook is incomplete or changed ('
            + detail + '). Loader repair is required.'}


def inspect_manifest(reader):
    """Check the exact reviewed hook chain without installing or modifying it."""
    game = reader.main_module
    if file_sha256(game.path) != HOOK_EXECUTABLE_SHA256:
        return {'state': 'unverified', 'message':
                'Custom-face manifest/provider hooks use an unverified game build.'}
    module = next((item for item in reader.modules
                   if item.name.casefold() == 'nba2k_hook.dll'), None)
    if module is None:
        return {'state': 'missing', 'message':
                'NBA2K_Hook.dll is not loaded; arbitrary custom face IDs require its manifest/provider hooks.'}
    if file_sha256(module.path) != HOOK_INSTALL_SHA256:
        return {'state': 'unverified', 'message':
                'Custom-face manifest/provider hook version is not verified.'}
    try:
        spans = [reader.read_or_raise(game.base_address + rva, len(original))
                 for _, rva, original, _, _ in CORE_SITES]
        if all(raw == site[2] for raw, site in zip(spans, CORE_SITES)):
            return {'state': 'inactive', 'message':
                    'NBA2K_Hook.dll is loaded, but its custom-face manifest/provider hooks are inactive. '
                    'Loader repair is required.'}
        for raw, (feature, rva, original, hook_rva, bridge) in zip(spans, CORE_SITES):
            if len(raw) != len(original) or raw[:1] != b'\xe9' or raw[5:] != b'\x90' * (len(original) - 5):
                return _broken(feature + ' detour')
            stub_address = game.base_address + rva + 5 + struct.unpack('<i', raw[1:5])[0]
            stub = reader.read_or_raise(stub_address, 14)
            if (len(stub) != 14 or stub[:6] != b'\xff\x25\x00\x00\x00\x00'
                    or struct.unpack('<Q', stub[6:])[0] != module.base_address + hook_rva):
                return _broken(feature + ' jump target')
            if reader.read_or_raise(module.base_address + hook_rva, len(bridge)) != bridge:
                return _broken(feature + ' bridge')
    except (OSError, ValueError, RuntimeError, struct.error) as exc:
        return _broken('code could not be read: ' + str(exc))
    return {'state': 'patched', 'message':
            'Custom-face manifest/provider patches detected; individual models still need an in-game check.'}
