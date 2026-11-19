"""Windows-only disposable-process harness for staged_mod_reader publication.

This module cannot select an existing process. It creates and owns a synthetic
child, and verifies its nonce, creation time, and exact command before attaching
a debugger. It never attaches to NBA2K17, invokes DLL entrypoints, or loads mods.
Only shared publication primitives are reused by the separately bound production
adapter; the disposable child constructor is never used by production UI/code.
"""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import queue
import secrets
import struct
import subprocess
import sys
import threading
import time

from .staged_mod_reader import Patch, ReaderPlan, Witness, publish_experimental


if os.name == 'nt':
    K = ctypes.WinDLL('kernel32', use_last_error=True)
    _api = {
        'OpenProcess': (wintypes.HANDLE, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]),
        'GetProcessTimes': (wintypes.BOOL, [wintypes.HANDLE] + [ctypes.c_void_p] * 4),
        'ReadProcessMemory': (wintypes.BOOL, [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p]),
        'WriteProcessMemory': (wintypes.BOOL, [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p]),
        'VirtualProtectEx': (wintypes.BOOL, [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, ctypes.c_void_p]),
        'VirtualQueryEx': (ctypes.c_size_t, [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]),
        'FlushInstructionCache': (wintypes.BOOL, [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t]),
        'CloseHandle': (wintypes.BOOL, [wintypes.HANDLE]),
        'DebugActiveProcess': (wintypes.BOOL, [wintypes.DWORD]),
        'DebugActiveProcessStop': (wintypes.BOOL, [wintypes.DWORD]),
        'DebugSetProcessKillOnExit': (wintypes.BOOL, [wintypes.BOOL]),
        'WaitForDebugEvent': (wintypes.BOOL, [ctypes.c_void_p, wintypes.DWORD]),
        'ContinueDebugEvent': (wintypes.BOOL, [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]),
        'CreateToolhelp32Snapshot': (wintypes.HANDLE, [wintypes.DWORD, wintypes.DWORD]),
        'Thread32First': (wintypes.BOOL, [wintypes.HANDLE, ctypes.c_void_p]),
        'Thread32Next': (wintypes.BOOL, [wintypes.HANDLE, ctypes.c_void_p]),
        'OpenThread': (wintypes.HANDLE, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]),
        'GetThreadContext': (wintypes.BOOL, [wintypes.HANDLE, ctypes.c_void_p]),
        'VirtualAlloc': (ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]),
        'CreateThread': (wintypes.HANDLE, [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]),
        'GetCurrentProcess': (wintypes.HANDLE, []),
    }
    for _name, (_restype, _argtypes) in _api.items():
        getattr(K, _name).restype, getattr(K, _name).argtypes = _restype, _argtypes


def _ok(result):
    if not result:
        raise ctypes.WinError(ctypes.get_last_error())
    return result


def _creation(handle):
    values = [ctypes.c_uint64() for _ in range(4)]
    _ok(K.GetProcessTimes(handle, *(ctypes.byref(value) for value in values)))
    return values[0].value


