import hashlib
import json
from pathlib import Path
import struct
from types import SimpleNamespace

import pytest

from nba2k17_viewer import game_hook as hook


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def pe_dll():
    raw = bytearray(256)
    raw[:2] = b'MZ'
    struct.pack_into('<I', raw, 0x3c, 0x80)
    raw[0x80:0x84] = b'PE\0\0'
    struct.pack_into('<H', raw, 0x84, 0x8664)
    struct.pack_into('<H', raw, 0x96, 0x2000)
    return bytes(raw)


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    game, payload = tmp_path / 'game', tmp_path / 'payload'
    game.mkdir()
    (payload / 'NBA2K_Hook').mkdir(parents=True)
    config = {'global': {key: 1 for key in hook.HOOK_OPTIONS if key != 'debugLogging'}}
    hook_raw = bytearray(pe_dll() + bytes(0xc500))
    for offset in hook.HOOK_LAYOUT_OFFSETS:
        struct.pack_into('<I', hook_raw, offset, hook.ORIGINAL_HOOK_IMAGE_SIZE)
    raws = {'d3d11.dll': pe_dll() + b'proxy', 'NBA2K_Hook.dll': bytes(hook_raw),
            hook.CONFIG_RELATIVE: json.dumps(config).encode(), 'readme.txt': b'license and instructions'}
    for name, raw in raws.items():
        (payload / name).write_bytes(raw)
    exe = bytearray(pe_dll() + b'supported executable')
    struct.pack_into('<I', exe, 0x88, hook.HOOK_PE_TIMESTAMP)
    struct.pack_into('<I', exe, 0xd0, hook.HOOK_IMAGE_SIZE)
    exe = bytes(exe)
    (game / 'NBA2K17.exe').write_bytes(exe)
    for name in hook.DEPENDENCIES:
        (game / name).write_bytes(pe_dll())
    monkeypatch.setattr(hook, 'hook_payload_dir', lambda: payload)
    monkeypatch.setattr(hook, 'PAYLOAD_HASHES', {name: digest(raw) for name, raw in raws.items()})
    for offset in hook.HOOK_LAYOUT_OFFSETS:
        struct.pack_into('<I', hook_raw, offset, hook.HOOK_IMAGE_SIZE)
    monkeypatch.setattr(hook, 'HOOK_INSTALL_SHA256', digest(hook_raw))
    monkeypatch.setattr(hook, 'supported_executable_sha256', lambda: digest(exe))
    monkeypatch.setattr(hook, '_process_api', lambda: (lambda: [], None, None))
    return game, payload


def test_missing_installation_and_dependency_report(fixture):
    game, _ = fixture
    result = hook.inspect_installation(game)
    assert result['installable'] and not result['ready']
    assert result['executable']['supported']
    assert result['config']['values']['rosterCustomData'] is None
    (game / hook.DEPENDENCIES[0]).unlink()
    result = hook.inspect_installation(game)
    assert not result['installable']
    assert hook.DEPENDENCIES[0] in ' '.join(result['issues'])


def test_injector_support_does_not_override_hook_header_mismatch(fixture, monkeypatch):
    game, _ = fixture
    path = game / 'NBA2K17.exe'
    raw = bytearray(path.read_bytes())
    struct.pack_into('<I', raw, 0xd0, hook.HOOK_IMAGE_SIZE - 0x24000)
    path.write_bytes(raw)
    monkeypatch.setattr(hook, 'supported_executable_sha256', lambda: digest(raw))
    result = hook.inspect_installation(game)
    assert result['executable']['supported']
    assert not result['executable']['hook_supported']
    assert not result['installable'] and not result['ready']
    with pytest.raises(ValueError, match='PE layout'):
        hook.install_hook(game)
    assert not (game / 'd3d11.dll').exists()


def test_install_and_repeat_preserve_existing_settings(fixture):
    game, _ = fixture
    first = hook.install_hook(game)
    assert first['ready'] and len(first['installed_files']) == 4
    assert not (game / 'readme.txt').exists()
    path = game / hook.CONFIG_RELATIVE
    values = json.loads(path.read_text())
    values['global']['unlockedShoesBrands'] = 0
    values['global']['futureOption'] = 42
    path.write_text(json.dumps(values))
    before = path.read_bytes()
    second = hook.install_hook(game)
    assert second['ready'] and second['installed_files'] == []
    assert path.read_bytes() == before
    assert second['config']['values']['unlockedShoesBrands'] is False
    assert second['config']['values']['debugLogging'] is False


