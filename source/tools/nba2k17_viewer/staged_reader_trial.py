"""Verified-build reader initializer, also available as an opt-in trial CLI.

Default with --pid is READ ONLY: no allocation, debugger attachment, suspension,
DLL loading, or writes. --apply-controlled-game-test is the explicit CLI gate.
Production calls are limited to an explicit injection through custom_face_runtime.
One controlled roster test confirmed Luka's custom model; this does not assert
startup behavior or compatibility with another build. No game is terminated here.
"""
import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import struct
import time

from .staged_mod_reader import (GAME_SHA256, COMPANION_SHA256, SITES, RecoveryRequired,
                                build_reader_plan, publish_experimental)
from .staged_reader_harness import K, WindowsPublicationPrimitives, _creation, _ok


DINPUT_SHA256 = '45b8743be441fcf00dedc15ef974e4aea08569f3cb4b2be530e5f69bc31972ad'
ORIGINAL_COMPANION = '6dbcdd5dde699af266430ae59c58b99ad1fe3765fc9e11bf947ddc31b13668d1'


class _ModuleEntry(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('module_id', wintypes.DWORD),
                ('pid', wintypes.DWORD), ('global_usage', wintypes.DWORD),
                ('process_usage', wintypes.DWORD), ('base', ctypes.c_void_p),
                ('image_size', wintypes.DWORD), ('module', wintypes.HMODULE),
                ('name', wintypes.WCHAR * 256), ('path', wintypes.WCHAR * 260)]


def _configure():
    declarations = {
        'Module32FirstW': (wintypes.BOOL, [wintypes.HANDLE, ctypes.c_void_p]),
        'Module32NextW': (wintypes.BOOL, [wintypes.HANDLE, ctypes.c_void_p]),
        'DuplicateHandle': (wintypes.BOOL, [wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
                                         ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]),
        'WaitForSingleObject': (wintypes.DWORD, [wintypes.HANDLE, wintypes.DWORD]),
        'GetExitCodeThread': (wintypes.BOOL, [wintypes.HANDLE, ctypes.c_void_p]),
        'VirtualAllocEx': (ctypes.c_void_p, [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t,
                                         wintypes.DWORD, wintypes.DWORD]),
        'VirtualFreeEx': (wintypes.BOOL, [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD]),
    }
    for name, (result, arguments) in declarations.items():
        getattr(K, name).restype, getattr(K, name).argtypes = result, arguments


def _modules(pid):
    snapshot = K.CreateToolhelp32Snapshot(0x8 | 0x10, pid)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    result = []
    try:
        entry = _ModuleEntry()
        entry.size = ctypes.sizeof(entry)
        present = K.Module32FirstW(snapshot, ctypes.byref(entry))
        while present:
            # A proxy loader and the Windows system DLL legitimately have the
            # same basename. Preserve every mapping rather than replacing one.
            result.append((entry.base, entry.image_size, Path(entry.path)))
            present = K.Module32NextW(snapshot, ctypes.byref(entry))
        if ctypes.get_last_error() not in (0, 18):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        _ok(K.CloseHandle(snapshot))
    return tuple(result)


def _select_reviewed_modules(modules):
    """Bind one game and its same-directory dependencies, independent of order."""
    games = [module for module in modules if module[2].name.casefold() == 'nba2k17.exe']
    if len(games) != 1:
        raise RuntimeError('The explicit process must have one unambiguous NBA2K17 executable mapping.')
    game = games[0]
    directory = os.path.normcase(str(game[2].parent.resolve(strict=True)))
    selected = [(game, GAME_SHA256)]
    for filename, digest in (('2k17waiguadll.dll', ORIGINAL_COMPANION), ('dinput8.dll', DINPUT_SHA256)):
        matches = [module for module in modules
                   if module[2].name.casefold() == filename
                   and os.path.normcase(str(module[2].parent.resolve(strict=True))) == directory]
        if len(matches) != 1:
            raise RuntimeError(f'The game folder must have one unambiguous loaded {filename} mapping.')
        selected.append((matches[0], digest))
    result = []
    for module, expected in selected:
        raw = module[2].read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise RuntimeError(f'The game-folder {module[2].name} is not the reviewed image.')
        result.append((module, raw))
    return tuple(result)