def _thread_pcs(pid):
    # THREADENTRY32 is architecture-independent; CONTEXT is 16-byte aligned x64.
    class Entry(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('usage', wintypes.DWORD),
                    ('tid', wintypes.DWORD), ('pid', wintypes.DWORD),
                    ('base', wintypes.LONG), ('delta', wintypes.LONG), ('flags', wintypes.DWORD)]
    snapshot = K.CreateToolhelp32Snapshot(4, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    pcs = []
    try:
        entry = Entry()
        entry.size = ctypes.sizeof(entry)
        present = K.Thread32First(snapshot, ctypes.byref(entry))
        while present:
            if entry.pid == pid:
                handle = _ok(K.OpenThread(0x8 | 0x40, False, entry.tid))
                try:
                    storage = ctypes.create_string_buffer(1232 + 15)
                    address = (ctypes.addressof(storage) + 15) & ~15
                    ctypes.c_uint32.from_address(address + 48).value = 0x100001
                    _ok(K.GetThreadContext(handle, address))
                    pcs.append(ctypes.c_uint64.from_address(address + 248).value)
                finally:
                    _ok(K.CloseHandle(handle))
            present = K.Thread32Next(snapshot, ctypes.byref(entry))
        if ctypes.get_last_error() not in (0, 18):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        _ok(K.CloseHandle(snapshot))
    if not pcs:
        raise RuntimeError('No stopped child threads could be verified.')
    return tuple(pcs)


class _StopLease:
    retained = False

    def retain_for_recovery(self):
        self.retained = True


class DisposableReaderHarness:
    """Own one synthetic child. No constructor accepts a process ID."""

    def __init__(self):
        if os.name != 'nt' or ctypes.sizeof(ctypes.c_void_p) != 8:
            raise OSError('The disposable publication harness requires 64-bit Windows.')
        nonce = secrets.token_hex(24)
        command = [sys.executable, '-u', '-m', 'nba2k17_viewer.staged_reader_harness', '--child', nonce]
        environment = dict(os.environ)
        environment['PYTHONPATH'] = str(Path(__file__).resolve().parents[1])
        self.child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True, env=environment,
                                      creationflags=subprocess.CREATE_NO_WINDOW)
        self.events = []
        self.attached = False
        self.pending = None
        self.retained = False
        self.handle = None
        self.original_protections = {}
        try:
            self.info = self._read_result()
            if (self.info.get('nonce') != nonce or self.info.get('pid') != self.child.pid
                    or self.child.args != command):
                raise RuntimeError('Disposable child handshake failed.')
            self.handle = _ok(K.OpenProcess(0x438, False, self.child.pid))
            self.creation = _creation(self.handle)
            if self.creation != self.info['creation']:
                raise RuntimeError('Disposable child identity changed.')
        except BaseException:
            self.close()
            raise

    def bind(self):
        if self.child.poll() is not None or _creation(self.handle) != self.creation:
            raise RuntimeError('Disposable child identity changed.')
        self.events.append('bind')

    @property
    def pid(self):
        return self.child.pid

    def _read_result(self):
        result = queue.Queue(maxsize=1)
        def read():
            try:
                result.put(self.child.stdout.readline())
            except BaseException as error:
                result.put(error)
        threading.Thread(target=read, daemon=True).start()
        try:
            line = result.get(timeout=10)
        except queue.Empty as error:
            raise TimeoutError('Disposable child response timed out.') from error
        if isinstance(line, BaseException):
            raise line
        if not line:
            raise RuntimeError('Disposable child closed its output before responding.')
        return json.loads(line)

    def read(self, address, size):
        data, count = ctypes.create_string_buffer(size), ctypes.c_size_t()
        _ok(K.ReadProcessMemory(self.handle, address, data, size, ctypes.byref(count)))
        if count.value != size:
            raise RuntimeError('Incomplete child memory read.')
        return data.raw

    def _write(self, address, data):
        count = ctypes.c_size_t()
        _ok(K.WriteProcessMemory(self.handle, address, data, len(data), ctypes.byref(count)))
        if count.value != len(data):
            raise RuntimeError('Incomplete child memory write.')

    def _protect(self, address, size, protection):
        old = wintypes.DWORD()
        _ok(K.VirtualProtectEx(self.handle, address, size, protection, ctypes.byref(old)))
        return old.value

    def protection(self, address):
        # MEMORY_BASIC_INFORMATION64; RegionSize at24, Protect at36.
        info = ctypes.create_string_buffer(48)
        if K.VirtualQueryEx(self.handle, address, info, len(info)) != len(info):
            raise ctypes.WinError(ctypes.get_last_error())
        return struct.unpack_from('<I', info.raw, 36)[0]

    def flush(self, address, size):
        _ok(K.FlushInstructionCache(self.handle, address, size))
        self.events.append('flush')

    def stage(self, address, data):
        if address != self.info['stub'] or len(data) > 0x1000:
            raise RuntimeError('Only the child-owned fresh stub allocation is writable.')
        if self.read(address, len(data)) != bytes(len(data)):
            raise RuntimeError('The child stub allocation is not fresh.')
        self._write(address, data)
        if self.read(address, len(data)) != data:
            raise RuntimeError('Stub readback failed.')
        self._protect(address, len(data), 0x20)
        if self.protection(address) != 0x20:
            raise RuntimeError('Stub executable protection was not established.')
        self.flush(address, len(data))
        self.events.append('staged_rx')

    def write_code(self, address, data):
        if not self.pending:
            raise RuntimeError('Code publication requires a pending debugger stop event.')
        if address not in self.info['sites'] or len(data) != 5:
            raise RuntimeError('Only synthetic harness patch sites may be written.')
        if address not in self.original_protections:
            self.original_protections[address] = self.protection(address)
        original = self.original_protections[address]
        self._protect(address, len(data), 0x40)
        try:
            self._write(address, data)
        finally:
            # Preserve the first observed protection across failed attempts.
            # A failed restore must not redefine an RWX page as the baseline.
            self._protect(address, len(data), original)
            if self.protection(address) != original:
                raise RuntimeError('Original code protection could not be verified.')
        self.events.append('write_code')

    def _continue(self):
        if self.pending:
            pid, tid, status = self.pending
            _ok(K.ContinueDebugEvent(pid, tid, status))
            self.pending = None

    @contextmanager
    def quiesce(self):
        lease = _StopLease()
        try:
            _ok(K.DebugActiveProcess(self.pid))
            self.attached = True
            _ok(K.DebugSetProcessKillOnExit(False))
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                event = ctypes.create_string_buffer(512)
                if not K.WaitForDebugEvent(event, 100):
                    if ctypes.get_last_error() == 121:
                        continue
                    raise ctypes.WinError(ctypes.get_last_error())
                kind, pid, tid = struct.unpack_from('<III', event.raw)
                status = 0x80010001 if kind == 1 else 0x10002  # preserve other exceptions
                self.pending = (pid, tid, status)
                if pid != self.pid:
                    raise RuntimeError('Debugger event did not belong to the disposable child.')
                # The debugger owns image-file handles. Process/thread event
                # handles belong to the debugging lifecycle; closing them here
                # breaks DebugActiveProcessStop (ERROR_ACCESS_DENIED).
                if kind in (3, 6):
                    handle = struct.unpack_from('<Q', event.raw, 16)[0]
                    if handle:
                        _ok(K.CloseHandle(handle))
                if kind == 1 and struct.unpack_from('<I', event.raw, 16)[0] == 0x80000003:
                    self.pending = (pid, tid, 0x10002)
                    lease.instruction_pointers = _thread_pcs(pid)
                    self.events.append('all_threads_stopped')
                    yield lease
                    return
                self._continue()
            raise TimeoutError('No initial child debugger breakpoint arrived.')
        finally:
            if lease.retained:
                self.retained = True
                self.events.append('retained_for_recovery')
            else:
                self._continue()
                if self.attached:
                    _ok(K.DebugActiveProcessStop(self.pid))
                    self.attached = False
                self.events.append('resumed')

    def synthetic_plan(self):
        original = bytes.fromhex('b801000000')  # mov eax,1; ret is untouched
        stub = bytes.fromhex('b807000000c3')  # replacement returns7
        patches = tuple(Patch(site, original, b'\xe9' + struct.pack('<i', self.info['stub'] - site - 5))
                        for site in self.info['sites'])
        return ReaderPlan(self.info['stub'], stub, patches,
                          tuple(Witness(p.address, original + b'\xc3', 'synthetic function') for p in patches))

    def result(self):
        self.child.stdin.write('result\n')
        self.child.stdin.flush()
        return self._read_result()

    def close(self):
        # This is only our disposable child, never the user's game. A deliberately
        # failed rollback is terminated while stopped, rather than resumed corrupt.
        if hasattr(self, 'child') and self.child.poll() is None:
            self.child.kill()
            if self.attached:
                self._continue()
                K.DebugActiveProcessStop(self.child.pid)
                self.attached = False
            self.child.wait(timeout=10)
        if self.handle:
            _ok(K.CloseHandle(self.handle))
            self.handle = None
        if hasattr(self, 'child'):
            for stream in (self.child.stdin, self.child.stdout, self.child.stderr):
                if stream:
                    stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class WindowsPublicationPrimitives:
    """Reviewed operations, without a constructor or process-lifetime policy.

    A separate adapter must own identity checks, allocation, permitted patches,
    and cleanup. These exact operations are exercised by the child harness above.
    In particular, the disposable child's killing close() is never inherited.
    """
    read = DisposableReaderHarness.read
    _write = DisposableReaderHarness._write
    _protect = DisposableReaderHarness._protect
    protection = DisposableReaderHarness.protection
    flush = DisposableReaderHarness.flush
    _continue = DisposableReaderHarness._continue
    quiesce = DisposableReaderHarness.quiesce