def test_compatibility_profile_changes_all_nine_selectors_only(fixture):
    game, payload = fixture
    original = (payload / 'NBA2K_Hook.dll').read_bytes()
    derived = hook.materialize_install_payload('NBA2K_Hook.dll', original)
    differences = {i for i, (a, b) in enumerate(zip(original, derived)) if a != b}
    assert differences == {offset + relative for offset in hook.HOOK_LAYOUT_OFFSETS for relative in (1, 2)}
    assert len(derived) == len(original)
    assert digest(derived) == hook.HOOK_INSTALL_SHA256
    result = hook.install_hook(game)
    assert (game / 'NBA2K_Hook.dll').read_bytes() == derived
    assert (payload / 'NBA2K_Hook.dll').read_bytes() == original
    assert result['files']['NBA2K_Hook.dll']['payload_match']
    assert result['executable']['hook_supported']


def test_partial_or_unrecognized_layout_profile_is_rejected(fixture, monkeypatch):
    game, payload = fixture
    raw = bytearray((payload / 'NBA2K_Hook.dll').read_bytes())
    struct.pack_into('<I', raw, hook.HOOK_LAYOUT_OFFSETS[0], hook.HOOK_IMAGE_SIZE)
    with pytest.raises(ValueError, match='changed'):
        hook.materialize_install_payload('NBA2K_Hook.dll', bytes(raw))
    monkeypatch.setitem(hook.PAYLOAD_HASHES, 'NBA2K_Hook.dll', digest(raw))
    with pytest.raises(ValueError, match='layout selectors'):
        hook.materialize_install_payload('NBA2K_Hook.dll', bytes(raw))
    assert not (game / 'NBA2K_Hook.dll').exists()


@pytest.mark.parametrize('conflict', ['d3d11.dll', 'NBA2K_Hook.dll'])
def test_conflicting_dll_stops_before_any_mutation(fixture, conflict):
    game, _ = fixture
    (game / conflict).write_bytes(b'unrelated third-party hook')
    before = {p.name: p.read_bytes() for p in game.iterdir()}
    with pytest.raises(ValueError, match='conflict'):
        hook.install_hook(game)
    assert {p.name: p.read_bytes() for p in game.iterdir()} == before


def test_unsupported_build_and_corrupt_payload_do_not_install(fixture):
    game, payload = fixture
    exe = game / 'NBA2K17.exe'
    original = exe.read_bytes()
    exe.write_bytes(b'other game build')
    with pytest.raises(ValueError, match='supported executable'):
        hook.install_hook(game)
    exe.write_bytes(original)
    (payload / 'NBA2K_Hook.dll').write_bytes(b'changed payload')
    with pytest.raises(ValueError, match='payload'):
        hook.install_hook(game)
    assert not (game / 'd3d11.dll').exists()


def test_running_game_blocks_install_without_closing_it(fixture, monkeypatch):
    game, _ = fixture
    calls = []
    def running():
        calls.append(1)
        return [(99, 'NBA2K17.exe')]
    monkeypatch.setattr(hook, '_process_api', lambda: (running, None, None))
    with pytest.raises(ValueError, match='Close NBA 2K17'):
        hook.install_hook(game)
    assert calls == [1]
    assert not (game / 'd3d11.dll').exists()


def test_game_launch_during_install_rolls_back_only_new_files(fixture, monkeypatch):
    game, payload = fixture
    (game / 'd3d11.dll').write_bytes((payload / 'd3d11.dll').read_bytes())
    calls = []
    def check():
        calls.append(1)
        if len(calls) == 4:
            raise ValueError('game opened')
    monkeypatch.setattr(hook, '_require_game_closed', check)
    with pytest.raises(ValueError, match='game opened'):
        hook.install_hook(game)
    assert (game / 'd3d11.dll').exists()
    assert not (game / 'NBA2K_Hook.dll').exists()
    assert not (game / 'NBA2K_Hook').exists()