class ControlledGameTrial(WindowsPublicationPrimitives):
    """Explicit-PID trial adapter. Construction and readiness() are read only."""

    def __init__(self, pid, *, allow_apply=False):
        if os.name != 'nt' or ctypes.sizeof(ctypes.c_void_p) != 8:
            raise OSError('A controlled trial requires 64-bit Windows.')
        _configure()
        self.pid = int(pid)
        self.allow_apply = bool(allow_apply)
        self.handle = None
        self.attached = self.retained = self.published = False
        self.pending = None
        self.allocation = None
        self.may_be_reachable = False
        self.recovery_verified = False
        self.plan = None
        self.events = []
        self.original_protections = {}
        # Query/read/duplicate/synchronize are enough for dry runs. No VM_WRITE or
        # VM_OPERATION permission is requested without the explicit apply gate.
        rights = 0x400 | 0x10 | 0x40 | 0x100000
        if allow_apply:
            rights |= 0x20 | 0x8
        try:
            self.handle = _ok(K.OpenProcess(rights, False, self.pid))
            self.creation = _creation(self.handle)
            ((self.game, self.game_image), (self.companion, self.companion_image),
             (self.dinput, self.dinput_image)) = _select_reviewed_modules(_modules(self.pid))
            self.headers = tuple((module[0], self.read(module[0], 0x1000))
                                 for module in (self.game, self.companion, self.dinput))
            self.bind()
        except BaseException:
            self.close()
            raise

    def _lifecycle(self):
        base = self.companion[0]
        once = struct.unpack('<I', self.read(base + 0x5210C, 4))[0]
        worker = struct.unpack('<Q', self.read(base + 0x520F8, 8))[0]
        if not 1 <= once <= 16 or not worker:
            raise RuntimeError('Legacy loader once-state/worker handle is not the reviewed settled case.')
        duplicate = wintypes.HANDLE()
        _ok(K.DuplicateHandle(self.handle, worker, K.GetCurrentProcess(), ctypes.byref(duplicate), 0, False, 2))
        try:
            if K.WaitForSingleObject(duplicate, 0) != 0:
                raise RuntimeError('The legacy loader worker has not exited; no trial is permitted.')
            code = wintypes.DWORD()
            _ok(K.GetExitCodeThread(duplicate, ctypes.byref(code)))
            if code.value == 259:
                raise RuntimeError('The legacy loader worker is still active.')
        finally:
            _ok(K.CloseHandle(duplicate))
        return {'once_state': once, 'worker_handle': hex(worker), 'worker_exited': True}

    def bind(self):
        if _creation(self.handle) != self.creation or K.WaitForSingleObject(self.handle, 0) != 258:
            raise RuntimeError('The selected process identity/lifetime changed.')
        for address, expected in self.headers:
            if self.read(address, len(expected)) != expected:
                raise RuntimeError('A bound loaded-module header changed.')
        self._lifecycle()
        self.events.append('bound_identity_and_finished_worker')

    def _prospective_plan(self, stub=None):
        # For read-only readiness, a hypothetical free address is sufficient to
        # check witnesses. The apply path rebuilds using its actual owned address.
        return build_reader_plan(self.game_image, self.companion_image,
                                 self.game[0], self.companion[0], stub or self.game[0] - 0x10000)

    def readiness(self):
        self.bind()
        plan = self._prospective_plan()
        for witness in plan.witnesses:
            if self.read(witness.address, len(witness.data)) != witness.data:
                raise RuntimeError(f'Read-only readiness failed: {witness.name} differs.')
        return {'mode': 'read_only', 'pid': self.pid, 'process_creation': self.creation,
                'ready_for_controlled_trial': True, 'applied': False, 'rendering_verified': False,
                'debugger_attached': False, 'allocated': False, 'writes': 0,
                'lifecycle': self._lifecycle()}

    def _allocate_near(self):
        if not self.allow_apply:
            raise RuntimeError('Read-only mode cannot allocate target memory.')
        # Request an exact64KiB-aligned address close to both game sites. Failed
        # VirtualAllocEx requests do not mutate existing allocations.
        for step in range(1, 0x4000):
            for address in (self.game[0] - step * 0x10000, self.game[0] + step * 0x10000):
                if address < 0x10000:
                    continue
                pointer = K.VirtualAllocEx(self.handle, address, 0x1000, 0x3000, 0x04)
                if pointer:
                    if pointer != address:
                        K.VirtualFreeEx(self.handle, pointer, 0, 0x8000)
                        raise RuntimeError('Near allocation did not use its requested address.')
                    self.allocation = pointer
                    return pointer
        raise RuntimeError('No owned stub allocation was available in rel32 range.')

    def stage(self, address, data):
        if not self.allow_apply or address != self.allocation or len(data) != 208:
            raise RuntimeError('Only this trial\'s owned reader allocation can be staged.')
        if self.protection(address) != 0x04 or self.read(address, len(data)) != bytes(len(data)):
            raise RuntimeError('The owned reader allocation is not fresh and writable.')
        self._write(address, data)
        if self.read(address, len(data)) != data:
            raise RuntimeError('Reader stub failed readback.')
        self._protect(address, len(data), 0x20)
        if self.protection(address) != 0x20:
            raise RuntimeError('Reader stub RX protection was not established.')
        self.flush(address, len(data))
        self.events.append('complete_stub_rx_flushed')

    def write_code(self, address, data):
        permitted = next((p for p in self.plan.patches if p.address == address), None) if self.plan else None
        if (not self.allow_apply or not self.pending or permitted is None
                or data not in (permitted.before, permitted.after)):
            raise RuntimeError('Only reviewed code bytes may be published under a debugger stop.')
        # Sticky before the first possible publication. A later debugger-resume
        # or detach failure must never cause close() to free a referenced stub.
        # Even a rolled-back trial conservatively retains this one page until
        # process exit; this avoids reasoning about post-resume execution races.
        if data == permitted.after:
            self.may_be_reachable = True
        if address not in self.original_protections:
            self.original_protections[address] = self.protection(address)
        original = self.original_protections[address]
        self._protect(address, len(data), 0x40)
        try:
            self._write(address, data)
        finally:
            self._protect(address, len(data), original)
            if self.protection(address) != original:
                raise RuntimeError('Original game code protection could not be restored.')

    def apply(self, journal):
        if not self.allow_apply:
            raise RuntimeError('The explicit controlled-game-test gate is disabled.')
        self.readiness()
        self.plan = self._prospective_plan(self._allocate_near())
        # Small transaction evidence, not a project/file backup. Contains exactly
        # the two original instructions and this process token for recovery.
        evidence = json.dumps({
            'pid': self.pid, 'creation': self.creation, 'stub': hex(self.plan.stub_address),
            'patches': [{'address': hex(p.address), 'before': p.before.hex(), 'after': p.after.hex()}
                        for p in self.plan.patches], 'rendering_verified': False,
        }, indent=2) + '\n'
        with Path(journal).open('x', encoding='utf-8') as stream:
            stream.write(evidence)
        result = publish_experimental(self.plan, self, enabled=True)
        self.published = True
        return result

    def recover_while_stopped(self):
        """Retry original code/protection restoration without executing target code."""
        if K.WaitForSingleObject(self.handle, 0) == 0:
            # The user/OS may end the process independently. Nothing remains to
            # protect, and this tool must not spin forever or free remote memory.
            self.pending = None
            if self.attached:
                K.DebugActiveProcessStop(self.pid)
            self.attached = self.retained = False
            self.allocation = None
            return
        if self.retained and self.recovery_verified and not self.pending:
            # Original code was verified and its event continued, but detaching
            # failed. Retry only detach; there is no stopped event to patch now.
            if self.attached:
                _ok(K.DebugActiveProcessStop(self.pid))
            self.attached = self.retained = False
            return
        if not self.retained or not self.pending or self.plan is None:
            raise RuntimeError('No retained transaction is available.')
        self.bind()
        for patch in reversed(self.plan.patches):
            self.write_code(patch.address, patch.before)
        for patch in self.plan.patches:
            if self.read(patch.address, len(patch.before)) != patch.before:
                raise RecoveryRequired('Original instruction bytes still differ.')
            self.flush(patch.address, len(patch.before))
        self.recovery_verified = True
        self._continue()
        _ok(K.DebugActiveProcessStop(self.pid))
        self.attached = self.retained = False

    def close(self):
        if self.handle and K.WaitForSingleObject(self.handle, 0) == 0:
            # Independent process exit ends any need to restore remote code.
            self.pending = None
            if self.attached:
                K.DebugActiveProcessStop(self.pid)
            self.attached = self.retained = False
            self.allocation = None
        if self.retained:
            raise RecoveryRequired('Keep this trial owner open: original code has not been verified.')
        if self.attached:
            self._continue()
            _ok(K.DebugActiveProcessStop(self.pid))
            self.attached = False
        if self.allocation and not self.published and not self.may_be_reachable:
            _ok(K.VirtualFreeEx(self.handle, self.allocation, 0, 0x8000))
            self.allocation = None
        if self.handle:
            _ok(K.CloseHandle(self.handle))
            self.handle = None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid', type=int, required=True, help='Explicit target; never auto-discovered.')
    parser.add_argument('--apply-controlled-game-test', action='store_true')
    parser.add_argument('--journal', type=Path)
    args = parser.parse_args(argv)
    if args.apply_controlled_game_test and not args.journal:
        parser.error('--journal is required for an experimental apply')
    trial = ControlledGameTrial(args.pid, allow_apply=args.apply_controlled_game_test)
    try:
        result = trial.apply(args.journal) if args.apply_controlled_game_test else trial.readiness()
        print(json.dumps(result, indent=2))
    except RecoveryRequired:
        # Never silently detach/exit with unknown instructions. No process killing
        # option exists. Keep this owner alive and retry restoring originals.
        while trial.retained:
            try:
                print('Recovery required: keeping the target stopped; retrying original code restoration.', flush=True)
                time.sleep(1)
                trial.recover_while_stopped()
            except (Exception, KeyboardInterrupt) as error:
                print(f'Recovery remains incomplete: {error}', flush=True)
        raise RuntimeError('The original reader instructions were recovered; the trial was not applied.')
    finally:
        if not trial.retained:
            trial.close()


if __name__ == '__main__':
    main()