def _child(nonce):
    if os.name != 'nt':
        raise OSError('Windows required.')
    block = _ok(K.VirtualAlloc(None, 0x3000, 0x3000, 0x40))
    sites, stub = (block, block + 0x100), block + 0x1000
    for site in sites:
        ctypes.memmove(site, bytes.fromhex('b801000000c3'), 6)
    # Native worker continuously calls both patch sites while the parent prepares
    # its stub. This exercises concurrent execution, independent of Python's GIL.
    worker = block + 0x200
    code = (b'\x48\x83\xec\x28' + b'\x48\xb8' + struct.pack('<Q', sites[0]) + b'\xff\xd0'
            + b'\x48\xb8' + struct.pack('<Q', sites[1]) + b'\xff\xd0' + b'\xeb\xe6')
    ctypes.memmove(worker, code, len(code))
    current = K.GetCurrentProcess()
    old = wintypes.DWORD()
    _ok(K.VirtualProtectEx(current, block, 0x1000, 0x20, ctypes.byref(old)))
    _ok(K.VirtualProtectEx(current, stub, 0x1000, 0x04, ctypes.byref(old)))
    _ok(K.FlushInstructionCache(current, block, 0x1000))
    worker_handle = _ok(K.CreateThread(None, 0, worker, None, 0, None))
    _ok(K.CloseHandle(worker_handle))
    print(json.dumps({'nonce': nonce, 'pid': os.getpid(), 'creation': _creation(current),
                      'sites': sites, 'stub': stub}), flush=True)
    for command in sys.stdin:
        if command.strip() == 'result':
            calls = [ctypes.WINFUNCTYPE(ctypes.c_int)(site)() for site in sites]
            print(json.dumps({'results': calls}), flush=True)


def main():
    if len(sys.argv) == 3 and sys.argv[1] == '--child':
        _child(sys.argv[2])
        return
    if sys.argv[1:] != ['--self-test-disposable-child']:
        raise SystemExit('Only --self-test-disposable-child is supported; existing processes cannot be selected.')
    with DisposableReaderHarness() as harness:
        outcome = publish_experimental(harness.synthetic_plan(), harness, enabled=True)
        outcome.update(harness.result())
        outcome['events'] = harness.events
        print(json.dumps(outcome, indent=2))


if __name__ == '__main__':
    main()