def test_write_failure_rolls_back_new_files(fixture, monkeypatch):
    game, _ = fixture
    original = Path.open
    def failing(path, mode='r', *args, **kwargs):
        if path == game / 'NBA2K_Hook.dll' and mode == 'xb':
            raise OSError('simulated disk error')
        return original(path, mode, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', failing)
    with pytest.raises(OSError, match='disk error'):
        hook.install_hook(game)
    assert not (game / 'd3d11.dll').exists()
    assert (game / 'NBA2K17.exe').exists()
    assert all((game / name).exists() for name in hook.DEPENDENCIES)


def test_atomic_settings_preserve_unknown_data_and_require_refresh(fixture):
    game, _ = fixture
    hook.install_hook(game)
    path = game / hook.CONFIG_RELATIVE
    raw = json.loads(path.read_text())
    raw['future'] = {'nested': ['preserve me']}
    raw['global']['unknown'] = 'preserve too'
    path.write_text(json.dumps(raw))
    old = hook.load_options(game)
    saved = hook.save_options(game, {'forcedisplayphotos': False, 'debugLogging': True}, expected_sha256=old['sha256'])
    assert saved['restart_required']
    current = json.loads(path.read_text())
    assert current['future'] == raw['future'] and current['global']['unknown'] == 'preserve too'
    assert current['global']['forcedisplayphotos'] == 0 and current['global']['debugLogging'] == 1
    before = path.read_bytes()
    with pytest.raises(ValueError, match='changed elsewhere'):
        hook.save_options(game, {'forcedisplayphotos': True}, expected_sha256=old['sha256'])
    assert path.read_bytes() == before


@pytest.mark.parametrize('raw', [b'{bad', b'[]', b'{"global": []}', b'{"global":{},"global":{}}'])
def test_malformed_existing_config_is_not_overwritten(fixture, raw):
    game, _ = fixture
    (game / 'NBA2K_Hook').mkdir()
    path = game / hook.CONFIG_RELATIVE
    path.write_bytes(raw)
    assert hook.load_options(game)['errors']
    with pytest.raises(ValueError):
        hook.install_hook(game)
    with pytest.raises(ValueError):
        hook.save_options(game, {'rosterCustomData': False})
    assert path.read_bytes() == raw
    assert not (game / 'd3d11.dll').exists()


def test_save_replace_failure_keeps_original_and_removes_temp(fixture, monkeypatch):
    game, _ = fixture
    hook.install_hook(game)
    path = game / hook.CONFIG_RELATIVE
    before = path.read_bytes()
    def fail(*_):
        raise OSError('sharing violation')
    monkeypatch.setattr(hook.os, 'replace', fail)
    with pytest.raises(OSError, match='sharing violation'):
        hook.save_options(game, {'rosterCustomData': False})
    assert path.read_bytes() == before
    assert not list(path.parent.glob('.hook-settings-*'))


def test_unknown_option_and_invalid_values_rejected(fixture):
    game, _ = fixture
    hook.install_hook(game)
    for values in ({'foreign': True}, {'rosterCustomData': 'yes'}, {'rosterCustomData': 2}, {'rosterCustomData': 1.0}):
        with pytest.raises(ValueError):
            hook.save_options(game, values)


def test_path_escape_preflight_blocks_external_write(fixture, tmp_path):
    game, _ = fixture
    outside = tmp_path / 'outside'
    outside.mkdir()
    try:
        (game / 'NBA2K_Hook').symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('Creating symlinks is not permitted on this Windows account')
    with pytest.raises(ValueError, match='outside'):
        hook.install_hook(game)
    assert not list(outside.iterdir())
    assert not (game / 'd3d11.dll').exists()


def test_log_tail_is_bounded_and_never_active_status(fixture):
    game, _ = fixture
    (game / 'NBA2K17_Hook.log').write_bytes(b'x' * 20000 + b'rosterCustomData installed')
    result = hook.inspect_installation(game)
    assert len(result['log']['text']) == 16384
    assert result['log']['session_verified'] is False
    assert result['settings_scope'] == 'next_game_launch'
    assert not result['ready']


def test_hash_cache_invalidates_after_file_change(fixture, monkeypatch):
    game, _ = fixture
    path = game / 'NBA2K17.exe'
    hook._hash_stat.cache_clear()
    a = hook.file_sha256(path)
    assert hook.file_sha256(path) == a
    assert hook._hash_stat.cache_info().hits == 1
    path.write_bytes(path.read_bytes() + b'changed')
    assert hook.file_sha256(path) != a


def test_process_probe_separates_module_loaded_from_verified_features(fixture, monkeypatch):
    game, _ = fixture
    hook.install_hook(game)
    module = SimpleNamespace(name='NBA2K_Hook.dll', path=str(game / 'NBA2K_Hook.dll'), base_address=1000, size=100)
    class Reader:
        def __init__(self, pid): self.pid = pid
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def process_info(self):
            return SimpleNamespace(executable_path=str(game / 'NBA2K17.exe'), modules=[module])
    monkeypatch.setattr(hook, '_process_api', lambda: (lambda: [(10, 'NBA2K17.exe')], Reader, lambda _: '123'))
    result = hook.probe_processes()
    item = result['processes'][0]
    assert result['error'] is None and item['error'] is None
    assert item['supported'] and item['hook_loaded'] and item['hook_verified']
    assert set(item['feature_status'].values()) == {'unverified'}
    assert item['token'] == '123' and item['pid'] == 10
    json.dumps(result)
    (game / 'NBA2K_Hook.dll').write_bytes(b'foreign module')
    item = hook.probe_processes()['processes'][0]
    assert item['hook_loaded'] and not item['hook_verified']


def test_probe_failure_is_reported_not_treated_as_game_closed(monkeypatch):
    def denied():
        raise OSError('access denied')
    monkeypatch.setattr(hook, '_process_api', lambda: (denied, None, None))
    result = hook.probe_processes()
    assert result['processes'] == [] and result['error'] == 'access denied'
    with pytest.raises(OSError):
        hook._require_game_closed()
